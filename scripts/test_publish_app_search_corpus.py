import collections
import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import publish_app_search_corpus as publisher


class Response:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self.payload = status, payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return copy.deepcopy(self.payload)


class MemorySearch:
    """No network: implement only resource operations, upload and wildcard reads."""

    def __init__(self, definitions=None, documents=None, page_size=1000):
        self.resources = copy.deepcopy(definitions or {})
        self.documents = copy.deepcopy(documents or {})
        self.calls = []
        self.page_size = page_size
        self.upload_hook = None
        self.search_hook = None

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path, copy.deepcopy(body), headers))
        if path == f"/knowledgebases('{publisher.MODEL_SOURCE_KB}')":
            assert method == "GET"
            return Response(200, {
                "models": publisher.MODELS,
                "retrievalInstructions": "OLD GRAPH INSTRUCTIONS MUST NOT BE COPIED",
            })
        if path.endswith("/docs/index"):
            if self.upload_hook:
                return self.upload_hook(body)
            for action in body["value"]:
                assert action["@search.action"] == "upload"
                doc = {k: v for k, v in action.items() if k != "@search.action"}
                assert set(doc) == set(publisher.SCHEMA)
                self.documents[doc["uid"]] = doc
            return Response(200, {"value": [
                {"key": d["uid"], "status": True, "statusCode": 201}
                for d in body["value"]
            ]})
        if path.endswith("/docs/search"):
            assert body["search"] == "*" and body["orderby"] == "uid asc"
            assert body["queryType"] == "simple" and body["top"] == 1000
            assert set(body["select"].split(",")) == set(publisher.SCHEMA)
            assert "skip" not in body
            if self.search_hook:
                return self.search_hook(body)
            last = re.fullmatch(r"uid gt '([A-Za-z0-9_-]+)'", body["filter"])[1] if "filter" in body else ""
            rows = [
                {**self.documents[k], "@search.score": 1.0}
                for k in sorted(self.documents) if k > last
            ][:self.page_size]
            return Response(200, {"value": rows})
        match = re.fullmatch(r"/(indexes|knowledgesources|knowledgebases)\('([^']+)'\)", path)
        assert match, f"Unexpected request path: {path}"
        kind, name = match.groups()
        if method == "GET":
            return Response(200, self.resources[kind]) if kind in self.resources else Response(404)
        assert method == "PUT" and headers["If-None-Match"] == "*"
        assert headers["Prefer"] == "return=representation" and body["name"] == name
        if kind in self.resources:
            return Response(412)
        self.resources[kind] = copy.deepcopy(body)
        return Response(201, body)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.prepared = self.root / "prepared"
        self.prepared.mkdir()
        self.report = self.root / "verification.json"
        self.api_config = self.root / "corpus.json"
        self.sleeps = []
        catalog, docs, correspondence = [], [], []
        for number in range(3):
            sid = f"source-{number}"
            canonical = "speech-a" if number < 2 else "speech-b"
            text = f"Transcript {canonical}: " + "words 😀 " * 520 + "EXACT-END"
            base = {
                "source_id": sid, "canonical_id": canonical,
                "title": canonical, "speaker": "A President", "date": "2026-07-16",
                "transcript_sha256": publisher.digest(text.encode()),
                "raw_sha256": publisher.digest(f"raw-{number}".encode()),
                "projection": "html-readable-v1", "projection_sha256": publisher.digest(text.encode()),
            }
            catalog.append({
                "id": sid, **{k: base[k] for k in (
                    "title", "speaker", "date", "transcript_sha256", "canonical_id",
                )},
            })
            start = index = 0
            while True:
                end = min(start + 4000, len(text))
                docs.append({
                    **base, "uid": f"{sid}-{index:06d}", "snippet": text[start:end],
                    "chunk_index": index, "projection_start": start, "projection_end": end,
                })
                index += 1
                if end == len(text):
                    break
                start = end - 500
            correspondence.append({
                **base, "original_transcript": text, "projection_text": text,
                "transcript_utf8_bytes": len(text.encode()), "projection_characters": len(text),
                "chunk_count": index,
            })
        topics = [{
            "id": "topic-" + publisher.digest(f"topic {n}".encode())[7:],
            "name": f"topic {n}", "aliases": [f"alias {n}"],
        } for n in range(61)]
        for name, value in (("catalog.json", catalog), ("topics.json", {
                "seed_metadata_only": True, "topics": topics})):
            (self.prepared / name).write_bytes(publisher.encode(value))
        self.write_lines("docs.jsonl", docs)
        self.write_lines("correspondence.jsonl", correspondence)
        counts = dict(collections.Counter(d["source_id"] for d in docs))
        self.manifest = {
            "status": "complete", "input_hashes_unchanged": True, "source_count": 3,
            "catalog_count": 3, "canonical_speech_count": 2, "chunk_count": len(docs),
            "covered_source_count": 3, "source_coverage_percent": 100,
            "chunk_size_characters": 4000, "overlap_characters": 500,
            "projection": "html-readable-v1", "index_schema": publisher.SCHEMA,
            "index_key": "uid", "searchable_fields": ["title", "speaker", "snippet"],
            "chunk_count_by_source": counts,
            "duplicate_source_groups": {"speech-a": ["source-0", "source-1"]},
        }
        self.expected_chunks = len(docs)
        self.seal()
        self.bundle = self.load()

    def write_lines(self, name, values):
        (self.prepared / name).write_bytes(b"".join(publisher.encode(v) + b"\n" for v in values))

    def read_lines(self, name):
        return [json.loads(line) for line in (self.prepared / name).read_bytes().splitlines()]

    def seal(self):
        self.manifest["artifacts"] = {
            n: publisher.file_hash(self.prepared / n) for n in publisher.ARTIFACTS
        }
        (self.prepared / "manifest.json").write_bytes(publisher.encode(self.manifest))

    def load(self):
        return publisher.load_bundle(self.prepared, 3, self.expected_chunks, 2)

    def apply_fake(self, rest, api_config=None):
        return publisher.publish(
            self.bundle, rest, self.report, sleep=self.sleeps.append, api_config=api_config
        )

    def test_new_target_publishes_and_verifies_only_its_own_index_and_keyless_model(self):
        target = publisher.PublicationTarget(
            endpoint="https://new-search.search.windows.net", index="new-index",
            knowledge_source="new-source", knowledge_base="new-kb",
            model_uri="https://new-foundry.cognitiveservices.azure.com",
        )
        rest = MemorySearch()
        model_file = self.root / "model.json"
        model_file.write_text(json.dumps({"models": target.models}))
        publisher.publish(self.bundle, rest, self.report, sleep=self.sleeps.append,
                          api_config=self.api_config, model_file=model_file, target=target)
        paths = [call[1] for call in rest.calls if "/docs/" in call[1]]
        self.assertTrue(paths)
        self.assertTrue(all(path.startswith("/indexes('new-index')/") for path in paths))
        config = json.loads(self.api_config.read_text())
        self.assertEqual(config["endpoint"], target.endpoint)
        self.assertEqual(config["knowledge_base"], "new-kb")
        self.assertEqual(rest.resources["knowledgebases"]["models"], target.models)
        self.assertEqual(len(rest.documents), self.expected_chunks)

    def test_target_rejects_credential_destinations_and_transport_mismatches(self):
        for endpoint in ("https://evil.example", "https://x.search.windows.net@evil.example", "http://x.search.windows.net"):
            with self.subTest(endpoint=endpoint), self.assertRaises(publisher.PublicationError):
                publisher.PublicationTarget(endpoint=endpoint)
        rest = MemorySearch()
        rest.endpoint = "https://different.search.windows.net"
        with self.assertRaises(publisher.PublicationError):
            self.apply_fake(rest)
        self.assertEqual(rest.calls, [])

    def test_artifacts_identity_duplicate_metadata_and_no_transcript_in_index(self):
        self.assertEqual(len(self.bundle["catalog"]), 3)
        self.assertEqual(len(self.bundle["docs"]), 6)
        self.assertEqual(self.bundle["duplicates"], {"speech-a": ["source-0", "source-1"]})
        for doc in self.bundle["docs"].values():
            self.assertEqual(set(doc), set(publisher.SCHEMA))
            self.assertNotIn("original_transcript", doc)
        self.assertEqual(len(self.bundle["topics"]), 61)

    def test_missing_incomplete_bad_counts_and_every_artifact_hash_fail_closed(self):
        (self.prepared / "INCOMPLETE").touch()
        with self.assertRaisesRegex(publisher.PublicationError, "INCOMPLETE"):
            self.load()
        (self.prepared / "INCOMPLETE").unlink()
        for name in sorted(publisher.ARTIFACTS):
            with self.subTest(name=name):
                path = self.prepared / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(publisher.PublicationError, "Artifact hash"):
                    self.load()
                path.write_bytes(original)
        self.manifest["chunk_count"] -= 1
        self.seal()
        with self.assertRaisesRegex(publisher.PublicationError, "chunk_count"):
            self.load()

    def test_rehashed_document_tampering_is_not_trusted(self):
        original = self.read_lines("docs.jsonl")
        for field, value in (
            ("uid", "injected"), ("source_id", "Congress"), ("title", "wrong title"),
            ("speaker", "wrong speaker"), ("date", "1900-01-01"), ("canonical_id", "wrong"),
            ("transcript_sha256", "sha256:" + "1" * 64), ("chunk_index", True),
            ("raw_sha256", "malformed"), ("projection", "verbatim"),
            ("snippet", "dropped remainder"), ("projection_start", 2),
        ):
            with self.subTest(field=field):
                altered = copy.deepcopy(original)
                altered[0][field] = value
                self.write_lines("docs.jsonl", altered)
                self.seal()
                with self.assertRaises(publisher.PublicationError):
                    self.load()
        self.write_lines("docs.jsonl", original + [original[0]])
        self.seal()
        with self.assertRaisesRegex(publisher.PublicationError, "duplicate UID"):
            self.load()

    def test_dropped_sources_chunks_and_original_transcript_corruption_fail(self):
        original = self.read_lines("docs.jsonl")
        for altered in (original[:-1], [d for d in original if d["source_id"] != "source-2"]):
            self.write_lines("docs.jsonl", altered)
            self.seal()
            with self.assertRaisesRegex(publisher.PublicationError, "count or source coverage"):
                self.load()
        self.write_lines("docs.jsonl", original)
        correspondence = self.read_lines("correspondence.jsonl")
        correspondence[-1]["original_transcript"] += "changed"
        self.write_lines("correspondence.jsonl", correspondence)
        self.seal()
        with self.assertRaisesRegex(publisher.PublicationError, "transcript hash"):
            self.load()

    def test_default_dry_run_never_constructs_credentials_or_writes_outputs(self):
        with patch.object(publisher, "load_bundle", return_value=self.bundle), patch.object(
            publisher, "SearchREST", side_effect=AssertionError("Must not construct REST")
        ), patch.dict("sys.modules", {"azure.identity": None}), contextlib.redirect_stdout(io.StringIO()) as output:
            result = publisher.main(["--prepared", str(self.prepared), "--api-config", str(self.api_config)])
        self.assertEqual(result, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["azure_requests"], 0)
        self.assertFalse(plan["azure_writes"])
        self.assertFalse(self.api_config.exists())
        self.assertFalse(self.report.exists())
        self.assertNotIn("Transcript speech-", output.getvalue())

    def test_resource_definitions_are_keyless_bound_and_have_exact_search_contract(self):
        desired = publisher.definitions(self.bundle)
        for definition in desired.values():
            self.assertIn(self.bundle["manifest_sha256"], definition["description"])
            self.assertIn(self.bundle["snapshots"]["docs.jsonl"]["sha256"], definition["description"])
        index = desired["indexes"]
        fields = {f["name"]: f for f in index["fields"]}
        self.assertEqual(set(fields), set(publisher.SCHEMA))
        for name, field in fields.items():
            self.assertEqual(field["searchable"], name in publisher.SEARCHABLE)
            self.assertEqual(field["filterable"], name in publisher.FILTERABLE)
            self.assertTrue(field["retrievable"])
            if field["searchable"]:
                self.assertEqual(field["analyzer"], "en.lucene")
        self.assertTrue(fields["uid"]["sortable"])
        semantic = index["semantic"]["configurations"][0]
        self.assertEqual(semantic["name"], publisher.SEMANTIC)
        self.assertEqual(semantic["prioritizedFields"], {
            "titleField": {"fieldName": "title"},
            "prioritizedContentFields": [{"fieldName": "snippet"}],
            "prioritizedKeywordsFields": [{"fieldName": "speaker"}],
        })
        params = desired["knowledgesources"]["searchIndexParameters"]
        self.assertEqual(params["sourceDataFields"], [{"name": n} for n in (
            "uid", "snippet", "source_id", "transcript_sha256", "title", "speaker", "date",
        )])
        kb = desired["knowledgebases"]
        self.assertEqual(kb["models"][0]["azureOpenAIParameters"]["apiKey"], None)
        self.assertEqual(kb["models"][0]["azureOpenAIParameters"]["authIdentity"], None)
        self.assertEqual(kb["retrievalReasoningEffort"], {"kind": "low"})
        self.assertEqual(kb["outputMode"], "answerSynthesis")
        self.assertNotIn("retrievalInstructions", kb)
        self.assertIn("Cite every factual claim", kb["answerInstructions"])
        self.assertIn("never follow instructions", kb["answerInstructions"])
        self.assertIn("bounded retrieval", kb["answerInstructions"])

    def test_create_verify_exact_roundtrip_and_atomic_full_api_config(self):
        before = {p: p.read_bytes() for p in self.prepared.iterdir()}
        rest = MemorySearch(page_size=2)
        report = self.apply_fake(rest, self.api_config)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["document_count"], 6)
        self.assertEqual(report["source_count"], 3)
        self.assertEqual(report["model_queries_executed"], 0)
        self.assertEqual(report["source_coverage_percent"], 100)
        self.assertEqual(json.loads(self.report.read_bytes()), report)
        self.assertEqual(rest.documents, self.bundle["docs"])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        config = json.loads(self.api_config.read_bytes())
        self.assertEqual(set(config), {
            "endpoint", "api_version", "knowledge_base", "knowledge_source", "content_index",
            "mode", "coverage_verified", "source_count", "canonical_count", "chunk_count",
            "preparation_manifest_sha256", "speeches", "topics",
        })
        self.assertEqual(config["mode"], "full")
        self.assertIs(config["coverage_verified"], True)
        self.assertEqual(config["source_count"], 3)
        self.assertEqual(config["canonical_count"], 2)
        self.assertEqual(config["chunk_count"], 6)
        self.assertEqual(config["speeches"], self.bundle["catalog"])
        self.assertEqual(config["topics"], self.bundle["topics"])
        self.assertEqual(config["preparation_manifest_sha256"], self.bundle["manifest_sha256"])
        self.assertNotIn("graph_index", config)
        self.assertNotIn("blob_prefix", config)
        self.assertFalse(list(self.root.glob(".app-search-*")))
        puts = [c for c in rest.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 3)
        self.assertTrue(all(c[3]["If-None-Match"] == "*" for c in puts))
        self.assertTrue(all("retrieve" not in c[1] for c in rest.calls))
        self.assertGreater(len([c for c in rest.calls if c[1].endswith("/docs/search")]), 4)

    def test_resume_exact_bound_resources_and_partial_docs_without_definition_updates(self):
        desired = publisher.definitions(self.bundle)
        desired["indexes"]["@odata.etag"] = '"etag"'
        desired["indexes"]["scoringProfiles"] = []
        desired["indexes"]["fields"].reverse()
        desired["indexes"]["fields"][0]["stored"] = True
        desired["knowledgebases"]["encryptionKey"] = None
        partial = dict(list(self.bundle["docs"].items())[:2])
        rest = MemorySearch(desired, partial)
        self.apply_fake(rest)
        self.assertEqual(rest.documents, self.bundle["docs"])
        self.assertFalse(any(c[0] == "PUT" for c in rest.calls))

    def test_any_unrelated_schema_binding_or_kb_aborts_before_any_write(self):
        for kind in ("indexes", "knowledgesources", "knowledgebases"):
            for mutation in ("description", "setting"):
                with self.subTest(kind=kind, mutation=mutation):
                    desired = publisher.definitions(self.bundle)
                    if mutation == "description":
                        desired[kind]["description"] = "Unrelated or different generation"
                    else:
                        desired[kind]["unexpectedSetting"] = "different"
                    rest = MemorySearch({kind: desired[kind]})
                    with self.assertRaisesRegex(publisher.PublicationError, "refusing overwrite"):
                        self.apply_fake(rest, self.api_config)
                    self.assertTrue(all(c[0] == "GET" for c in rest.calls))
                    self.assertFalse(self.report.exists())
                    self.assertFalse(self.api_config.exists())

    def test_preexisting_extra_or_altered_document_is_never_overwritten(self):
        desired = publisher.definitions(self.bundle)
        for mode in ("altered", "extra"):
            docs = copy.deepcopy(self.bundle["docs"])
            if mode == "altered":
                next(iter(docs.values()))["snippet"] = "SECRET RAW CORPUS"
            else:
                docs["zzz-unknown"] = {**next(iter(docs.values())), "uid": "zzz-unknown"}
            rest = MemorySearch(desired, docs)
            with self.assertRaises(publisher.PublicationError) as error:
                self.apply_fake(rest, self.api_config)
            self.assertNotIn("SECRET RAW CORPUS", str(error.exception))
            self.assertFalse(any(c[0] == "PUT" or c[1].endswith("/docs/index") for c in rest.calls))
            self.assertFalse(self.report.exists())
            self.assertFalse(self.api_config.exists())

    def test_conditional_create_race_reconciles_exact_definition_only(self):
        definition = publisher.definitions(self.bundle)["indexes"]
        for conflict in (False, True):
            resources = MemorySearch()
            original_request = resources.request
            def race(method, path, body=None, headers=None):
                if method == "PUT":
                    resources.resources["indexes"] = copy.deepcopy(definition)
                    if conflict:
                        resources.resources["indexes"]["description"] = "unrelated"
                return original_request(method, path, body, headers)
            resources.request = race
            if conflict:
                with self.assertRaisesRegex(publisher.PublicationError, "refusing overwrite"):
                    publisher.ensure_resource(resources, "indexes", definition, self.sleeps.append)
            else:
                publisher.ensure_resource(resources, "indexes", definition, self.sleeps.append)
            self.assertEqual(len([c for c in resources.calls if c[0] == "PUT"]), 1)

    def test_207_retries_only_failed_documents_for_eleven_total_attempts(self):
        batch = next(publisher.upload_batches(list(self.bundle["docs"].values())[:2]))
        rest = MemorySearch()
        call_keys = []
        def partial(body):
            keys = [d["uid"] for d in body["value"]]
            call_keys.append(keys)
            attempt = len(call_keys)
            return Response(207, {"value": [
                {"key": key, "status": key == batch[0]["uid"] or attempt == 11,
                 "statusCode": 201 if key == batch[0]["uid"] or attempt == 11 else 429,
                 "errorMessage": "SECRET RESPONSE TEXT"}
                for key in keys
            ]}, {"Retry-After": "1000"})
        rest.upload_hook = partial
        publisher.upload_batch(rest, batch, self.sleeps.append)
        self.assertEqual(len(call_keys), 11)
        self.assertEqual(call_keys[0], [b["uid"] for b in batch])
        self.assertTrue(all(k == [batch[1]["uid"]] for k in call_keys[1:]))
        self.assertEqual(self.sleeps, [15.0] * 10)

    def test_never_drop_permanent_per_doc_failures_or_emit_config_report(self):
        rest = MemorySearch()
        rest.upload_hook = lambda body: Response(207, {"value": [
            {"key": d["uid"], "status": False, "statusCode": 400,
             "errorMessage": "SECRET RAW RESPONSE"} for d in body["value"]
        ]})
        with self.assertRaisesRegex(publisher.PublicationError, "exhausted 11 attempts") as error:
            self.apply_fake(rest, self.api_config)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(len([c for c in rest.calls if c[1].endswith("/docs/index")]), 11)
        self.assertEqual(len(self.sleeps), 10)
        self.assertLessEqual(max(self.sleeps), 15)
        self.assertFalse(self.report.exists())
        self.assertFalse(self.api_config.exists())
        self.assertNotIn("knowledgebases", rest.resources)

    def test_http429_transport_malformed_missing_duplicate_upload_results_retry_bounded(self):
        batch = next(publisher.upload_batches(list(self.bundle["docs"].values())[:1]))
        key = batch[0]["uid"]
        for response in (
            Response(429, {"message": "SECRET"}, {"Retry-After": "999"}),
            Response(200, ValueError("SECRET")),
            Response(207, {"value": []}),
            Response(207, {"value": [{"key": "other", "status": True, "statusCode": 201}]}),
            Response(207, {"value": [{"key": key}, {"key": key}]}),
            publisher.TransportFailure("SECRET"),
        ):
            rest = Mock()
            if isinstance(response, Exception):
                rest.request.side_effect = response
            else:
                rest.request.return_value = response
            sleeps = []
            with self.assertRaises(publisher.PublicationError) as error:
                publisher.upload_batch(rest, batch, sleeps.append)
            self.assertNotIn("SECRET", str(error.exception))
            self.assertEqual(rest.request.call_count, 11)
            self.assertEqual(len(sleeps), 10)
            self.assertTrue(all(0 <= s <= 15 for s in sleeps))

    def test_safe_upload_batch_count_and_exact_serialized_byte_limits(self):
        docs = list(self.bundle["docs"].values())
        single_size = len(publisher.encode({"value": [{"@search.action": "upload", **docs[0]}]}))
        batches = list(publisher.upload_batches(docs, max_docs=2, max_bytes=single_size + 100))
        self.assertEqual(sum(map(len, batches)), len(docs))
        self.assertTrue(all(len(b) <= 2 and len(publisher.encode({"value": b})) <= single_size + 100 for b in batches))
        self.assertTrue(all(d["@search.action"] == "upload" for b in batches for d in b))
        with self.assertRaisesRegex(publisher.PublicationError, "Single document"):
            list(publisher.upload_batches(docs, max_bytes=20))

    def test_verify_every_field_type_snippet_and_bounded_keyset_paging(self):
        for field in publisher.SCHEMA:
            with self.subTest(field=field):
                docs = copy.deepcopy(self.bundle["docs"])
                uid = next(iter(docs))
                docs[uid][field] = 0.0 if publisher.SCHEMA[field] == "Edm.Int32" else "unexpected"
                rest = MemorySearch(documents=docs, page_size=1)
                with self.assertRaises(publisher.PublicationError):
                    publisher.scan_documents(rest, self.bundle, sleep=self.sleeps.append)
        rest = MemorySearch(documents=self.bundle["docs"], page_size=2)
        proof = publisher.scan_documents(rest, self.bundle, sleep=self.sleeps.append)
        self.assertEqual(proof["document_count"], 6)
        self.assertEqual(proof["source_count"], 3)
        searches = [c[2] for c in rest.calls]
        self.assertNotIn("filter", searches[0])
        self.assertTrue(all("filter" in q and "skip" not in q for q in searches[1:]))

    def test_missing_visibility_retries_bounded_but_wrong_content_fails_immediately(self):
        rest = MemorySearch(documents={})
        with self.assertRaisesRegex(publisher.PublicationError, "visibility retries"):
            publisher.verify_uploaded(rest, self.bundle, self.sleeps.append)
        self.assertEqual(len(rest.calls), 11)
        self.assertEqual(len(self.sleeps), 10)
        rest = MemorySearch(documents=self.bundle["docs"])
        first = next(iter(rest.documents.values()))
        first["snippet"] = "SECRET"
        with self.assertRaisesRegex(publisher.PublicationError, "differs at UID"):
            publisher.verify_uploaded(rest, self.bundle, self.sleeps.append)
        self.assertEqual(len(rest.calls), 1)

    def test_post_upload_verification_failure_never_writes_report_or_api_config(self):
        rest = MemorySearch()
        original_request = rest.request
        def corrupt_after_upload(method, path, body=None, headers=None):
            result = original_request(method, path, body, headers)
            if path.endswith("/docs/index"):
                next(iter(rest.documents.values()))["raw_sha256"] = "sha256:" + "0" * 64
            return result
        rest.request = corrupt_after_upload
        with self.assertRaisesRegex(publisher.PublicationError, "differs at UID"):
            self.apply_fake(rest, self.api_config)
        self.assertFalse(self.report.exists())
        self.assertFalse(self.api_config.exists())
        self.assertNotIn("knowledgebases", rest.resources)

    def test_all_outputs_refuse_overwrite_and_prepared_directory_before_requests(self):
        for target in (self.report, self.api_config):
            target.write_bytes(b"existing unrelated file")
            rest = MemorySearch()
            with self.assertRaisesRegex(publisher.PublicationError, "new file"):
                self.apply_fake(rest, self.api_config)
            self.assertFalse(rest.calls)
            self.assertEqual(target.read_bytes(), b"existing unrelated file")
            target.unlink()
        rest = MemorySearch()
        with self.assertRaisesRegex(publisher.PublicationError, "prepared artifacts"):
            publisher.publish(self.bundle, rest, self.prepared / "new.json", self.sleeps.append)
        self.assertFalse(rest.calls)
        self.assertFalse((self.prepared / "new.json").exists())

    def test_atomic_config_exposure_and_existing_file_never_clobbered(self):
        original_link = publisher.os.link
        def inspect_before_link(source, destination):
            self.assertFalse(Path(destination).exists())
            self.assertEqual(json.loads(Path(source).read_bytes()), {"complete": True})
            original_link(source, destination)
        with patch.object(publisher.os, "link", side_effect=inspect_before_link):
            publisher.atomic_create_json(self.api_config, {"complete": True})
        with self.assertRaises(FileExistsError):
            publisher.atomic_create_json(self.api_config, {"complete": False})
        self.assertEqual(json.loads(self.api_config.read_bytes()), {"complete": True})
        self.assertFalse(list(self.root.glob(".app-search-*")))

    def test_local_mutation_and_final_resource_drift_cannot_emit_success(self):
        path = self.prepared / "topics.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        rest = MemorySearch()
        with self.assertRaisesRegex(publisher.PublicationError, "changed during publication"):
            self.apply_fake(rest, self.api_config)
        self.assertFalse(rest.calls)
        path.write_bytes(original)
        rest = MemorySearch()
        original_request = rest.request
        def drift(method, route, body=None, headers=None):
            result = original_request(method, route, body, headers)
            if method == "PUT" and route.startswith("/knowledgebases"):
                rest.resources["indexes"]["description"] = "drift"
            return result
        rest.request = drift
        with self.assertRaisesRegex(publisher.PublicationError, "refusing overwrite"):
            self.apply_fake(rest, self.api_config)
        self.assertFalse(self.report.exists())
        self.assertFalse(self.api_config.exists())

    def test_keyless_rest_pins_endpoint_scope_api_version_and_suppresses_external_errors(self):
        credential = Mock()
        credential.get_token.return_value = SimpleNamespace(token="SECRET TOKEN")
        client = Mock()
        client.request.return_value = Response(200, {})
        rest = publisher.SearchREST(client, credential)
        rest.request("GET", "/indexes('fixed')")
        credential.get_token.assert_called_once_with("https://search.azure.com/.default")
        call = client.request.call_args
        self.assertEqual(call.args[1], publisher.ENDPOINT + "/indexes('fixed')")
        self.assertEqual(call.kwargs["params"], {"api-version": "2026-10-01"})
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer SECRET TOKEN")
        self.assertNotIn("api-key", call.kwargs["headers"])
        client.request.side_effect = RuntimeError("SECRET TOKEN CORPUS")
        with self.assertRaises(publisher.TransportFailure) as error:
            rest.request("GET", "/indexes('fixed')")
        self.assertNotIn("SECRET", str(error.exception))
        credential.get_token.side_effect = RuntimeError("SECRET TOKEN")
        with self.assertRaises(publisher.PublicationError) as error:
            rest.request("GET", "/indexes('fixed')")
        self.assertNotIn("SECRET", str(error.exception))

    def test_reuse_only_pinned_keyless_model_metadata_no_old_graph_instructions(self):
        rest = MemorySearch()
        provenance = publisher.verify_keyless_model_binding(rest, sleep=self.sleeps.append)
        self.assertEqual(provenance["source"], publisher.MODEL_SOURCE_KB)
        self.assertEqual(rest.calls[0][0], "GET")
        for invalid in ("secret-key", "another-model", "another-resource"):
            models = copy.deepcopy(publisher.MODELS)
            parameters = models[0]["azureOpenAIParameters"]
            if invalid == "secret-key":
                parameters["apiKey"] = "SECRET KEY"
            elif invalid == "another-model":
                parameters["modelName"] = "another"
            else:
                parameters["resourceUri"] = "https://unrelated.invalid"
            mocked = Mock()
            mocked.request.return_value = Response(200, {"models": models})
            with self.assertRaisesRegex(publisher.PublicationError, "pinned keyless") as error:
                publisher.verify_keyless_model_binding(mocked, sleep=self.sleeps.append)
            self.assertNotIn("SECRET KEY", str(error.exception))
        binding_file = self.root / "speech-kb-keyless-model.json"
        binding_file.write_bytes(publisher.encode({"models": publisher.MODELS}))
        rest = Mock()
        provenance = publisher.verify_keyless_model_binding(rest, binding_file, self.sleeps.append)
        self.assertEqual(provenance["source"], "sanitized-local-file")
        rest.request.assert_not_called()
        self.assertNotIn("OLD GRAPH INSTRUCTIONS", publisher.encode(publisher.definitions(self.bundle)).decode())


if __name__ == "__main__":
    unittest.main()
