#!/usr/bin/env python3
"""Create a local, editorially augmented pilot; never upload or call Azure."""

import argparse
import datetime
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


HEADER = re.compile(
    r"\A# (?P<title>[^\r\n]+)\r?\n\r?\n"
    r"\*\*President\*\*: (?P<president>[^\r\n]+)\r?\n"
    r"Date: (?P<date>\d{4}-\d{2}-\d{2})\r?\n\r?\n"
    r"\*\*Transcript\*\*\r?\n\r?\n"
)


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def safe_path(value):
    path = Path(value).absolute()
    if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Paths must not contain traversal or symbolic links.")
    return path


def excerpts(text):
    while text:
        units = end = 0
        for character in text[:1000]:
            size = utf16_length(character)
            if units + size > 1000:
                break
            units += size
            end += 1
        if end < len(text):
            whitespace = [i + 1 for i, c in enumerate(text[:end]) if c.isspace()]
            if whitespace and whitespace[-1] > end // 2:
                end = whitespace[-1]
            if text[end - 1:end + 1] == "\r\n":
                end -= 1
        yield text[:end]
        text = text[end:]


def render(raw):
    text = raw.decode("utf-8")
    match = HEADER.match(text)
    if not match or len(re.findall(r"(?m)^\*\*Transcript\*\*\r?$", text)) != 1:
        raise ValueError("Expected one unambiguous title/President/Date/Transcript header.")
    metadata = match.groupdict()
    metadata["president"] = metadata["president"].strip()
    if not metadata["president"] or metadata["title"] != metadata["title"].strip():
        raise ValueError("A canonical title and a named president are required.")
    datetime.date.fromisoformat(metadata["date"])
    transcript = text[match.end():]
    if not transcript.strip():
        raise ValueError("Transcript must not be empty.")
    fact = (
        f'{metadata["president"]} delivered the speech named '
        f'{json.dumps(metadata["title"], ensure_ascii=False)} on {metadata["date"]}.'
    )
    editorial = (
        "**Source metadata (editorial, not transcript)**\n"
        f'**President**: {metadata["president"]}\n' + fact + "\n\n"
    )
    block = editorial + "**Transcript excerpt**\n\n"
    if utf16_length(block) > 500:
        raise ValueError("Metadata is too long for the tested context-window bounds.")
    header = text[:match.end()].encode("utf-8")
    output = bytearray(header)
    segments = [{
        "source_start": 0, "derived_start": 0, "length": len(header),
        "sha256": digest(header),
    }]
    source_start = len(header)
    for excerpt in excerpts(transcript):
        output.extend(block.encode("utf-8"))
        data = excerpt.encode("utf-8")
        segments.append({
            "source_start": source_start, "derived_start": len(output),
            "length": len(data), "sha256": digest(data),
        })
        output.extend(data)
        output.extend(b"\n\n")
        source_start += len(data)
    output.extend(editorial.encode("utf-8"))
    return bytes(output), metadata, segments, fact


def verify_segments(source, derived, segments):
    cursor = 0
    reconstructed = bytearray()
    for segment in segments:
        start, length = segment["derived_start"], segment["length"]
        data = derived[start:start + length]
        if (
            segment["source_start"] != cursor or digest(data) != segment["sha256"]
            or data != source[cursor:cursor + length]
        ):
            raise ValueError("Source preservation verification failed.")
        reconstructed.extend(data)
        cursor += length
    if bytes(reconstructed) != source:
        raise ValueError("Source preservation verification failed.")


def page_estimate(data):
    units = utf16_length(data.decode("utf-8"))
    return 1 + math.ceil(max(0, units - 2000) / 1800)


