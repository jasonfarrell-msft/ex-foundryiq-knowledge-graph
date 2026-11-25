import json
import tempfile
import unittest
from pathlib import Path

from prepare_app_search_corpus import (
    KEY, OUTPUT, ROOT, RUN, chunks, prepare, project, verify_documents,
)
from prepare_full_speech_corpus import prepare as prepare_originals
from prepare_speech_graph_corpus import digest
from prepare_speech_grounding import write_registry


class SearchCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / RUN
        (self.run / "entity").mkdir(parents=True)
        (self.run / "ontology").mkdir()
        self.source = self.root / "speeches"
        self.source.mkdir()
        self.output = self.root / OUTPUT
        self.ledger_path = self.run / "entity/full-corpus-v7-provenance.json"
        self.manifest_path = self.run / "entity/full-corpus-v7-manifest.json"
        self.transcript = "<p>Exact &amp; readable <em>words</em>.</p>\r\n" + (
            "Unicode 😀 café &lt;literal&gt; " * 390
        ) + "END-TAIL-😀"
        for name, title in (
            ("first.json", "July 16,2026: Address "),
            ("duplicate.json", "July 16, 2026: Address"),
            ("third.json", "Another speech"),
        ):
            (self.source / name).write_bytes(json.dumps({
                "title": title, "president": "Donald Trump",
                "date": "2026-07-16T21:00:00-04:00", "transcript": self.transcript,
            }, ensure_ascii=False).encode("utf-8"))
        self.ledger = prepare_originals(
            self.source, self.run / "corpus-full-v7", self.ledger_path, 3, 2
        )
        self.manifest_path.write_text(json.dumps({"manifest": {
            "source": {"kind": "local_path", "reference": self.ledger["derived_reference"]},
            "items": [
                {"original_uri": i["derived_uri"], "version_or_hash": i["derived_hash"]}
                for i in self.ledger["items"]
            ],
        }}), encoding="utf-8")
        # Use the actual curated vocabulary, not graph/trial observations.
        lexicon_path = self.run / "ontology/topic-lexicon-v7.json"
        lexicon_path.write_bytes((ROOT / RUN / "ontology/topic-lexicon-v7.json").read_bytes())
        write_registry(
            self.ledger, json.loads(lexicon_path.read_bytes()),
            self.run / "entity/grounding-entities-v7.csv",
        )

    def run_prepare(self):
        return prepare(self.root, self.output, expected_sources=3, expected_canonicals=2)

    def test_all_counts_preservation_duplicates_and_chunk_integrity(self):
        inputs = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report = self.run_prepare()
        self.assertEqual(report["source_count"], 3)
        self.assertEqual(report["canonical_speech_count"], 2)
        self.assertEqual(report["source_coverage_percent"], 100)
        self.assertTrue(report["input_hashes_unchanged"])
        self.assertFalse((self.output / "INCOMPLETE").exists())
        self.assertEqual({p: p.read_bytes() for p in inputs}, inputs)
        catalog = json.loads((self.output / "catalog.json").read_bytes())
        self.assertEqual(len(catalog), 3)
        self.assertEqual(len({c["id"] for c in catalog}), 3)
        duplicates = list(report["duplicate_source_groups"].values())
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(len(duplicates[0]), 2)
        records = [json.loads(line) for line in (self.output / "correspondence.jsonl").read_bytes().splitlines()]
        docs = [json.loads(line) for line in (self.output / "docs.jsonl").read_bytes().splitlines()]
        self.assertEqual(len(docs), report["chunk_count"])
        self.assertEqual(len({d["uid"] for d in docs}), len(docs))
        for record in records:
            self.assertEqual(record["original_transcript"].encode("utf-8"), self.transcript.encode("utf-8"))
            self.assertEqual(record["transcript_sha256"], digest(self.transcript.encode("utf-8")))
            text = record["projection_text"]
            self.assertEqual(text, project(self.transcript))
            source_docs = [d for d in docs if d["source_id"] == record["source_id"]]
            self.assertEqual(len(source_docs), record["chunk_count"])
            reconstructed = ""
            for index, doc in enumerate(source_docs):
                self.assertRegex(doc["uid"], KEY)
                self.assertLessEqual(len(doc["uid"]), 128)
                start, end = doc["projection_start"], doc["projection_end"]
                self.assertEqual(doc["snippet"], text[start:end])
                self.assertEqual(doc["chunk_index"], index)
                self.assertLessEqual(len(doc["snippet"]), 4000)
                if index:
                    self.assertEqual(source_docs[index - 1]["projection_end"] - start, 500)
                    self.assertEqual(source_docs[index - 1]["snippet"][-500:], doc["snippet"][:500])
                reconstructed += doc["snippet"][500:] if index else doc["snippet"]
            self.assertEqual(reconstructed, text)
            self.assertTrue(reconstructed.endswith("END-TAIL-😀"))
            self.assertEqual(source_docs[-1]["projection_end"], len(text))
        for name, artifact in report["artifacts"].items():
            self.assertEqual(digest((self.output / name).read_bytes()), artifact["sha256"])
        seeds = json.loads((self.output / "topics.json").read_bytes())
        self.assertEqual(len(seeds["topics"]), 61)
        self.assertTrue(seeds["seed_metadata_only"])
        self.assertTrue(all(t["id"].startswith("topic-") for t in seeds["topics"]))
        self.assertFalse(any(t["name"] == "Congress" for t in seeds["topics"]))
        with self.assertRaisesRegex(ValueError, "new output"):
            self.run_prepare()

    def test_html_projection_is_explicitly_not_verbatim(self):
        transcript = "<p>One &amp; <em>two</em><br/>three &#128512;</p><!--comment-->"
        self.assertEqual(project(transcript), "\nOne & two\nthree 😀\n")
        self.assertEqual(project("  Plain\r\ntext < 2 &lt;x&gt;  "), "  Plain\r\ntext < 2 <x>  ")
        with self.assertRaisesRegex(ValueError, "empty"):
            project("<p><!--nothing--></p>")

    def test_boundaries_overlap_unicode_and_final_remainder(self):
        for length in (1, 3999, 4000, 4001, 7499, 7500, 7501, 8001):
            with self.subTest(length=length):
                text = "😀" * (length - 1) + "Z"
                parts = list(chunks(text))
                rebuilt = parts[0][2] + "".join(p[2][500:] for p in parts[1:])
                self.assertEqual(rebuilt, text)
                self.assertEqual(parts[-1][1], length)
                self.assertEqual(parts[0][0], 0)
        for size, overlap in ((0, 0), (4000, 4000), (5, -1), (2, 3)):
            with self.assertRaises(ValueError):
                list(chunks("text", size, overlap))

    def test_exact_offender_and_no_success_manifest_for_malformed_source(self):
        path = self.source / "third.json"
        path.write_bytes(b'{"transcript":')
        with self.assertRaisesRegex(ValueError, "third.json"):
            self.run_prepare()
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertFalse(self.output.exists())

    def test_dropped_source_or_corrupted_prepared_asset_fails(self):
        ledger_bytes = self.ledger_path.read_bytes()
        ledger = json.loads(ledger_bytes)
        ledger["items"].pop()
        self.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source count"):
            self.run_prepare()
        self.ledger_path.write_bytes(ledger_bytes)
        entry = self.ledger["items"][0]
        prepared = Path(self.ledger["derived_reference"]) / entry["filename"]
        prepared.write_bytes(prepared.read_bytes() + b"corruption")
        with self.assertRaisesRegex(ValueError, "Source .*manifest hash"):
            self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_missing_source_and_ledger_corruption_fail_closed(self):
        entry = self.ledger["items"][0]
        entry["segments"][1]["length"] -= 1
        self.ledger_path.write_text(json.dumps(self.ledger), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source .*segments"):
            self.run_prepare()
        (self.source / "third.json").unlink()
        with self.assertRaisesRegex(ValueError, "inventory count"):
            self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_readback_verifier_rejects_dropped_tail(self):
        self.run_prepare()
        docs_path = self.output / "docs.jsonl"
        lines = docs_path.read_bytes().splitlines(keepends=True)
        docs_path.write_bytes(b"".join(lines[:-1]))
        correspondences = [
            json.loads(line) for line in (self.output / "correspondence.jsonl").read_bytes().splitlines()
        ]
        base_fields = {
            "source_id", "canonical_id", "title", "speaker", "date", "transcript_sha256",
            "raw_sha256", "projection", "projection_sha256",
        }
        records = [
            {"base": {k: r[k] for k in base_fields}, "correspondence": r}
            for r in correspondences
        ]
        with self.assertRaises(ValueError):
            verify_documents(docs_path, records, 4000, 500)


if __name__ == "__main__":
    unittest.main()
