#!/usr/bin/env python3
"""Prepare local search projections, never a validated semantic graph or Azure upload.

Inputs are the frozen full-corpus manifest, preservation ledger, and original assets.
Offsets in docs.jsonl are Python Unicode character offsets into projection_text in
correspondence.jsonl, NOT byte offsets into the original transcript. Hashes use the
existing ledger's ``sha256:<hex>`` convention. Originals are never rewritten.
"""

import argparse
import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from prepare_full_speech_corpus import speech_record
from prepare_speech_graph_corpus import digest, safe_path


ROOT = Path(__file__).resolve().parents[1]
RUN = Path(".foundry-iq/fiq-speeches-9c65c09f")
OUTPUT = RUN / "entity/app-full-search-v1"
PROJECTION = "html-readable-v1"
CHUNK_SIZE = 4000
OVERLAP = 500
KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ReadableHTML(HTMLParser):
    """Keep data/entities, discard markup/comments, separate block boundaries."""

    BLOCKS = {
        "address", "article", "aside", "blockquote", "br", "div", "dl", "dt",
        "dd", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
        "li", "main", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def project(transcript):
    parser = ReadableHTML()
    parser.feed(transcript)
    parser.close()
    text = "".join(parser.parts)
    if not text.strip():
        raise ValueError("Readable transcript projection is empty.")
    return text


def chunks(text, size=CHUNK_SIZE, overlap=OVERLAP):
    if not isinstance(size, int) or not isinstance(overlap, int) or not 0 <= overlap < size:
        raise ValueError("Chunk size must be positive and overlap smaller than size.")
    if not text.strip():
        raise ValueError("Cannot chunk an empty transcript.")
    start = 0
    while True:
        end = min(start + size, len(text))
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = end - overlap


def local_uri(value):
    uri = urlparse(value)
    if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
        raise ValueError(f"Expected a local file URI: {value!r}")
    return safe_path(unquote(uri.path))


def encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def topic_metadata(lexicon_bytes, registry_bytes):
    """Export curated seeds only; never consume observed/trial graph entities."""
    seeds = json.loads(lexicon_bytes)["topics"]
    rows = list(csv.DictReader(io.StringIO(registry_bytes.decode("utf-8"))))
    registry_topics = {row["name"]: row for row in rows if row["type"] == "Topic"}
    require(len(seeds) == len(registry_topics) == 61, "Expected 61 curated topic seeds.")
    topics = []
    for seed in seeds:
        row = registry_topics[seed["name"]]
        identifier = "topic-" + digest(seed["name"].casefold().encode("utf-8"))[7:]
        require(row["id"] == identifier, f"Topic registry ID mismatch: {seed['name']}")
        phrases = [seed["name"], *seed["aliases"]]
        registered = {row["name"].casefold(), *(a.casefold() for a in row["aliases"].split("|"))}
        require(all(p.casefold() in registered for p in phrases),
                f"Topic registry aliases mismatch: {seed['name']}")
        topics.append({"id": identifier, "name": seed["name"], "aliases": seed["aliases"]})
    return {
        "seed_metadata_only": True,
        "policy": (
            "For future exact phrase matching of name or listed aliases only, "
            "case-insensitive with Unicode word boundaries. No stemming, fuzzy, "
            "prefix, native BM25 grounding, semantic equivalence, or addressesTopic "
            "assertions. No matches or graph edges are asserted by this file."
        ),
        "topics": topics,
    }


def verify_documents(path, records, size, overlap):
    """Read serialized chunks back and verify exact coverage, overlap and end tails."""
    total = 0
    with path.open("rb") as handle:
        for record in records:
            base, text = record["base"], record["correspondence"]["projection_text"]
            for index, (start, end, snippet) in enumerate(chunks(text, size, overlap)):
                expected = {
                    **base, "uid": f"{base['source_id']}-{index:06d}",
                    "snippet": snippet, "chunk_index": index,
                    "projection_start": start, "projection_end": end,
                }
                require(KEY.fullmatch(expected["uid"]), f"Invalid uid: {expected['uid']}")
                require(json.loads(handle.readline()) == expected,
                        f"Serialized chunk mismatch: {expected['uid']}")
                total += 1
        require(not handle.read(), "Unexpected extra serialized chunks.")
    return total


def prepare(input_root=ROOT, output=None, expected_sources=1060,
            expected_canonicals=1059, size=CHUNK_SIZE, overlap=OVERLAP):
    root = safe_path(input_root)
    destination = safe_path(output if output is not None else root / OUTPUT)
    # Validate bounds even before any source is read.
    list(chunks("bounds", size, overlap))
    require(expected_sources > 0 and expected_canonicals > 0, "Expected counts must be positive.")
    require(not destination.exists(), "Use a new output directory; nothing is overwritten.")
    run = root / RUN
    manifest_path = run / "entity/full-corpus-v7-manifest.json"
    ledger_path = run / "entity/full-corpus-v7-provenance.json"
    lexicon_path = run / "ontology/topic-lexicon-v7.json"
    registry_path = run / "entity/grounding-entities-v7.csv"
    inputs = {}

    def read_input(path):
        path = safe_path(path)
        require(not path.is_relative_to(destination), f"Output overlaps input: {path}")
        raw = path.read_bytes()
        observed = {"path": str(path), "sha256": digest(raw), "bytes": len(raw)}
        previous = inputs.setdefault(path, observed)
        require(previous == observed, f"Input changed while reading: {path}")
        return raw

    manifest = json.loads(read_input(manifest_path))["manifest"]
    ledger = json.loads(read_input(ledger_path))
    require(ledger["status"] == "complete", "Preservation ledger is not complete.")
    require(manifest["source"]["kind"] == "local_path", "Expected local prepared corpus.")
    source = safe_path(ledger["source_reference"])
    derived = safe_path(ledger["derived_reference"])
    require(safe_path(manifest["source"]["reference"]) == derived,
            "Manifest and ledger prepared roots differ.")
    for protected in (source, derived):
        require(not destination.is_relative_to(protected) and not protected.is_relative_to(destination),
                f"Output overlaps protected corpus: {protected}")
    inventory = sorted(source.glob("*.json"))
    require(len(inventory) == expected_sources, "Original source inventory count mismatch.")
    require(len(ledger["items"]) == ledger["source_count"] == expected_sources,
            "Ledger source count mismatch.")
    require(len(manifest["items"]) == expected_sources, "Manifest source count mismatch.")
    frozen = {}
    for item in manifest["items"]:
        path = local_uri(item["original_uri"])
        require(path.parent == derived and path not in frozen,
                f"Unexpected or duplicate prepared manifest path: {path}")
        frozen[path] = item["version_or_hash"]
    topics = topic_metadata(read_input(lexicon_path), read_input(registry_path))
    records, source_ids, raw_paths, prepared_paths, canonical_hashes = [], set(), set(), set(), {}
    duplicates = {}
    for item in ledger["items"]:
        offender = item.get("raw_source_uri", repr(item))
        try:
            raw_path = local_uri(item["raw_source_uri"])
            prepared_path = local_uri(item["derived_uri"])
            require(raw_path.parent == source and raw_path not in raw_paths,
                    "Unexpected or repeated original source path.")
            require(prepared_path.parent == derived and prepared_path not in prepared_paths,
                    "Unexpected or repeated prepared source path.")
            original = read_input(raw_path)
            prepared = read_input(prepared_path)
            # Reuse the established parser/normalization and segment verifier.
            raw, regenerated, expected = speech_record(raw_path)
            require(raw == original, "Original changed during parsing.")
            for key, value in expected.items():
                require(item[key] == value, f"Ledger mismatch for {key}.")
            require(prepared == regenerated and digest(prepared) == frozen[prepared_path],
                    "Prepared corpus/manifest hash or segment preservation mismatch.")
            transcript = json.loads(original)["transcript"]
            from_segments = b"".join(
                prepared[s["derived_start"]:s["derived_start"] + s["length"]]
                for s in item["segments"][1:]
            )
            require(from_segments == transcript.encode("utf-8"),
                    "Ledger transcript is not byte-verbatim.")
            source_id, canonical = item["source_id"], item["speech_id"]
            require(KEY.fullmatch(source_id) and source_id not in source_ids,
                    "Invalid or duplicate source identity.")
            require(canonical_hashes.setdefault(canonical, item["transcript_hash"]) == item["transcript_hash"],
                    "Canonical identity has conflicting transcripts.")
            text = project(transcript)
            metadata = item["metadata"]
            base = {
                "source_id": source_id, "canonical_id": canonical,
                "title": metadata["title"], "speaker": metadata["president"],
                "date": metadata["date"], "transcript_sha256": item["transcript_hash"],
                "raw_sha256": item["raw_source_hash"], "projection": PROJECTION,
                "projection_sha256": digest(text.encode("utf-8")),
            }
            count = sum(1 for _ in chunks(text, size, overlap))
            correspondence = {
                **base, "raw_source_uri": raw_path.as_uri(),
                "prepared_uri": prepared_path.as_uri(),
                "prepared_sha256": item["derived_hash"],
                "original_metadata": item["original_metadata"],
                "original_transcript": transcript, "projection_text": text,
                "transcript_utf8_bytes": len(from_segments),
                "projection_characters": len(text), "chunk_count": count,
            }
            records.append({"base": base, "correspondence": correspondence})
            source_ids.add(source_id)
            raw_paths.add(raw_path)
            prepared_paths.add(prepared_path)
            duplicates.setdefault(canonical, []).append(source_id)
        except (OSError, ValueError, KeyError, TypeError, IndexError, UnicodeError) as error:
            raise ValueError(f"Source {offender}: {error}") from error
    require(raw_paths == set(inventory), "Ledger does not cover the original source inventory.")
    require(prepared_paths == set(frozen), "Ledger does not cover the frozen manifest.")
    require(len(canonical_hashes) == ledger["canonical_speech_count"] == expected_canonicals,
            "Canonical count mismatch.")
    duplicate_groups = {k: v for k, v in duplicates.items() if len(v) > 1}
    require(duplicate_groups == ledger["duplicate_source_groups"], "Ledger duplicate groups differ.")
    records.sort(key=lambda r: r["base"]["source_id"])
    destination.mkdir(parents=True)
    marker = destination / "INCOMPLETE"
    marker.write_bytes(b"Not publishable. Preparation/verification has not completed.\n")
    docs_path = destination / "docs.jsonl"
    correspondence_path = destination / "correspondence.jsonl"
    with docs_path.open("xb") as docs, correspondence_path.open("xb") as correspondence:
        for record in records:
            base = record["base"]
            for index, (start, end, snippet) in enumerate(chunks(
                    record["correspondence"]["projection_text"], size, overlap)):
                docs.write(encode({
                    **base, "uid": f"{base['source_id']}-{index:06d}",
                    "snippet": snippet, "chunk_index": index,
                    "projection_start": start, "projection_end": end,
                }))
            correspondence.write(encode(record["correspondence"]))
    catalog = [
        {"id": r["base"]["source_id"], **{k: r["base"][k] for k in (
            "title", "speaker", "date", "transcript_sha256", "canonical_id"
        )}}
        for r in records
    ]
    for name, value in (("catalog.json", catalog), ("topics.json", topics)):
        with (destination / name).open("xb") as handle:
            handle.write(encode(value))
        require(json.loads((destination / name).read_bytes()) == value,
                f"Artifact round trip failed: {name}")
    with correspondence_path.open("rb") as handle:
        for record in records:
            require(json.loads(handle.readline()) == record["correspondence"],
                    f"Correspondence round trip failed: {record['base']['source_id']}")
        require(not handle.read(), "Extra correspondence entries.")
    chunk_count = verify_documents(docs_path, records, size, overlap)
    require(chunk_count == sum(r["correspondence"]["chunk_count"] for r in records),
            "Chunk count mismatch.")
    require(sorted(source.glob("*.json")) == inventory, "Original source inventory changed.")
    for path, observed in inputs.items():
        require(digest(safe_path(path).read_bytes()) == observed["sha256"],
                f"Input changed during preparation: {path}")
    report = {
        "version": 1, "status": "complete", "azure_writes": False,
        "purpose": "Full-source search document preparation; NOT a validated semantic graph.",
        "publication_gate": "Parent/second-agent review required before Azure publication.",
        "source_count": len(records), "canonical_speech_count": len(canonical_hashes),
        "catalog_count": len(catalog), "chunk_count": chunk_count,
        "covered_source_count": len(source_ids), "source_coverage_percent": 100,
        "chunk_size_characters": size, "overlap_characters": overlap,
        "chunk_count_by_source": {r["base"]["source_id"]: r["correspondence"]["chunk_count"]
                                  for r in records},
        "projection": PROJECTION,
        "projection_policy": (
            "HTMLParser strips tags/comments/declarations, decodes character references "
            "in normal data, retains text data and adds newlines at block tags. No "
            "whitespace trimming or normalization. This is NOT byte-verbatim transcript "
            "text and NOT HTML sanitization for browser rendering. Original parsed "
            "transcript UTF-8 bytes are retained exactly in correspondence.jsonl "
            "original_transcript and verified against original JSON and segment ledger."
        ),
        "offset_policy": "Zero-based half-open Unicode character offsets into projection_text.",
        "hash_policy": "sha256:<lowercase hex>; text hashes are over UTF-8 encoded strings.",
        "index_schema": {
            **{k: "Edm.String" for k in (
                "uid", "source_id", "canonical_id", "title", "speaker", "date", "snippet",
                "transcript_sha256", "raw_sha256", "projection", "projection_sha256"
            )},
            **{k: "Edm.Int32" for k in ("chunk_index", "projection_start", "projection_end")},
        },
        "index_key": "uid", "searchable_fields": ["title", "speaker", "snippet"],
        "duplicate_source_groups": duplicate_groups,
        "topic_seed_count": len(topics["topics"]),
        "topic_seed_reference": str(lexicon_path),
        "authoritative_registry_reference": str(registry_path),
        "provenance_reference": str(ledger_path),
        "frozen_manifest_reference": str(manifest_path),
        "input_hashes_unchanged": True,
        "input_files": sorted(inputs.values(), key=lambda i: i["path"]),
        "input_inventory_sha256": digest(encode(sorted(inputs.values(), key=lambda i: i["path"]))),
        "artifacts": {
            name: {"sha256": digest((destination / name).read_bytes()),
                   "bytes": (destination / name).stat().st_size}
            for name in ("docs.jsonl", "catalog.json", "correspondence.jsonl", "topics.json")
        },
    }
    with (destination / "manifest.json").open("xb") as handle:
        handle.write(encode(report))
    marker.unlink()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=ROOT,
                        help="Project root containing the existing named run; ledger URIs remain authoritative.")
    parser.add_argument("--output", type=Path,
                        help=f"New directory (default: <input-root>/{OUTPUT}).")
    args = parser.parse_args()
    try:
        report = prepare(args.input_root, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Search preparation failed: {error}\n")
    print(json.dumps({k: report[k] for k in (
        "status", "source_count", "canonical_speech_count", "chunk_count",
        "source_coverage_percent", "input_hashes_unchanged", "publication_gate"
    )}, indent=2))


if __name__ == "__main__":
    main()
