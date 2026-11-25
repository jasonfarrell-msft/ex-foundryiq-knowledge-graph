import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_speech_graph_corpus as corpus


HEADER = (
    "# January 20, 1993: First Inaugural\n\n"
    "**President**: Bill Clinton  \nDate: 1993-01-20\n\n**Transcript**\n\n"
)


class SpeechCorpusTests(unittest.TestCase):
    def test_every_editorial_block_repeats_the_authoritative_president_field(self):
        raw = (HEADER + "A constitution protects liberty. " * 300).encode()
        derived, _, segments, _ = corpus.render(raw)
        corpus.verify_segments(raw, derived, segments)
        blocks = derived.decode().split("**Source metadata (editorial, not transcript)**")[1:]
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertTrue(block.startswith("\n**President**: Bill Clinton\n"))
        self.assertEqual(derived.count(b"**President**: Bill Clinton"), len(blocks) + 1)

    def test_preserves_bytes_and_context_with_long_unicode_paragraphs(self):
        body = ("A constitution protects liberty. " + "\U0001f600" * 10 + " ") * 400
        raw = (HEADER + body + "\nShort final sentence.\n").replace("\n", "\r\n").encode()
        derived, metadata, segments, fact = corpus.render(raw)
        corpus.verify_segments(raw, derived, segments)
        self.assertTrue(derived.startswith(HEADER.replace("\n", "\r\n").encode()))
        self.assertEqual(metadata["president"], "Bill Clinton")
        for piece in corpus.excerpts(body):
            self.assertLessEqual(len(piece), 1000)
            self.assertLessEqual(corpus.utf16_length(piece), 1000)
        encoded = derived.decode().encode("utf-16-le")
        assertion = fact.encode("utf-16-le")
        for start in range(0, len(encoded) - 4000 + 1, 2):
            self.assertIn(assertion, encoded[start:start + 4000])
        self.assertIn(assertion, encoded[-4000:])
        self.assertTrue(derived.endswith((fact + "\n\n").encode()))
        self.assertEqual(
            b"".join(derived[s["derived_start"]:s["derived_start"] + s["length"]] for s in segments),
            raw,
        )

    def test_editorial_lookalikes_in_transcript_are_not_removed(self):
        raw = (HEADER + "**Source metadata (editorial, not transcript)**\n"
               'Bill Clinton delivered the speech named "January 20, 1993: First Inaugural" '
               "on 1993-01-20.\n\n**Transcript excerpt**\n\nOriginal quotation.\n").encode()
        derived, _, segments, _ = corpus.render(raw)
        corpus.verify_segments(raw, derived, segments)
        with self.assertRaises(ValueError):
            corpus.verify_segments(raw, derived + b"", segments[:-1])

    def test_rejects_invalid_dates_duplicate_metadata_and_ambiguous_markers(self):
        cases = [
            HEADER.replace("1993-01-20", "1993-02-30") + "Text",
            HEADER.replace("Date:", "Date: 1993-01-20\nDate:") + "Text",
            HEADER + "Text\n**Transcript**\n\nMore text",
            HEADER + "  \n",
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                corpus.render(text.encode())

    def fixture(self, root):
        source, originals = root / "source", root / "documents"
        source.mkdir()
        originals.mkdir()
        raw = (HEADER + "Preserve all words and whitespace.\n").encode()
        (source / "speech.md").write_bytes(raw)
        (originals / "speech.md").write_bytes(raw)
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"manifest": {
            "source": {"kind": "local_path", "reference": str(source)},
            "items": [{
                "original_uri": (source / "speech.md").as_uri(),
                "version_or_hash": corpus.digest(raw),
            }],
        }}))
        return source, originals, manifest, raw

    def test_preparation_preserves_sources_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, originals, manifest, raw = self.fixture(root)
            output, mapping = root / "derived", root / "mapping.json"
            report = corpus.prepare(manifest, originals, output, mapping)
            self.assertEqual(report["status"], "complete")
            self.assertFalse((output / "INCOMPLETE").exists())
            self.assertEqual((source / "speech.md").read_bytes(), raw)
            self.assertEqual((originals / "speech.md").read_bytes(), raw)
            saved = json.loads(mapping.read_text())
            corpus.verify_segments(raw, (output / "speech.md").read_bytes(), saved["items"][0]["segments"])
            with self.assertRaises(ValueError):
                corpus.prepare(manifest, originals, output, mapping)

    def test_validation_failures_never_create_output(self):
        for failure in ("unexpected", "changed", "duplicate", "symlink"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source, originals, manifest, raw = self.fixture(root)
                if failure == "unexpected":
                    (source / "extra.md").write_bytes(raw)
                elif failure == "changed":
                    (originals / "speech.md").write_bytes(raw + b"changed")
                elif failure == "duplicate":
                    (source / "duplicate.md").write_bytes(raw)
                    (originals / "duplicate.md").write_bytes(raw)
                    data = json.loads(manifest.read_text())
                    data["manifest"]["items"].append({
                        "original_uri": (source / "duplicate.md").as_uri(),
                        "version_or_hash": corpus.digest(raw),
                    })
                    manifest.write_text(json.dumps(data))
                else:
                    (source / "speech.md").unlink()
                    (source / "speech.md").symlink_to(originals / "speech.md")
                output = root / "derived"
                with self.assertRaises(ValueError):
                    corpus.prepare(manifest, originals, output, root / "mapping.json")
                self.assertFalse(output.exists())

    def test_destination_symlink_and_interrupted_write_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, originals, manifest, raw = self.fixture(root)
            output = root / "derived"
            output.symlink_to(originals, target_is_directory=True)
            with self.assertRaises(ValueError):
                corpus.prepare(manifest, originals, output, root / "mapping.json")
            output.unlink()
            original_open = Path.open

            def interrupted(path, mode="r", *args, **kwargs):
                if path == output / "speech.md" and mode == "xb":
                    raise OSError("Simulated interrupted write.")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", interrupted), self.assertRaises(OSError):
                corpus.prepare(manifest, originals, output, root / "mapping.json")
            self.assertTrue((output / "INCOMPLETE").is_file())
            self.assertEqual((originals / "speech.md").read_bytes(), raw)
            self.assertEqual((source / "speech.md").read_bytes(), raw)

    def test_mapping_cannot_write_into_either_input_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, originals, manifest, _ = self.fixture(root)
            output = root / "derived"
            for directory in (source, originals):
                with self.assertRaises(ValueError):
                    corpus.prepare(manifest, originals, output, directory / "mapping.json")
                self.assertFalse(output.exists())
                self.assertFalse((directory / "mapping.json").exists())


if __name__ == "__main__":
    unittest.main()
