#!/usr/bin/env python3
"""Emit an entity list for the supported grounding loader; never call Azure."""

import argparse
import csv
import json
import unicodedata

from prepare_speech_graph_corpus import digest, safe_path


def normalized(value):
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00|;"):
        raise ValueError("Names and aliases must be single, unambiguous labels.")
    value = unicodedata.normalize("NFC", value.strip())
    if not value:
        raise ValueError("Names and aliases must not be empty.")
    return value


def registry(provenance, lexicon):
    entities, labels = {}, {}

    def add(kind, name, aliases=(), identifier=None):
        name = normalized(name)
        identifier = identifier or (
            kind.lower() + "-" + digest(name.casefold().encode("utf-8"))[7:]
        )
        values = {normalized(a) for a in (name, *aliases)}
        values.update({v.lower() for v in tuple(values)})
        for value in values:
            owner = labels.setdefault(value.casefold(), identifier)
            if owner != identifier:
                raise ValueError(f"Ambiguous grounding alias: {value!r}.")
        record = entities.setdefault(
            identifier, {"id": identifier, "name": name, "type": kind, "aliases": set()}
        )
        if record["type"] != kind or record["name"] != name:
            raise ValueError("Conflicting canonical entity identity.")
        record["aliases"].update(values - {name})

    if provenance["status"] != "complete":
        raise ValueError("Only a complete preparation ledger is supported.")
    for item in provenance["items"]:
        metadata = item["metadata"]
        add("Person", metadata["president"])
        add("SpeechDate", metadata["date"])
        add(
            "Speech", metadata["title"],
            [item["original_metadata"]["title"]], item["speech_id"],
        )
    for topic in lexicon["topics"]:
        add("Topic", topic["name"], topic["aliases"])
    return [
        {**record, "aliases": "|".join(sorted(record["aliases"]))}
        for _, record in sorted(entities.items())
    ]


def write_registry(provenance, lexicon, output):
    output = safe_path(output)
    roots = [safe_path(provenance[k]) for k in ("source_reference", "derived_reference")]
    if output.exists() or any(output.is_relative_to(root) for root in roots):
        raise ValueError("Use a new entity-list output outside the source corpora.")
    rows = registry(provenance, lexicon)
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "name", "type", "aliases"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--lexicon", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    provenance, lexicon, output = map(
        safe_path, (args.provenance, args.lexicon, args.output)
    )
    try:
        if output.exists() or output in (provenance, lexicon):
            raise ValueError("Use a new entity-list output.")
        rows = write_registry(
            json.loads(provenance.read_bytes()), json.loads(lexicon.read_bytes()), output
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Grounding preparation failed: {error}\n")
    print(json.dumps({
        "entity_count": len(rows), "azure_writes": False,
        "format": "entity_list", "output": str(output),
        "type_counts": {kind: sum(r["type"] == kind for r in rows)
                        for kind in sorted({r["type"] for r in rows})},
    }, indent=2))


if __name__ == "__main__":
    main()
