#!/usr/bin/env python3
"""Audit frozen speech exports locally; never mutate or publish an Azure graph."""

import argparse
import csv
import html
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

from prepare_speech_graph_corpus import digest, safe_path


def normalized(value):
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def alias_index(rows):
    entities, aliases = {}, defaultdict(set)
    for row in rows:
        if row["id"] in entities:
            raise ValueError("Duplicate registry identity.")
        entities[row["id"]] = row
        for label in [row["name"], *row["aliases"].split("|")]:
            if label.strip():
                aliases[(row["type"], normalized(label))].add(row["id"])
    return entities, aliases


def plain_text(text):
    return html.unescape(re.sub(r"<[^>]*>", " ", text))


def literal_support(label, transcript):
    needle = normalized(label)
    haystack = normalized(plain_text(transcript))
    return bool(needle and re.search(
        r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack
    ))


def classify(edge, source, entities, aliases, relations, transcript):
    predicate = edge["predicate"]
    if predicate not in relations:
        return "rejected", "undeclared_predicate", None
    if (edge["subject_type"], edge["object_type"]) != relations[predicate]:
        return "rejected", "ontology_domain_or_range_mismatch", None
    for end in ("subject", "object"):
        known = entities.get(edge[end + "_id"])
        if known and known["type"] != edge[end + "_type"]:
            return "rejected", "cross_class_identity", None
    speech_end = "object" if predicate == "delivered" else "subject"
    if edge[speech_end + "_id"] != source["speech_id"]:
        return "rejected", "wrong_source_speech_identity", None
    mappings = {}
    for end in ("subject", "object"):
        matches = aliases.get((edge[end + "_type"], normalized(edge[end])), set())
        if len(matches) > 1:
            return "unresolved", "ambiguous_explicit_alias", None
        if not matches:
            return "unresolved", "no_explicit_class_scoped_alias", None
        identifier = next(iter(matches))
        if identifier != edge[end + "_id"]:
            return "rejected", "identity_disagrees_with_explicit_alias", None
        mappings[end] = identifier
    if predicate in ("delivered", "givenOn"):
        end = "subject" if predicate == "delivered" else "object"
        field = "president" if predicate == "delivered" else "date"
        if normalized(entities[mappings[end]]["name"]) != normalized(
            source["metadata"][field]
        ):
            return "rejected", "source_metadata_disagreement", None
        return "accepted", "extractor_metadata_matches_verified_ledger", mappings
    if not literal_support(edge["object"], transcript):
        return "unresolved", "no_literal_transcript_support", mappings
    # A literal occurrence proves neither substantive discussion nor identity.
    return "unresolved", "literal_support_requires_semantic_adjudication", mappings


def local_file(uri, root):
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise ValueError("Expected a local file URI.")
    path = safe_path(unquote(parsed.path))
    if not path.is_relative_to(root):
        raise ValueError("Source path escaped its approved corpus.")
    return path


