import tempfile
import unittest
from pathlib import Path

from prepare_speech_grounding import registry, write_registry


class GroundingRegistryTests(unittest.TestCase):
    def provenance(self):
        item = {
            "metadata": {
                "title": "July 16, 2026: Address",
                "president": "Donald Trump", "date": "2026-07-16",
            },
            "original_metadata": {"title": "July 16,2026: Address "},
            "speech_id": "speech-one",
        }
        return {"status": "complete", "items": [item, item]}

    def test_shared_sources_use_one_id_and_explicit_topic_aliases(self):
        rows = registry(self.provenance(), {
            "topics": [{"name": "healthcare", "aliases": ["health care"]}]
        })
        self.assertEqual(len(rows), 4)
        speech = next(row for row in rows if row["type"] == "Speech")
        self.assertEqual(speech["id"], "speech-one")
        self.assertIn("July 16,2026: Address", speech["aliases"])
        topic = next(row for row in rows if row["type"] == "Topic")
        self.assertEqual(topic["aliases"], "health care")

    def test_ambiguous_aliases_and_loader_delimiters_are_rejected(self):
        for topics in (
            [{"name": "one", "aliases": ["shared"]},
             {"name": "two", "aliases": ["SHARED"]}],
            [{"name": "one", "aliases": ["hidden|second"]}],
            [{"name": "Donald Trump", "aliases": []}],
        ):
            with self.assertRaises(ValueError):
                registry(self.provenance(), {"topics": topics})

    def test_registry_output_cannot_contaminate_either_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            provenance = self.provenance()
            for key in ("source_reference", "derived_reference"):
                directory = root / key
                directory.mkdir()
                provenance[key] = str(directory)
            for key in ("source_reference", "derived_reference"):
                output = Path(provenance[key]) / "entities.csv"
                with self.assertRaisesRegex(ValueError, "outside the source corpora"):
                    write_registry(provenance, {"topics": []}, output)
                self.assertFalse(output.exists())
            output = root / "entities.csv"
            self.assertEqual(len(write_registry(provenance, {"topics": []}, output)), 3)
            with self.assertRaises(ValueError):
                write_registry(provenance, {"topics": []}, output)


if __name__ == "__main__":
    unittest.main()
