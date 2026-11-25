import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from package_speech_api import FILES, package


class PackageTests(unittest.TestCase):
    def test_generated_private_metadata_is_readable_in_worker_and_unrelated_files_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = root / "api"
            api.mkdir()
            for name in FILES:
                (api / name).write_text("example")
            config = {"mode": "full", "coverage_verified": True, "source_count": 1060,
                      "chunk_count": 7422, "canonical_count": 1059, "speeches": [{}] * 1060}
            (api / "corpus.json").write_text(json.dumps(config))
            (api / "corpus.json").chmod(0o600)
            (api / "local.settings.json").write_text("must not ship")
            output = root / "api.zip"
            package(output, api)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(FILES))
                for entry in archive.infolist():
                    self.assertEqual(stat.S_IMODE(entry.external_attr >> 16), 0o644)
                    self.assertEqual(archive.read(entry), (api / entry.filename).read_bytes())
            self.assertEqual(stat.S_IMODE((api / "corpus.json").stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                package(output, api)
            partial = root / "partial.zip"
            with patch.object(zipfile.ZipFile, "writestr", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    package(partial, api)
            self.assertFalse(partial.exists())
            config["coverage_verified"] = False
            (api / "corpus.json").write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                package(root / "invalid.zip", api)
            self.assertFalse((root / "invalid.zip").exists())


if __name__ == "__main__":
    unittest.main()