def prototype(provenance, manifest, registry, ontology, content, edges, selection):
    entities, aliases = alias_index(registry)
    relations = {
        row["@id"].split(":")[-1]: (
            row["rdfs:domain"]["@id"].split(":")[-1],
            row["rdfs:range"]["@id"].split(":")[-1],
        )
        for row in ontology["@graph"] if row["@type"] == "owl:ObjectProperty"
    }
    if provenance["status"] != "complete" or len(selection) != len(set(selection)):
        raise ValueError("Incomplete ledger or duplicate selected sources.")
    selected = set(selection)
    by_name = {row["filename"]: row for row in provenance["items"]}
    sources, transcripts = {}, {}
    raw_root = safe_path(provenance["source_reference"])
    derived_root = safe_path(provenance["derived_reference"])
    for item in manifest["items"]:
        if item["source_id"] not in selected:
            continue
        derived_path = local_file(item["original_uri"], derived_root)
        row = by_name[derived_path.name]
        raw = local_file(row["raw_source_uri"], raw_root).read_bytes()
        derived = derived_path.read_bytes()
        transcript = json.loads(raw)["transcript"]
        if (digest(raw) != row["raw_source_hash"]
                or digest(derived) != row["derived_hash"]
                or digest(derived) != item["version_or_hash"]
                or digest(transcript.encode()) != row["transcript_hash"]):
            raise ValueError("Source hash mismatch.")
        restored = b"".join(
            derived[s["derived_start"]:s["derived_start"] + s["length"]]
            for s in row["segments"][1:]
        )
        if restored != transcript.encode():
            raise ValueError("Parsed transcript byte preservation failed.")
        if item["source_id"] in sources:
            raise ValueError("Duplicate manifest identity.")
        sources[item["source_id"]] = row
        transcripts[item["source_id"]] = transcript
    if set(sources) != selected:
        raise ValueError("Selection does not match manifest.")
    chunks, parents = {}, {}
    prefix = "/" + selection.container + "/" + selection.staging_prefix + "/"
    for chunk in content:
        parsed = urlparse(chunk["blob_url"])
        source_id = Path(unquote(parsed.path)).stem
        if (parsed.scheme != "https" or parsed.netloc != selection.storage_host
                or source_id not in selected
                or parsed.path != prefix + source_id + ".md"
                or parsed.query or parsed.fragment
                or chunk["uid"] in chunks):
            raise ValueError("Unexpected chunk source or duplicate chunk.")
        parent = chunk["snippet_parent_id"]
        if parent in parents and parents[parent] != source_id:
            raise ValueError("Conflicting chunk parent.")
        chunks[chunk["uid"]] = chunk
        parents[parent] = source_id
    if set(parents.values()) != selected:
        raise ValueError("Missing selected source chunks.")
    results = {kind: [] for kind in ("accepted", "rejected", "unresolved")}
    seen = set()
    for edge in edges:
        chunk = chunks.get(edge["chunk_id"])
        if (edge["id"] in seen or chunk is None
                or chunk["snippet_parent_id"] != edge["parent_id"]
                or edge["graph_name"] != selection.graph_name):
            raise ValueError("Invalid graph provenance or duplicate row.")
        seen.add(edge["id"])
        source_id = parents[edge["parent_id"]]
        source = sources[source_id]
        category, reason, mappings = classify(
            edge, source, entities, aliases, relations, transcripts[source_id]
        )
        results[category].append({
            "original_edge": edge, "reason": reason,
            "explicit_alias_candidates": mappings,
            "provenance": {
                "manifest_source_id": source_id,
                "raw_source_uri": source["raw_source_uri"],
                "raw_source_hash": source["raw_source_hash"],
                "transcript_hash": source["transcript_hash"],
                "derived_hash": source["derived_hash"],
                "chunk_id": edge["chunk_id"],
                "chunk_snippet_hash": digest(chunk["snippet"].encode()),
                "evidence_scope": "source transcript, not chunk-level entailment",
                "chunk_to_derived_text_correspondence": "unverified",
            },
        })
    ledger_assertions = []
    for source_id, source in sorted(sources.items()):
        speech = entities.get(source["speech_id"])
        if (speech is None or speech["type"] != "Speech"
                or normalized(speech["name"]) != normalized(source["metadata"]["title"])):
            raise ValueError("Canonical speech disagrees with source ledger.")
        for predicate, kind, field in (
            ("delivered", "Person", "president"),
            ("givenOn", "SpeechDate", "date"),
        ):
            matches = aliases.get((kind, normalized(source["metadata"][field])), set())
            if len(matches) != 1:
                raise ValueError("Ambiguous or missing ledger metadata identity.")
            identifier = next(iter(matches))
            ledger_assertions.append({
                "origin": "verified_source_metadata_not_extractor",
                "predicate": predicate,
                "subject_id": identifier if predicate == "delivered" else source["speech_id"],
                "object_id": source["speech_id"] if predicate == "delivered" else identifier,
                "manifest_source_id": source_id,
                "raw_source_uri": source["raw_source_uri"],
                "raw_source_hash": source["raw_source_hash"],
                "metadata": source["metadata"],
            })
    results["ledger_derived"] = ledger_assertions
    results["summary"] = {
        "status": "local_feasibility_only_not_publishable",
        "azure_writes": False, "model_calls": 0,
        "selected_sources": len(selected), "input_graph_rows": len(edges),
        "counts": {kind: len(rows) for kind, rows in results.items()},
        "reason_counts": dict(Counter(
            row["reason"] for kind in ("accepted", "rejected", "unresolved")
            for row in results[kind]
        )),
        "accepted_scope": "Only extractor metadata corroborated by the verified ledger",
        "semantic_consistency_proven": False,
        "limitations": [
            "No graph row was repaired or deployed; original IDs and labels are retained.",
            "Exact aliases do not disambiguate homonyms or prove topic equivalence.",
            "All content relationships require semantic adjudication; lexical support is not entailment.",
            "Chunk IDs and exact staged URIs are checked; snippet-to-source text correspondence is unverified.",
            "Registry coverage is limited; missing aliases are not extraction failures.",
        ],
    }
    return results


class Selection(list):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("provenance", "manifest", "registry", "ontology", "content", "edges",
                 "plan", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        paths = {name: safe_path(value) for name, value in vars(args).items()}
        loaded = {}
        hashes = {}
        for name, path in paths.items():
            if name == "output":
                continue
            data = path.read_bytes()
            hashes[name] = digest(data)
            loaded[name] = (
                list(csv.DictReader(data.decode().splitlines())) if name == "registry"
                else json.loads(data)
            )
        provenance, plan = loaded["provenance"], loaded.pop("plan")
        output = paths["output"]
        protected = [safe_path(provenance[key]) for key in
                     ("source_reference", "derived_reference")]
        if output.exists() or any(output.is_relative_to(root) for root in protected):
            raise ValueError("Use a new output directory outside both source corpora.")
        selection = Selection(plan["scope"]["trial"]["source_ids"])
        selection.staging_prefix = plan["scope"]["staging_prefix"]
        selection.graph_name = plan["grounding"]["graph_name"]
        selection.storage_host = urlparse(plan["scope"]["storage_endpoint"]).netloc
        selection.container = plan["scope"]["storage_container"]
        loaded["manifest"] = loaded["manifest"]["manifest"]
        result = prototype(**loaded, selection=selection)
        result["summary"]["frozen_input_hashes"] = hashes
        output.mkdir()
        for name, value in result.items():
            (output / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")
        print(json.dumps(result["summary"], indent=2))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Local canonicalization failed: {error}\n")


if __name__ == "__main__":
    main()