def prepare(manifest_path, originals, destination, mapping_path):
    manifest_path, originals, destination, mapping_path = map(
        safe_path, (manifest_path, originals, destination, mapping_path)
    )
    manifest = json.loads(manifest_path.read_bytes())["manifest"]
    if manifest["source"]["kind"] != "local_path":
        raise ValueError("Only a frozen local Markdown corpus is supported.")
    source = safe_path(manifest["source"]["reference"])
    if (
        destination.exists() or mapping_path.exists()
        or destination.is_relative_to(source) or destination.is_relative_to(originals)
        or any(mapping_path.is_relative_to(root) for root in (destination, source, originals))
    ):
        raise ValueError("Use new, separate corpus and mapping paths; nothing is overwritten.")
    prepared, names, titles = [], set(), set()
    for item in manifest["items"]:
        uri = urlparse(item["original_uri"])
        if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
            raise ValueError("Expected local source URIs.")
        path = safe_path(unquote(uri.path))
        name = path.name
        if (
            path.parent != source or not re.fullmatch(r"[a-z0-9-]+\.md", name)
            or name in names or not path.is_file()
        ):
            raise ValueError("Unexpected or duplicate source file.")
        original = safe_path(originals / name)
        if not original.is_file():
            raise ValueError("Original document is missing.")
        raw = path.read_bytes()
        if digest(raw) != item["version_or_hash"] or original.read_bytes() != raw:
            raise ValueError("Frozen or original document differs from its approved hash.")
        derived, metadata, segments, fact = render(raw)
        if metadata["title"].casefold() in titles:
            raise ValueError("Duplicate canonical speech title.")
        names.add(name)
        titles.add(metadata["title"].casefold())
        verify_segments(raw, derived, segments)
        prepared.append((path, original, raw, derived, metadata, segments, fact))
    if not names or {p.name for p in source.iterdir()} != names:
        raise ValueError("Frozen corpus contains unexpected files or is empty.")
    destination.mkdir()
    marker = destination / "INCOMPLETE"
    with marker.open("xb") as file:
        file.write(b"Preparation incomplete. Do not inventory or upload this directory.\n")
    entries = []
    for path, original, raw, derived, metadata, segments, fact in prepared:
        output = destination / path.name
        with output.open("xb") as file:
            file.write(derived)
        verify_segments(raw, output.read_bytes(), segments)
        if path.read_bytes() != raw or original.read_bytes() != raw:
            raise ValueError("An original changed during preparation; output remains incomplete.")
        entries.append({
            "filename": path.name, "original_uri": original.as_uri(),
            "frozen_uri": path.as_uri(), "derived_uri": output.as_uri(),
            "original_hash": digest(raw), "derived_hash": digest(derived),
            "metadata": metadata, "editorial_assertion": fact, "segments": segments,
            "original_bytes": len(raw), "derived_bytes": len(derived),
            "original_full_length_page_estimate": page_estimate(raw),
            "derived_full_length_page_estimate": page_estimate(derived),
        })
    for path, original, raw, *_ in prepared:
        safe_path(path)
        safe_path(original)
        if path.read_bytes() != raw or original.read_bytes() != raw:
            raise ValueError("An original changed during preparation; output remains incomplete.")
    before = sum(e["original_bytes"] for e in entries)
    after = sum(e["derived_bytes"] for e in entries)
    report = {
        "version": 1, "status": "complete", "azure_writes": False,
        "description": "Derived pilot with editorial metadata interleaved between verbatim excerpts.",
        "original_bytes": before, "derived_bytes": after,
        "expansion_ratio": after / before,
        "original_full_length_page_estimate": sum(e["original_full_length_page_estimate"] for e in entries),
        "derived_full_length_page_estimate": sum(e["derived_full_length_page_estimate"] for e in entries),
        "page_estimate_caveat": "2000 UTF-16 units, overlap 200; sentence boundaries can change actual pages.",
        "publication_gate": "Inspect every actual chunk for canonical metadata and a valid vector; validate exact graph relations and provenance.",
        "items": entries,
    }
    with mapping_path.open("x", encoding="utf-8") as file:
        json.dump(report, file, indent=2, ensure_ascii=False)
        file.write("\n")
    marker.unlink()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--originals", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mapping", required=True)
    args = parser.parse_args()
    try:
        report = prepare(args.source_manifest, args.originals, args.output, args.mapping)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Preparation failed: {error}\n")
    print(json.dumps({k: v for k, v in report.items() if k != "items"}, indent=2))


if __name__ == "__main__":
    main()
