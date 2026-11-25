#!/usr/bin/env python3
"""Package only verified, nonsecret production API files with portable read permissions."""

import argparse
import json
import os
import stat
import zipfile
from pathlib import Path


API = Path(__file__).resolve().parents[1] / "api"
FILES = ("function_app.py", "service.py", "host.json", "requirements.txt", "corpus.json")


def package(output, api=API):
    config = json.loads((api / "corpus.json").read_text())
    if (config.get("coverage_verified") is not True or config.get("mode") != "full"
            or config.get("source_count") != 1060 or len(config["speeches"]) != 1060
            or config.get("chunk_count") != 7422 or config.get("canonical_count") != 1059):
        raise ValueError("A verified full-corpus configuration is required.")
    contents = [(name, (api / name).read_bytes()) for name in FILES]
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle, zipfile.ZipFile(
            handle, "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for name, data in contents:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                # Atomic JSON creation uses 0600 locally. The deployed worker must
                # be able to read these allowlisted files under its own Linux UID.
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
    except BaseException:
        Path(output).unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="A new ZIP path; existing files are never replaced.")
    package(parser.parse_args().output)
    print("Packaged the verified full-corpus API; no local settings, tests, or development files.")
