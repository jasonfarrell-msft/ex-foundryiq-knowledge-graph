#!/usr/bin/env python3
"""Prepare every raw speech locally, preserving transcripts and source provenance."""

import argparse
import datetime
import json
import re
import unicodedata

from prepare_speech_graph_corpus import (
    digest,
    page_estimate,
    render,
    safe_path,
    verify_segments,
)


def canonical_text(value):
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ValueError("Metadata must be single-line text.")
    value = unicodedata.normalize("NFC", value.strip())
    if not value:
        raise ValueError("Metadata must not be empty.")
    return value


def speech_record(path):
    raw = path.read_bytes()
    source = json.loads(raw)
    title = re.sub(r",\s*", ", ", canonical_text(source["title"]))
    president = canonical_text(source["president"])
    date = datetime.datetime.fromisoformat(source["date"]).date().isoformat()
    transcript = source["transcript"]
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("A complete, nonempty transcript is required.")
    identity = json.dumps(
        [president, date, title], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    markdown = (
        f"# {title}\n\n**President**: {president}\nDate: {date}\n\n"
        "**Transcript**\n\n" + transcript
    ).encode("utf-8")
    derived, metadata, segments, fact = render(markdown)
    verify_segments(markdown, derived, segments)
    transcript_bytes = transcript.encode("utf-8")
    reconstructed = b"".join(
        derived[s["derived_start"]:s["derived_start"] + s["length"]]
        for s in segments[1:]
    )
    if reconstructed != transcript_bytes:
        raise ValueError("Parsed transcript preservation failed.")
    slug = re.sub(r"[^a-z0-9-]", "", title.lower().replace(":", "-"))[:100]
    filename = f"{slug or 'speech'}-{digest(path.name.encode())[7:23]}.md"
    return raw, derived, {
        "filename": filename,
        "raw_source_uri": path.as_uri(),
        "raw_source_hash": digest(raw),
        "source_id": "source-" + digest(path.name.encode())[7:],
        "speech_id": "speech-" + digest(identity)[7:],
        "metadata": metadata,
        "original_metadata": {
            k: source[k] for k in ("title", "president", "date")
        },
        "transcript_hash": digest(transcript_bytes),
        "transcript_bytes": len(transcript_bytes),
        "prepared_markdown_hash": digest(markdown),
        "derived_hash": digest(derived),
        "derived_bytes": len(derived),
        "derived_page_estimate": page_estimate(derived),
        "segments": segments,
        "editorial_assertion": fact,
    }


def prepare(source, destination, mapping, expected_sources, expected_speeches):
    source, destination, mapping = map(safe_path, (source, destination, mapping))
    if not source.is_dir() or expected_sources < 1 or expected_speeches < 1:
        raise ValueError("A source directory and positive expected counts are required.")
    if (
        destination.exists() or mapping.exists()
        or destination.is_relative_to(source) or source.is_relative_to(destination)
        or mapping.is_relative_to(source) or mapping.is_relative_to(destination)
    ):
        raise ValueError("Use new outputs outside the original source directory.")
    paths = sorted(source.glob("*.json"))
    if len(paths) != expected_sources:
        raise ValueError("Source count differs from the explicitly expected count.")
    prepared, identities, filenames = [], {}, set()
    for path in paths:
        path = safe_path(path)
        raw, derived, item = speech_record(path)
        if item["filename"] in filenames:
            raise ValueError("Output filename collision.")
        filenames.add(item["filename"])
        previous = identities.setdefault(item["speech_id"], item)
        if previous["transcript_hash"] != item["transcript_hash"]:
            raise ValueError("Different transcripts share a canonical speech identity.")
        prepared.append((path, raw, derived, item))
    if len(identities) != expected_speeches:
        raise ValueError("Canonical speech count differs from the expected count.")
    destination.mkdir()
    marker = destination / "INCOMPLETE"
    with marker.open("xb") as handle:
        handle.write(b"Preparation incomplete. Do not inventory or upload.\n")
    items = []
    for path, raw, derived, item in prepared:
        output = destination / item["filename"]
        with output.open("xb") as handle:
            handle.write(derived)
        if output.read_bytes() != derived or path.read_bytes() != raw:
            raise ValueError("Source or derived file changed during preparation.")
        item["derived_uri"] = output.as_uri()
        items.append(item)
    if sorted(source.glob("*.json")) != paths:
        raise ValueError("Source inventory changed during preparation.")
    for path, raw, _, _ in prepared:
        if safe_path(path).read_bytes() != raw:
            raise ValueError("Original source changed during preparation.")
    duplicates = {}
    for item in items:
        duplicates.setdefault(item["speech_id"], []).append(item["source_id"])
    report = {
        "version": 1,
        "status": "complete",
        "azure_writes": False,
        "source_count": len(items),
        "canonical_speech_count": len(identities),
        "source_reference": str(source),
        "derived_reference": str(destination),
        "derived_bytes": sum(i["derived_bytes"] for i in items),
        "derived_page_estimate": sum(i["derived_page_estimate"] for i in items),
        "page_estimate_caveat": "2000 UTF-16 units, overlap 200; actual pages vary.",
        "preservation": (
            "All original JSON files retained and individually hashed. Parsed "
            "transcript UTF-8 bytes, including inline HTML, preserved verbatim "
            "through the segment ledger. This is not raw JSON reconstruction."
        ),
        "metadata_normalization": (
            "Derived headers only: NFC, outer whitespace trim, comma spacing, "
            "ISO calendar date. Original metadata recorded separately."
        ),
        "duplicate_source_groups": {
            key: values for key, values in duplicates.items() if len(values) > 1
        },
        "items": items,
    }
    with mapping.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    marker.unlink()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--expected-sources", type=int, required=True)
    parser.add_argument("--expected-speeches", type=int, required=True)
    args = parser.parse_args()
    try:
        report = prepare(
            args.source, args.output, args.mapping,
            args.expected_sources, args.expected_speeches,
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Preparation failed: {error}\n")
    print(json.dumps({k: v for k, v in report.items() if k != "items"}, indent=2))


if __name__ == "__main__":
    main()
