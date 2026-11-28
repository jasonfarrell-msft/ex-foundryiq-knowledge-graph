import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from azure.identity import CredentialUnavailableError

import build_markdown_graph as bmg


SAMPLE_ONTOLOGY = {
    "@context": {
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "ex": "https://schema.search.azure.com/entities#",
    },
    "@graph": [
        {"@id": "ex:Document", "@type": "owl:Class", "rdfs:comment": "A source document."},
        {"@id": "ex:Person", "@type": "owl:Class", "rdfs:comment": "A named person."},
        {"@id": "ex:Topic", "@type": "owl:Class", "rdfs:comment": "A policy or technical topic."},
        {"@id": "ex:Organization", "@type": "owl:Class", "rdfs:comment": "An organization."},
        {
            "@id": "ex:authoredBy",
            "@type": "owl:ObjectProperty",
            "rdfs:domain": {"@id": "ex:Document"},
            "rdfs:range": {"@id": "ex:Person"},
            "rdfs:comment": "Document author.",
        },
        {
            "@id": "ex:addressesTopic",
            "@type": "owl:ObjectProperty",
            "rdfs:domain": {"@id": "ex:Document"},
            "rdfs:range": {"@id": "ex:Topic"},
            "rdfs:comment": "Document discusses this topic.",
        },
        {
            "@id": "ex:mentionsOrganization",
            "@type": "owl:ObjectProperty",
            "rdfs:domain": {"@id": "ex:Document"},
            "rdfs:range": {"@id": "ex:Organization"},
            "rdfs:comment": "Document mentions an organization.",
        },
    ],
}


class FakeCredential:
    def get_token(self, scope):
        return MagicMock(token="fake-entra-token", expires_on=9999999999)


class BuildMarkdownGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "alpha.md").write_text(
            "---\ntitle: Alpha Architecture\nauthor: Ada Lovelace\n---\n\n"
            "Contoso Labs builds resilient knowledge graphs for enterprise search.\n",
            encoding="utf-8",
        )
        (self.docs / "beta.md").write_text(
            "# Beta Brief\n\n**Author**: Grace Hopper\nDate: 2026-01-15\n\n"
            "**Transcript**\n\nThis note covers Graph Search and Contoso Labs.\n",
            encoding="utf-8",
        )
        self.ontology_path = self.root / "ontology.json"
        self.ontology_path.write_text(json.dumps(SAMPLE_ONTOLOGY), encoding="utf-8")
        self.out = self.root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_performs_no_writes(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bmg.main([
                "--input-dir", str(self.docs),
                "--ontology", str(self.ontology_path),
                "--output-dir", str(self.out),
                "--prefix", "custdocs",
            ])
        self.assertEqual(rc, 0)
        self.assertFalse(self.out.exists())
        plan = json.JSONDecoder().raw_decode(buf.getvalue())[0]
        self.assertEqual(plan["mode"], "offline-dry-run")
        self.assertEqual(plan["markdown_files_discovered"], 2)

    def test_prepare_writes_chunks_and_metadata_edges(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bmg.main([
                "--input-dir", str(self.docs),
                "--ontology", str(self.ontology_path),
                "--output-dir", str(self.out),
                "--prefix", "custdocs",
                "--prepare",
            ])
        self.assertEqual(rc, 0)
        chunks = [json.loads(l) for l in (self.out / "chunks.jsonl").read_text().splitlines()]
        edges = [json.loads(l) for l in (self.out / "edges.jsonl").read_text().splitlines()]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(edges), 2)
        self.assertEqual({e["predicate"] for e in edges}, {"authoredBy"})
        self.assertEqual({e["object"] for e in edges}, {"Ada Lovelace", "Grace Hopper"})

    def test_rejects_output_inside_input(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = bmg.main([
                "--input-dir", str(self.docs),
                "--ontology", str(self.ontology_path),
                "--output-dir", str(self.docs / "nested"),
                "--prefix", "custdocs",
                "--prepare",
            ])
        self.assertEqual(rc, 1)

    def test_class_scoped_ids_prevent_cross_class_collisions(self):
        ontology = bmg.load_ontology(self.ontology_path)
        doc_ctx = {"doc_id": "document-1234", "doc_class": "Document", "title": "Washington Note", "source_path": "w.md"}

        # Same label ("Washington") as Person vs Organization must stay distinct IDs.
        person_id, _ = bmg.resolve_entity_id("Washington", "Person", doc_ctx)
        org_id, _ = bmg.resolve_entity_id("Washington", "Organization", doc_ctx)
        self.assertNotEqual(person_id, org_id)
        self.assertTrue(person_id.startswith("person-"))
        self.assertTrue(org_id.startswith("organization-"))

        # Undeclared predicate or domain/range mismatch must be rejected.
        bad_pred, reason1 = bmg.validate_and_build_edge(
            {"subject": "Washington Note", "subject_type": "Document", "predicate": "locatedIn", "object": "Seattle", "object_type": "Organization"},
            ontology=ontology, doc_context=doc_ctx, chunk_uid="document-1234-000000", graph_name="custdocs",
        )
        self.assertIsNone(bad_pred)
        self.assertEqual(reason1, "undeclared_predicate")

        bad_range, reason2 = bmg.validate_and_build_edge(
            {"subject": "Washington Note", "subject_type": "Document", "predicate": "mentionsOrganization", "object": "Seattle", "object_type": "Person"},
            ontology=ontology, doc_context=doc_ctx, chunk_uid="document-1234-000000", graph_name="custdocs",
        )
        self.assertIsNone(bad_range)
        self.assertEqual(reason2, "ontology_domain_or_range_mismatch")

    def test_extract_caches_and_quarantines_invalid_triples(self):
        manifest, ontology, registry = bmg.prepare_corpus(
            input_dir=self.docs, ontology_path=self.ontology_path,
            output_dir=self.out, prefix="custdocs",
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "triples": [
                            {
                                "subject": "Alpha Architecture", "subject_type": "Document",
                                "predicate": "mentionsOrganization",
                                "object": "Contoso Labs", "object_type": "Organization",
                                "confidence": 0.95,
                            },
                            {
                                "subject": "Alpha Architecture", "subject_type": "Document",
                                "predicate": "addressesTopic",
                                "object": "Contoso Labs", "object_type": "Organization",
                                "confidence": 0.6,
                            },
                        ]
                    })
                }
            }]
        }
        client = MagicMock()
        client.post.return_value = fake_resp

        summary = bmg.extract_edges(
            output_dir=self.out, ontology=ontology,
            foundry_endpoint="https://example-foundry.cognitiveservices.azure.com",
            chat_deployment="chat", prefix="custdocs", doc_class=manifest["doc_class"],
            registry=registry, max_chunks=1, credential=FakeCredential(), client=client,
        )
        self.assertEqual(summary["chunks_processed"], 1)
        self.assertEqual(summary["accepted_edges"], 3)  # 2 deterministic + 1 valid extracted
        self.assertEqual(summary["rejected_edges"], 1)  # domain/range mismatch quarantined

        # Verify rejected_edges.jsonl and edges.jsonl on disk.
        rejected = [json.loads(l) for l in (self.out / "rejected_edges.jsonl").read_text().splitlines()]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "ontology_domain_or_range_mismatch")
        disk_edges = [json.loads(l) for l in (self.out / "edges.jsonl").read_text().splitlines()]
        self.assertEqual(len(disk_edges), 3)

        # Second run reuses extraction_cache.jsonl and makes zero extra LLM calls.
        client.post.reset_mock()
        summary2 = bmg.extract_edges(
            output_dir=self.out, ontology=ontology,
            foundry_endpoint="https://example-foundry.cognitiveservices.azure.com",
            chat_deployment="chat", prefix="custdocs", doc_class=manifest["doc_class"],
            registry=registry, max_chunks=1, credential=FakeCredential(), client=client,
        )
        client.post.assert_not_called()
        self.assertEqual(summary2["accepted_edges"], 3)

        # Modifying alpha.md invalidates its cache entry even though chunk_uid is unchanged.
        (self.docs / "alpha.md").write_text(
            "---\ntitle: Alpha Architecture\nauthor: Ada Lovelace\n---\n\n"
            "Fabrikam Research now leads graph extraction.\n",
            encoding="utf-8",
        )
        bmg.prepare_corpus(
            input_dir=self.docs, ontology_path=self.ontology_path,
            output_dir=self.out, prefix="custdocs",
        )
        summary3 = bmg.extract_edges(
            output_dir=self.out, ontology=ontology,
            foundry_endpoint="https://example-foundry.cognitiveservices.azure.com",
            chat_deployment="chat", prefix="custdocs", doc_class=manifest["doc_class"],
            registry=registry, max_chunks=1, credential=FakeCredential(), client=client,
        )
        self.assertEqual(client.post.call_count, 1)
        self.assertEqual(summary3["accepted_edges"], 3)

    def test_deduplicates_identical_fact_per_chunk(self):
        manifest, ontology, registry = bmg.prepare_corpus(
            input_dir=self.docs, ontology_path=self.ontology_path,
            output_dir=self.out, prefix="custdocs",
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "triples": [
                            {"subject": "Alpha Architecture", "subject_type": "Document", "predicate": "authoredBy", "object": "Ada Lovelace", "object_type": "Person", "confidence": 0.9},
                            {"subject": "Alpha Architecture", "subject_type": "Document", "predicate": "mentionsOrganization", "object": "Contoso Labs", "object_type": "Organization", "confidence": 0.9},
                            {"subject": "Alpha Architecture", "subject_type": "Document", "predicate": "mentionsOrganization", "object": "Contoso Labs", "object_type": "Organization", "confidence": 0.8},
                        ]
                    })
                }
            }]
        }
        client = MagicMock()
        client.post.return_value = fake_resp
        summary = bmg.extract_edges(
            output_dir=self.out, ontology=ontology,
            foundry_endpoint="https://example-foundry.services.ai.azure.com",
            chat_deployment="chat", prefix="custdocs", doc_class=manifest["doc_class"],
            registry=registry, max_chunks=1, credential=FakeCredential(), client=client,
        )
        self.assertEqual(summary["accepted_edges"], 3)  # deterministic authoredBy + 1 Contoso Labs

    def test_publish_creates_graph_profile_and_retries_partial_207(self):
        bmg.prepare_corpus(
            input_dir=self.docs, ontology_path=self.ontology_path,
            output_dir=self.out, prefix="custdocs",
        )
        edges_path = self.out / "edges.jsonl"
        existing = edges_path.read_text(encoding="utf-8").splitlines()
        extra = json.loads(existing[0])
        extra["id"] = "extracted-edge-1"
        edges_path.write_text("\n".join([*existing, json.dumps(extra)]) + "\n", encoding="utf-8")

        chunk_attempts = {"count": 0}

        def fake_request(method, url, params=None, headers=None, json=None):
            resp = MagicMock()
            if method == "PUT":
                resp.status_code = 201
            elif "custdocs-chunks" in url and url.endswith("/docs/index"):
                chunk_attempts["count"] += 1
                if chunk_attempts["count"] == 1:
                    resp.status_code = 207
                    resp.json.return_value = {
                        "value": [
                            {"key": json["value"][0]["uid"], "status": True},
                            {"key": json["value"][1]["uid"], "status": False},
                        ]
                    }
                else:
                    resp.status_code = 200
                    resp.json.return_value = {"value": [{"key": d["uid"], "status": True} for d in json["value"]]}
            elif url.endswith("/docs/index"):
                resp.status_code = 200
                resp.json.return_value = {"value": [{"key": d["id"], "status": True} for d in json["value"]]}
            elif url.endswith("/docs/search.graph"):
                resp.status_code = 200
                resp.json.return_value = {"value": [{"subject": "Alpha"}], "@search.graphProvenanceDocuments": {"doc-1": {}}}
            return resp

        mock_client = MagicMock()
        mock_client.request.side_effect = fake_request

        with patch("azure.identity.AzureCliCredential", return_value=FakeCredential()), \
             patch("httpx.Client", return_value=mock_client) as client_cls, \
             patch.object(bmg.time, "sleep"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bmg.main([
                    "--input-dir", str(self.docs),
                    "--ontology", str(self.ontology_path),
                    "--output-dir", str(self.out),
                    "--prefix", "custdocs",
                    "--apply", "--verify-gql",
                    "--search-endpoint", "https://example-search.search.windows.net",
                ])
        self.assertEqual(rc, 0)
        self.assertEqual(chunk_attempts["count"], 2)
        self.assertFalse(client_cls.call_args.kwargs["follow_redirects"])

        decoder = json.JSONDecoder()
        raw_out = buf.getvalue()
        _, idx = decoder.raw_decode(raw_out.lstrip())
        out_json, _ = decoder.raw_decode(raw_out[idx:].lstrip())
        self.assertTrue(out_json["prepare"]["reused_existing"])
        self.assertEqual(out_json["publish"]["edges_uploaded"], 3)
        self.assertEqual(out_json["publish"]["gql_verification"]["hydrated_docs"], 1)

        put_calls = [c for c in mock_client.request.call_args_list if c.args[0] == "PUT"]
        chunk_put = [c for c in put_calls if "custdocs-chunks" in c.args[1]][0]
        graph_put = [c for c in put_calls if "custdocs-graph" in c.args[1]][0]
        self.assertEqual(chunk_put.kwargs["params"]["api-version"], "2026-11-01-preview")
        self.assertEqual(chunk_put.kwargs["headers"]["Authorization"], "Bearer fake-entra-token")
        self.assertIn("snippet", [f["name"] for f in chunk_put.kwargs["json"]["fields"]])
        self.assertEqual(graph_put.kwargs["json"]["graphProfile"]["provenanceIndexName"], "custdocs-chunks")
        self.assertEqual(graph_put.kwargs["json"]["graphProfile"]["fields"]["sourceChunkIdField"], "chunk_id")

    def test_markdown_parser_handles_frontmatter_h1_and_urls(self):
        md = "---\ntitle: Override Title\n---\n# H1 Heading\n**Author**: Ada Lovelace\n\nhttps://example.com/path\n\nBody text."
        title, meta, body = bmg.parse_markdown_document(md, "doc.md")
        self.assertEqual(title, "Override Title")
        self.assertEqual(meta["author"], "Ada Lovelace")
        self.assertNotIn("https", meta)
        self.assertIn("https://example.com/path", body)

    def test_chunk_text_overlap_and_bounds(self):
        text = "word " * 40
        chunks = list(bmg.chunk_text(text, chunk_size=60, overlap=15))
        self.assertGreater(len(chunks), 2)
        self.assertEqual(chunks[0][0], 0)
        self.assertLess(chunks[1][0], chunks[0][1])

    def test_entity_registry_csv_json_and_ambiguity(self):
        csv_path = self.root / "entities.csv"
        csv_path.write_text(
            "id,name,type,aliases\n"
            "org-1,Contoso Labs,Organization,Contoso|CL\n"
            "org-2,Contoso Energy,Organization,Contoso\n",
            encoding="utf-8",
        )
        reg = bmg.load_entity_registry(csv_path)
        self.assertEqual(bmg.resolve_entity_id("CL", "Organization", None, reg)[0], "org-1")
        ambig_id, _ = bmg.resolve_entity_id("Contoso", "Organization", None, reg)
        self.assertTrue(ambig_id.startswith("organization-ambig-"))

        json_path = self.root / "topics.json"
        json_path.write_text(json.dumps({"topics": [{"name": "AI Safety", "aliases": ["safety"]}]}), encoding="utf-8")
        treg = bmg.load_entity_registry(json_path)
        self.assertEqual(bmg.resolve_entity_id("safety", "Topic", None, treg)[1], "AI Safety")

    def test_transport_and_credential_errors_are_sanitized(self):
        _, ontology, registry = bmg.prepare_corpus(
            input_dir=self.docs, ontology_path=self.ontology_path,
            output_dir=self.out, prefix="custdocs",
        )
        bad_cred = MagicMock()
        bad_cred.get_token.side_effect = CredentialUnavailableError(message="secret-cli-detail")
        with self.assertRaisesRegex(bmg.GraphBuildError, "Azure CLI authentication failed") as ctx:
            bmg.extract_edges(
                output_dir=self.out, ontology=ontology,
                foundry_endpoint="https://example-foundry.cognitiveservices.azure.com",
                chat_deployment="chat", prefix="custdocs", doc_class="Document",
                registry=registry, credential=bad_cred, client=MagicMock(),
            )
        self.assertNotIn("secret-cli-detail", str(ctx.exception))

        timeout_client = MagicMock()
        timeout_client.post.side_effect = httpx.ReadTimeout("secret-timeout-detail")
        with patch.object(bmg.time, "sleep"), \
             self.assertRaisesRegex(bmg.GraphBuildError, "transport failed") as ctx2:
            bmg.extract_edges(
                output_dir=self.out, ontology=ontology,
                foundry_endpoint="https://example-foundry.cognitiveservices.azure.com",
                chat_deployment="chat", prefix="custdocs", doc_class="Document",
                registry=registry, credential=FakeCredential(), client=timeout_client,
            )
        self.assertEqual(timeout_client.post.call_count, 8)
        self.assertNotIn("secret-timeout-detail", str(ctx2.exception))

    def test_endpoint_and_symlink_validation(self):
        for bad in (
            "http://example.search.windows.net",
            "https://user:pass@example.search.windows.net",
            "https://example.search.windows.net:8443",
            "https://example.search.windows.net/extra",
            "https://example.search.windows.net.evil.com",
        ):
            with self.assertRaises(bmg.GraphBuildError):
                bmg.validate_azure_endpoint(bad, (".search.windows.net",), "Search")

        link_dir = self.root / "linked"
        link_dir.symlink_to(self.docs)
        with self.assertRaises(bmg.GraphBuildError):
            bmg.safe_path(link_dir / "alpha.md")
        with self.assertRaises(bmg.GraphBuildError):
            bmg.safe_path(self.docs / ".." / "ontology.json")

    def test_main_flag_validation_errors(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bmg.main([
                "--input-dir", str(self.docs), "--ontology", str(self.ontology_path),
                "--output-dir", str(self.out), "--prefix", "custdocs", "--extract",
            ]), 1)
            self.assertEqual(bmg.main([
                "--input-dir", str(self.docs), "--ontology", str(self.ontology_path),
                "--output-dir", str(self.out), "--prefix", "custdocs", "--apply",
            ]), 1)


if __name__ == "__main__":
    unittest.main()
