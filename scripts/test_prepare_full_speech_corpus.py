import json
import tempfile
import unittest
from pathlib import Path

from prepare_full_speech_corpus import prepare


class FullCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.mapping = self.root / "mapping.json"

    def write(self, filename, title="July 16,2026: Address ", transcript=None):
        path = self.source / filename
        path.write_text(json.dumps({
            "title": title,
            "president": "Donald Trump",
            "date": "2026-07-16T21:00:00-04:00",
            "transcript": transcript if transcript is not None else (
                "<em>Exact</em> words.\r\n" + "Text \U0001f600 &amp; " * 240
            ),
        }), encoding="utf-8")
        return path

    def test_preserves_html_unicode_and_both_duplicate_sources(self):
        paths = [
            self.write("first.json"),
            self.write("second.json", title="July 16, 2026: Address"),
        ]
        originals = {p: p.read_bytes() for p in paths}
        report = prepare(self.source, self.output, self.mapping, 2, 1)
        self.assertEqual(report["source_count"], 2)
        self.assertEqual(report["canonical_speech_count"], 1)
        self.assertEqual(len(list(self.output.iterdir())), 2)
        self.assertEqual(len(report["duplicate_source_groups"]), 1)
        self.assertNotEqual(report["items"][0]["filename"], report["items"][1]["filename"])
        for item in report["items"]:
            raw = json.loads(Path(item["raw_source_uri"].removeprefix("file://")).read_bytes())
            derived = Path(item["derived_uri"].removeprefix("file://")).read_bytes()
            body = b"".join(
                derived[s["derived_start"]:s["derived_start"] + s["length"]]
                for s in item["segments"][1:]
            )
            self.assertEqual(body, raw["transcript"].encode("utf-8"))
            self.assertEqual(item["metadata"]["title"], "July 16, 2026: Address")
        self.assertEqual({p: p.read_bytes() for p in paths}, originals)

    def test_conflicting_transcripts_fail_before_any_output(self):
        self.write("first.json")
        self.write("second.json", transcript="A different transcript.")
        with self.assertRaisesRegex(ValueError, "Different transcripts"):
            prepare(self.source, self.output, self.mapping, 2, 1)
        self.assertFalse(self.output.exists())

    def test_counts_and_existing_outputs_are_not_silently_accepted(self):
        self.write("first.json")
        with self.assertRaisesRegex(ValueError, "Source count"):
            prepare(self.source, self.output, self.mapping, 2, 1)
        with self.assertRaisesRegex(ValueError, "Canonical speech count"):
            prepare(self.source, self.output, self.mapping, 1, 2)
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "new outputs"):
            prepare(self.source, self.output, self.mapping, 1, 1)

    def test_rejects_symlinks_and_invalid_metadata(self):
        path = self.write("first.json")
        (self.source / "link.json").symlink_to(path)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            prepare(self.source, self.output, self.mapping, 2, 1)
        (self.source / "link.json").unlink()
        self.write("first.json", title="Title\nInjected header")
        with self.assertRaisesRegex(ValueError, "single-line"):
            prepare(self.source, self.output, self.mapping, 1, 1)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
