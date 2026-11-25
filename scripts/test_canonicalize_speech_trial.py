import json
import tempfile
import unittest
from pathlib import Path

from canonicalize_speech_trial import (
    Selection, alias_index, classify, digest, literal_support, prototype,
)


class CanonicalizationTests(unittest.TestCase):
    def setUp(self):
        self.entities, self.aliases = alias_index([
            {"id": "speech-one", "name": "First Address", "type": "Speech", "aliases": ""},
            {"id": "person-one", "name": "One Person", "type": "Person", "aliases": ""},
            {"id": "topic-one", "name": "employment", "type": "Topic", "aliases": "jobs"},
        ])
        self.source = {"speech_id": "speech-one", "metadata": {"president": "One Person"}}
        self.relations = {"addressesTopic": ("Speech", "Topic"),
                          "delivered": ("Person", "Speech")}
        self.edge = {
            "predicate": "addressesTopic", "subject": "First Address",
            "subject_type": "Speech", "subject_id": "speech-one",
            "object": "jobs", "object_type": "Topic", "object_id": "topic-one",
        }

    def check_edge(self, **changes):
        return classify({**self.edge, **changes}, self.source, self.entities,
                        self.aliases, self.relations, "We need more jobs.")[0:2]

    def test_corrupt_classes_and_wrong_roots_are_not_repaired(self):
        self.assertEqual(self.check_edge(object_id="speech-one"),
                         ("rejected", "cross_class_identity"))
        self.assertEqual(self.check_edge(subject_id="speech-other"),
                         ("rejected", "wrong_source_speech_identity"))
        self.assertEqual(self.check_edge(predicate="addressesPlace"),
                         ("rejected", "undeclared_predicate"))

    def test_exact_alias_and_literal_occurrence_do_not_prove_semantics(self):
        self.assertEqual(self.check_edge(),
                         ("unresolved", "literal_support_requires_semantic_adjudication"))
        self.assertEqual(self.check_edge(object="joblessness"),
                         ("unresolved", "no_explicit_class_scoped_alias"))
        self.assertEqual(self.check_edge(object="employment"),
                         ("unresolved", "no_literal_transcript_support"))
        self.assertFalse(literal_support("war", "We reward workers."))
        self.assertTrue(literal_support("jobs", "More <b>jobs</b>."))

    def test_ambiguous_alias_is_quarantined(self):
        self.aliases[("Topic", "jobs")].add("topic-other")
        self.assertEqual(self.check_edge(), ("unresolved", "ambiguous_explicit_alias"))

    def test_only_corroborated_extracted_metadata_is_accepted(self):
        edge = {**self.edge, "predicate": "delivered", "subject": "One Person",
                "subject_type": "Person", "subject_id": "person-one",
                "object": "First Address", "object_type": "Speech",
                "object_id": "speech-one"}
        self.assertEqual(classify(edge, self.source, self.entities, self.aliases,
                                 self.relations, "")[:2],
                         ("accepted", "extractor_metadata_matches_verified_ledger"))
        self.source["metadata"]["president"] = "Somebody Else"
        self.assertEqual(classify(edge, self.source, self.entities, self.aliases,
                                 self.relations, "")[:2],
                         ("rejected", "source_metadata_disagreement"))

    def test_provenance_tampering_fails_and_ledger_assertions_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            raw_root, derived_root = root / "raw", root / "derived"
            raw_root.mkdir()
            derived_root.mkdir()
            raw_path, derived_path = raw_root / "one.json", derived_root / "one.md"
            raw = json.dumps({"transcript": "More jobs."}).encode()
            derived = b"Header\nMore jobs."
            raw_path.write_bytes(raw)
            derived_path.write_bytes(derived)
            source = {
                **self.source, "source_id": "original-one", "filename": "one.md",
                "raw_source_uri": raw_path.as_uri(), "raw_source_hash": digest(raw),
                "derived_hash": digest(derived), "transcript_hash": digest(b"More jobs."),
                "metadata": {**self.source["metadata"], "date": "2000-01-01",
                             "title": "First Address"},
                "segments": [{}, {"derived_start": 7, "length": 10}],
            }
            selection = Selection(["source-one"])
            selection.staging_prefix = "trial/graph"
            selection.container = "speeches"
            selection.storage_host = "storage.blob.core.windows.net"
            selection.graph_name = "graph"
            registry = list(self.entities.values()) + [{
                "id": "date-one", "type": "SpeechDate",
                "name": "2000-01-01", "aliases": "",
            }]
            edge = {**self.edge, "id": "edge-one", "chunk_id": "chunk-one",
                    "parent_id": "parent-one", "graph_name": "graph"}
            inputs = {
                "provenance": {
                    "status": "complete", "items": [source],
                    "source_reference": str(raw_root), "derived_reference": str(derived_root),
                },
                "manifest": {"items": [{
                    "source_id": "source-one", "original_uri": derived_path.as_uri(),
                    "version_or_hash": digest(derived),
                }]},
                "registry": registry,
                "ontology": {"@graph": [{
                    "@id": "ex:addressesTopic", "@type": "owl:ObjectProperty",
                    "rdfs:domain": {"@id": "ex:Speech"},
                    "rdfs:range": {"@id": "ex:Topic"},
                }]},
                "content": [{
                    "uid": "chunk-one", "snippet_parent_id": "parent-one",
                    "blob_url": "https://storage.blob.core.windows.net/"
                                "speeches/trial/graph/source-one.md",
                    "snippet": "More jobs.",
                }],
                "edges": [edge], "selection": selection,
            }
            result = prototype(**inputs)
            self.assertEqual(len(result["unresolved"]), 1)
            self.assertEqual(result["accepted"], [])
            self.assertEqual(len(result["ledger_derived"]), 2)
            self.assertEqual(result["unresolved"][0]["original_edge"], edge)
            self.assertEqual(result["unresolved"][0]["provenance"]
                             ["chunk_to_derived_text_correspondence"], "unverified")
            inputs["content"][0]["blob_url"] = (
                "https://storage.blob.core.windows.net/"
                "speeches/trial/graph/nested/source-one.md"
            )
            with self.assertRaisesRegex(ValueError, "Unexpected chunk source"):
                prototype(**inputs)
            inputs["content"][0]["blob_url"] = (
                "https://storage.blob.core.windows.net/"
                "speeches/trial/graph/source-one.md"
            )
            edge["parent_id"] = "different-parent"
            with self.assertRaisesRegex(ValueError, "Invalid graph provenance"):
                prototype(**inputs)
            edge["parent_id"] = "parent-one"
            raw_path.write_bytes(raw + b" ")
            with self.assertRaisesRegex(ValueError, "Source hash mismatch"):
                prototype(**inputs)


if __name__ == "__main__":
    unittest.main()
