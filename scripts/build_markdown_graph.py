#!/usr/bin/env python3
"""Accelerate turning any Markdown corpus into a class-scoped Azure AI Search graph.

Default execution prints an offline plan with zero writes or network calls.

Workflow:
  1. Offline preparation (--prepare):
     Parses Markdown files (frontmatter, H1, **Key**: Value headers), chunks text
     with overlap, emits deterministic metadata edges (via --metadata-map), and
     writes chunks.jsonl, edges.jsonl, and manifest.json with SHA-256 provenance.
  2. Controlled LLM extraction (--extract):
     Calls Microsoft Foundry v1 GA (/openai/v1/chat/completions) via Entra ID
     (AzureCliCredential; zero API keys) with strict JSON schema output, enforces
     ontology domain/range and class-scoped entity IDs, quarantines invalid rows
     to rejected_edges.jsonl, and checkpoints progress in extraction_cache.jsonl.
  3. Azure publication (--apply):
     Creates/verifies `{prefix}-chunks` and `{prefix}-graph` (graphProfile
     edgeList linked via provenanceIndexName) using `2026-11-01-preview`, uploads
     verified batches, and optionally runs a `search.graph` + hydrate smoke check.

Example (offline dry-run on local Markdown docs):
  python3 scripts/build_markdown_graph.py \\
    --input-dir documents \\
    --ontology .foundry-iq/fiq-speeches-9c65c09f/ontology/speeches-grounded-v7.jsonld \\
    --output-dir /tmp/my-graph-out \\
    --prefix mydocs
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx


SEARCH_API_VERSION = "2026-11-01-preview"
DEFAULT_CHUNK_SIZE = 4000
DEFAULT_OVERLAP = 500
DEFAULT_MAX_BATCH_DOCS = 100
DEFAULT_MAX_BATCH_BYTES = 8 * 1024 * 1024
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
PREFIX_RE = re.compile(r"[a-z][a-z0-9-]{1,38}[a-z0-9]")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$")
KV_LINE_RE = re.compile(r"^(?:\*\*([A-Za-z0-9 _-]{1,40})\*\*|([A-Za-z][A-Za-z0-9_-]{1,24})):\s+(.{1,200}?)\s*$")
SYSTEM_SYMLINKS = {Path("/var"), Path("/tmp"), Path("/etc")} if sys.platform == "darwin" else set()


class GraphBuildError(Exception):
    """Safe diagnostic error; never wraps secrets or raw upstream credentials."""


def sha256_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def normalize_label(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def slugify(value, max_len=32):
    cleaned = re.sub(r"[^a-z0-9]+", "-", normalize_label(value)).strip("-")
    return (cleaned or "entity")[:max_len]


def safe_path(value):
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise GraphBuildError(f"Paths must not contain traversal: {value}")
    absolute = raw if raw.is_absolute() else (Path.cwd() / raw)
    for parent in (absolute, *absolute.parents):
        if parent.is_symlink() and parent not in SYSTEM_SYMLINKS:
            raise GraphBuildError(f"Paths must not contain symlinks: {value}")
    return absolute.resolve()


def validate_azure_endpoint(url, suffixes, label):
    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not any(re.fullmatch(r"[a-z0-9][a-z0-9-]*" + re.escape(s), host) for s in suffixes)
    ):
        raise GraphBuildError(f"{label} must be a canonical Azure HTTPS resource endpoint.")
    return f"https://{host}"


def load_ontology(path):
    data = json.loads(safe_path(path).read_bytes())
    graph = data.get("@graph")
    if not isinstance(graph, list):
        raise GraphBuildError("Ontology JSON-LD must contain a @graph list.")
    classes = {}
    predicates = {}
    for node in graph:
        kind = node.get("@type")
        node_id = str(node.get("@id", ""))
        short = node_id.split(":")[-1].split("#")[-1].strip()
        if not short:
            continue
        if kind == "owl:Class":
            classes[short] = str(node.get("rdfs:comment", "")).strip()
        elif kind == "owl:ObjectProperty":
            domain = str((node.get("rdfs:domain") or {}).get("@id", "")).split(":")[-1].split("#")[-1].strip()
            range_ = str((node.get("rdfs:range") or {}).get("@id", "")).split(":")[-1].split("#")[-1].strip()
            if not domain or not range_:
                raise GraphBuildError(f"ObjectProperty {short} must declare rdfs:domain and rdfs:range.")
            predicates[short] = {
                "domain": domain,
                "range": range_,
                "comment": str(node.get("rdfs:comment", "")).strip(),
            }
    if not classes or not predicates:
        raise GraphBuildError("Ontology must define at least one owl:Class and one owl:ObjectProperty.")
    for pred, spec in predicates.items():
        if spec["domain"] not in classes or spec["range"] not in classes:
            raise GraphBuildError(f"ObjectProperty {pred} references an undeclared class.")
    return {"classes": classes, "predicates": predicates, "raw_sha256": sha256_bytes(safe_path(path).read_bytes())}


def load_entity_registry(path):
    """Build a strictly class-scoped (type, normalized_label) -> canonical_id lookup."""
    resolved = safe_path(path)
    raw = resolved.read_bytes()
    by_class_label = defaultdict(set)
    canonical_records = {}
    if resolved.suffix.lower() == ".csv":
        import csv
        import io
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
        for row in reader:
            eid = (row.get("id") or "").strip()
            name = (row.get("name") or "").strip()
            etype = (row.get("type") or "").strip()
            aliases = [a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()]
            if not eid or not name or not etype:
                continue
            canonical_records[eid] = {"id": eid, "name": name, "type": etype}
            for label in [name, *aliases]:
                norm = normalize_label(label)
                if norm:
                    by_class_label[(etype, norm)].add(eid)
    else:
        payload = json.loads(raw)
        rows = payload.get("topics") or payload.get("entities") or (payload if isinstance(payload, list) else [])
        for row in rows:
            name = (row.get("name") or "").strip()
            etype = (row.get("type") or ("Topic" if "topics" in payload else "")).strip()
            if not name or not etype:
                continue
            eid = (row.get("id") or f"{slugify(etype)}-{sha256_text(etype + ':' + normalize_label(name))[7:23]}")
            aliases = row.get("aliases") or []
            canonical_records[eid] = {"id": eid, "name": name, "type": etype}
            for label in [name, *aliases]:
                norm = normalize_label(label)
                if norm:
                    by_class_label[(etype, norm)].add(eid)
    return canonical_records, by_class_label


def parse_markdown_document(text, rel_path):
    """Extract optional frontmatter/header metadata and return (title, metadata, body)."""
    metadata = {}
    rest = text
    fm_match = FRONTMATTER_RE.match(rest)
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                k = key.strip().lower()
                v = val.strip().strip('"').strip("'")
                if k and v:
                    metadata[k] = v
        rest = rest[fm_match.end():]

    lines = rest.splitlines()
    title = metadata.get("title")
    body_start = 0
    for idx, line in enumerate(lines[:30]):
        stripped = line.strip()
        if not stripped:
            body_start = idx + 1
            continue
        if stripped == "**Transcript**":
            body_start = idx + 1
            break
        h1 = H1_RE.match(stripped)
        if h1:
            if not title:
                title = h1.group(1).strip()
            body_start = idx + 1
            continue
        kv = KV_LINE_RE.match(stripped)
        if kv:
            key = (kv.group(1) or kv.group(2)).strip().lower()
            val = kv.group(3).strip()
            if key and val and key not in metadata:
                metadata[key] = val
            body_start = idx + 1
            continue
        break

    body = "\n".join(lines[body_start:]).strip() or rest.strip()
    if not title:
        title = Path(rel_path).stem.replace("-", " ").replace("_", " ").strip() or rel_path
    return title, metadata, body


def chunk_text(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_OVERLAP):
    if chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise GraphBuildError("chunk_size must be positive and 0 <= overlap < chunk_size.")
    cleaned = text.strip()
    if not cleaned:
        return
    start = 0
    while True:
        end = min(start + chunk_size, len(cleaned))
        if end < len(cleaned):
            ws = cleaned.rfind(" ", start + (chunk_size // 2), end)
            nl = cleaned.rfind("\n", start + (chunk_size // 2), end)
            cut = max(ws, nl)
            if cut > start:
                end = cut
        snippet = cleaned[start:end].strip()
        if snippet:
            yield start, end, snippet
        if end >= len(cleaned):
            break
        start = max(start + 1, end - overlap)


def discover_markdown_files(input_dir, max_docs=None):
    root = safe_path(input_dir)
    if not root.is_dir():
        raise GraphBuildError(f"Input directory does not exist: {input_dir}")
    files = sorted(p for p in root.rglob("*.md") if p.is_file() and not p.is_symlink())
    if not files:
        raise GraphBuildError(f"No .md files found under {input_dir}")
    if max_docs is not None:
        if max_docs < 1:
            raise GraphBuildError("--max-docs must be >= 1.")
        files = files[:max_docs]
    return root, files


def resolve_entity_id(label, etype, doc_context, registry=None):
    """Resolve an entity ID strictly within its ontology class; never cross-class."""
    norm = normalize_label(label)
    if not norm:
        return None, label
    if doc_context and etype == doc_context["doc_class"]:
        if norm in {normalize_label(doc_context["title"]), normalize_label(doc_context["doc_id"])}:
            return doc_context["doc_id"], doc_context["title"]
    if registry is not None:
        canonical_records, by_class_label = registry
        matches = by_class_label.get((etype, norm), set())
        if len(matches) == 1:
            cid = next(iter(matches))
            return cid, canonical_records[cid]["name"]
        if len(matches) > 1:
            # Ambiguous alias within the same class: keep disambiguated deterministic ID
            return f"{slugify(etype)}-ambig-{sha256_text(etype + ':' + norm)[7:23]}", label.strip()
    return f"{slugify(etype)}-{sha256_text(etype + ':' + norm)[7:23]}", label.strip()


def make_edge_record(
    *,
    subject,
    subject_type,
    subject_id,
    predicate,
    obj,
    object_type,
    object_id,
    chunk_id,
    parent_id,
    graph_name,
    confidence=1.0,
    s_confidence=1.0,
    p_confidence=1.0,
    o_confidence=1.0,
    ordinal=0,
):
    fact_seed = f"{graph_name}|{subject_id}|{predicate}|{object_id}"
    fact_id = "fact-" + sha256_text(fact_seed)[7:31]
    edge_seed = f"{chunk_id}|{fact_id}"
    edge_id = f"{chunk_id}-{sha256_text(edge_seed)[7:23]}"
    return {
        "id": edge_id,
        "fact_id": fact_id,
        "subject": subject[:500],
        "predicate": predicate,
        "object": obj[:500],
        "object_string": obj[:500],
        "object_double": None,
        "object_bool": None,
        "object_datetime": None,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "object_type": object_type,
        "object_id": object_id,
        "confidence": round(float(confidence), 4),
        "subject_confidence": round(float(s_confidence), 4),
        "predicate_confidence": round(float(p_confidence), 4),
        "object_confidence": round(float(o_confidence), 4),
        "chunk_id": chunk_id,
        "parent_id": parent_id,
        "graph_name": graph_name,
        "subject_display_name": subject[:500],
        "object_display_name": obj[:500],
    }


def validate_and_build_edge(
    triple,
    *,
    ontology,
    doc_context,
    chunk_uid,
    graph_name,
    registry=None,
    ordinal=0,
):
    """Enforce ontology domain/range and class-scoped entity identity before emission."""
    predicate = str(triple.get("predicate") or "").strip()
    subject = str(triple.get("subject") or "").strip()
    subject_type = str(triple.get("subject_type") or "").strip()
    obj = str(triple.get("object") or "").strip()
    object_type = str(triple.get("object_type") or "").strip()

    if predicate not in ontology["predicates"]:
        return None, "undeclared_predicate"
    spec = ontology["predicates"][predicate]
    if (subject_type, object_type) != (spec["domain"], spec["range"]):
        return None, "ontology_domain_or_range_mismatch"
    if not subject or not obj:
        return None, "empty_endpoint_label"

    subject_id, canonical_subject = resolve_entity_id(subject, subject_type, doc_context, registry)
    object_id, canonical_object = resolve_entity_id(obj, object_type, doc_context, registry)
    if not subject_id or not object_id or subject_id == object_id:
        return None, "invalid_or_self_loop_identity"

    try:
        conf = float(triple.get("confidence", 0.85))
        if not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
            conf = 0.85
    except (TypeError, ValueError):
        conf = 0.85

    return make_edge_record(
        subject=canonical_subject,
        subject_type=subject_type,
        subject_id=subject_id,
        predicate=predicate,
        obj=canonical_object,
        object_type=object_type,
        object_id=object_id,
        chunk_id=chunk_uid,
        parent_id=doc_context["doc_id"],
        graph_name=graph_name,
        confidence=conf,
        s_confidence=conf,
        p_confidence=1.0,
        o_confidence=conf,
        ordinal=ordinal,
    ), None


def default_metadata_edges(doc_context, metadata, ontology, graph_name, first_chunk_uid, registry=None):
    """Emit deterministic edges when document metadata aligns with ontology properties."""
    edges = []
    doc_class = doc_context["doc_class"]
    doc_title = doc_context["title"]

    # Support common document-level metadata conventions if matching ontology predicates exist.
    candidates = [
        ("delivered", ("president", "speaker", "author"), "subject_field"),
        ("authoredBy", ("author", "writer"), "object_field"),
        ("givenOn", ("date", "published", "created"), "object_field"),
        ("publishedOn", ("date", "published"), "object_field"),
    ]
    for predicate, keys, direction in candidates:
        spec = ontology["predicates"].get(predicate)
        if not spec:
            continue
        val = next((metadata[k] for k in keys if metadata.get(k)), None)
        if not val:
            continue
        if direction == "subject_field" and spec["range"] == doc_class:
            triple = {
                "subject": val,
                "subject_type": spec["domain"],
                "predicate": predicate,
                "object": doc_title,
                "object_type": doc_class,
                "confidence": 1.0,
            }
        elif direction == "object_field" and spec["domain"] == doc_class:
            triple = {
                "subject": doc_title,
                "subject_type": doc_class,
                "predicate": predicate,
                "object": val,
                "object_type": spec["range"],
                "confidence": 1.0,
            }
        else:
            continue
        edge, _ = validate_and_build_edge(
            triple,
            ontology=ontology,
            doc_context=doc_context,
            chunk_uid=first_chunk_uid,
            graph_name=graph_name,
            registry=registry,
            ordinal=len(edges),
        )
        if edge:
            edges.append(edge)
    return edges


def build_extraction_prompt(ontology, doc_context, snippet, seed_hints=None):
    classes_desc = "\n".join(
        f"- {name}: {desc or 'Entity of type ' + name}"
        for name, desc in ontology["classes"].items()
    )
    preds_desc = "\n".join(
        f"- ({spec['domain']}) -[{name}]-> ({spec['range']}): {spec['comment'] or name}"
        for name, spec in ontology["predicates"].items()
    )
    hints_text = ""
    if seed_hints:
        hints_text = "\nPreferred canonical labels when applicable:\n" + ", ".join(seed_hints[:80])
    return (
        "Extract factual relationships explicitly supported by the passage below.\n"
        "Strict rules:\n"
        "1. Use ONLY the declared classes and predicates.\n"
        "2. Respect each predicate's exact (domain -> range) types.\n"
        f"3. When referencing the current document itself (class {doc_context['doc_class']}), "
        f"use its exact title: {json.dumps(doc_context['title'])}.\n"
        "4. Do not invent unstated relationships or merge different entity types.\n\n"
        f"Classes:\n{classes_desc}\n\nPredicates:\n{preds_desc}{hints_text}\n\n"
        f"Document metadata: title={json.dumps(doc_context['title'])}, "
        f"source_path={json.dumps(doc_context['source_path'])}\n\n"
        f"Passage:\n{snippet}"
    )


def extraction_response_schema(ontology):
    class_names = sorted(ontology["classes"])
    pred_names = sorted(ontology["predicates"])
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "graph_triples",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "triples": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "subject": {"type": "string"},
                                "subject_type": {"type": "string", "enum": class_names},
                                "predicate": {"type": "string", "enum": pred_names},
                                "object": {"type": "string"},
                                "object_type": {"type": "string", "enum": class_names},
                                "confidence": {"type": "number"},
                            },
                            "required": [
                                "subject",
                                "subject_type",
                                "predicate",
                                "object",
                                "object_type",
                                "confidence",
                            ],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["triples"],
                "additionalProperties": False,
            },
        },
    }


def prepare_corpus(
    *,
    input_dir,
    ontology_path,
    output_dir,
    prefix,
    doc_class=None,
    entities_path=None,
    chunk_size=DEFAULT_CHUNK_SIZE,
    overlap=DEFAULT_OVERLAP,
    max_docs=None,
):
    if not PREFIX_RE.fullmatch(prefix):
        raise GraphBuildError("--prefix must be 3-40 lowercase letters, digits, or dashes.")
    ontology = load_ontology(ontology_path)
    resolved_doc_class = doc_class or ("Speech" if "Speech" in ontology["classes"] else (
        "Document" if "Document" in ontology["classes"] else next(iter(ontology["classes"]))
    ))
    if resolved_doc_class not in ontology["classes"]:
        raise GraphBuildError(f"--doc-class {resolved_doc_class!r} is not declared in ontology.")

    registry = load_entity_registry(entities_path) if entities_path else None
    root, files = discover_markdown_files(input_dir, max_docs=max_docs)
    out = safe_path(output_dir)
    if out == root or out.is_relative_to(root) or root.is_relative_to(out):
        raise GraphBuildError("--output-dir must be outside --input-dir.")
    out.mkdir(parents=True, exist_ok=True)

    documents = []
    chunks = []
    metadata_edges = []
    seen_doc_ids = set()

    for path in files:
        rel_path = path.relative_to(root).as_posix()
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        title, metadata, body = parse_markdown_document(text, rel_path)
        doc_id = f"{slugify(resolved_doc_class)}-{sha256_text(rel_path)[7:23]}"
        if doc_id in seen_doc_ids:
            raise GraphBuildError(f"Duplicate document ID generated for {rel_path}")
        seen_doc_ids.add(doc_id)

        doc_context = {
            "doc_id": doc_id,
            "doc_class": resolved_doc_class,
            "title": title,
            "source_path": rel_path,
        }
        doc_chunks = list(chunk_text(body, chunk_size=chunk_size, overlap=overlap))
        if not doc_chunks:
            continue

        documents.append({
            "doc_id": doc_id,
            "doc_class": resolved_doc_class,
            "source_path": rel_path,
            "source_sha256": sha256_bytes(raw_bytes),
            "title": title,
            "metadata": metadata,
            "chunk_count": len(doc_chunks),
        })

        for idx, (start, end, snippet) in enumerate(doc_chunks):
            uid = f"{doc_id}-{idx:06d}"
            chunks.append({
                "uid": uid,
                "parent_id": doc_id,
                "source_path": rel_path,
                "title": title,
                "chunk_index": idx,
                "char_start": start,
                "char_end": end,
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
                "snippet": snippet,
            })

        metadata_edges.extend(
            default_metadata_edges(
                doc_context, metadata, ontology, prefix, f"{doc_id}-000000", registry=registry
            )
        )

    def write_jsonl(filename, rows):
        with (out / filename).open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    write_jsonl("documents.jsonl", documents)
    write_jsonl("chunks.jsonl", chunks)
    write_jsonl("metadata_edges.jsonl", metadata_edges)
    # Initialize edges.jsonl with deterministic metadata edges when no LLM pass runs yet.
    write_jsonl("edges.jsonl", metadata_edges)

    manifest = {
        "version": 1,
        "prefix": prefix,
        "graph_name": prefix,
        "doc_class": resolved_doc_class,
        "ontology_sha256": ontology["raw_sha256"],
        "document_count": len(documents),
        "chunk_count": len(chunks),
        "metadata_edge_count": len(metadata_edges),
        "chunk_index": f"{prefix}-chunks",
        "graph_index": f"{prefix}-graph",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest, ontology, registry


def extract_edges(
    *,
    output_dir,
    ontology,
    foundry_endpoint,
    chat_deployment,
    prefix,
    doc_class,
    registry=None,
    max_chunks=None,
    workers=2,
    credential=None,
    client=None,
):
    from azure.identity import AzureCliCredential

    if not PREFIX_RE.fullmatch(prefix):
        raise GraphBuildError("--prefix must be 3-40 lowercase letters, digits, or dashes.")
    if max_chunks is not None and max_chunks < 1:
        raise GraphBuildError("--max-chunks must be >= 1.")
    if not 1 <= workers <= 8:
        raise GraphBuildError("--workers must be between 1 and 8.")
    endpoint = validate_azure_endpoint(
        foundry_endpoint,
        (".cognitiveservices.azure.com", ".openai.azure.com", ".services.ai.azure.com"),
        "Foundry endpoint",
    )
    out = safe_path(output_dir)
    docs_by_id = {
        row["doc_id"]: row
        for row in (json.loads(line) for line in (out / "documents.jsonl").read_text(encoding="utf-8").splitlines() if line)
    }
    chunks = [
        json.loads(line)
        for line in (out / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    if max_chunks is not None:
        chunks = chunks[:max_chunks]

    cache_path = out / "extraction_cache.jsonl"
    cached = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            entry = json.loads(line)
            cached[(
                entry["chunk_uid"],
                entry.get("chunk_sha256"),
                entry.get("title"),
                entry["ontology_sha256"],
                entry["deployment"],
            )] = entry["triples"]

    seed_hints = (
        sorted({rec["name"] for rec in registry[0].values()})
        if registry is not None else None
    )
    schema = extraction_response_schema(ontology)
    cred = credential or AzureCliCredential(process_timeout=10)
    token_lock = threading.Lock()
    cache_lock = threading.Lock()
    token = None
    token_expires = 0.0

    def auth_header():
        nonlocal token, token_expires
        with token_lock:
            if token is None or time.time() >= token_expires - 120:
                try:
                    access = cred.get_token("https://ai.azure.com/.default")
                except Exception:
                    raise GraphBuildError("Azure CLI authentication failed for Foundry; check az login.") from None
                token = access.token
                token_expires = float(access.expires_on)
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    pending_chunks = []
    for chunk in chunks:
        doc = docs_by_id[chunk["parent_id"]]
        chunk_sha = sha256_text(chunk["snippet"])
        key = (chunk["uid"], chunk_sha, doc["title"], ontology["raw_sha256"], chat_deployment)
        if key not in cached:
            pending_chunks.append((chunk, doc, chunk_sha, key))

    def process_one(item, http_client):
        chunk, doc, chunk_sha, key = item
        doc_context = {
            "doc_id": doc["doc_id"],
            "doc_class": doc_class,
            "title": doc["title"],
            "source_path": doc["source_path"],
        }
        prompt = build_extraction_prompt(ontology, doc_context, chunk["snippet"], seed_hints)
        payload = {
            "model": chat_deployment,
            "messages": [
                {"role": "system", "content": "You are a precise ontology-constrained knowledge graph extractor."},
                {"role": "user", "content": prompt},
            ],
            "response_format": schema,
        }
        response = None
        for attempt in range(8):
            try:
                response = http_client.post(
                    f"{endpoint}/openai/v1/chat/completions",
                    headers=auth_header(),
                    json=payload,
                )
            except httpx.HTTPError:
                response = None
                if attempt < 7:
                    time.sleep(min(60.0, 2.0 ** attempt))
                    continue
                raise GraphBuildError("Foundry chat completion transport failed after retries.") from None
            if response.status_code not in RETRYABLE_STATUS:
                break
            retry_after = response.headers.get("Retry-After")
            wait = min(65.0, float(retry_after)) if retry_after and retry_after.isdigit() else min(60.0, 2.0 ** attempt)
            if attempt < 7:
                time.sleep(wait)
        if response is None or response.status_code != 200:
            code = response.status_code if response is not None else "no-response"
            raise GraphBuildError(f"Foundry chat completion failed with HTTP {code}.")
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("empty content")
            triples = json.loads(content).get("triples", [])
            if not isinstance(triples, list):
                raise ValueError("triples not a list")
        except (ValueError, KeyError, IndexError, TypeError):
            raise GraphBuildError("Foundry chat completion returned an unexpected response payload.") from None
        return chunk["uid"], chunk_sha, doc["title"], key, triples

    if pending_chunks:
        http_clients = [client] if client is not None else [
            httpx.Client(timeout=httpx.Timeout(90, connect=10), follow_redirects=False)
            for _ in range(workers)
        ]
        try:
            with cache_path.open("a", encoding="utf-8") as cache_file:
                if client is not None or workers == 1:
                    for idx, item in enumerate(pending_chunks, 1):
                        uid, chunk_sha, title, key, triples = process_one(item, http_clients[0])
                        cached[key] = triples
                        cache_file.write(json.dumps({
                            "chunk_uid": uid, "chunk_sha256": chunk_sha, "title": title,
                            "ontology_sha256": ontology["raw_sha256"], "deployment": chat_deployment,
                            "triples": triples,
                        }, ensure_ascii=False) + "\n")
                        cache_file.flush()
                        if idx % 25 == 0 or idx == len(pending_chunks):
                            print(f"Extracted {idx}/{len(pending_chunks)} pending chunks...", file=sys.stderr, flush=True)
                else:
                    local = threading.local()
                    pool_iter = iter(http_clients)
                    pool_lock = threading.Lock()

                    def worker_fn(item):
                        if not hasattr(local, "client"):
                            with pool_lock:
                                local.client = next(pool_iter)
                        return process_one(item, local.client)

                    completed = 0
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = [executor.submit(worker_fn, item) for item in pending_chunks]
                        for future in as_completed(futures):
                            uid, chunk_sha, title, key, triples = future.result()
                            with cache_lock:
                                cached[key] = triples
                                cache_file.write(json.dumps({
                                    "chunk_uid": uid, "chunk_sha256": chunk_sha, "title": title,
                                    "ontology_sha256": ontology["raw_sha256"], "deployment": chat_deployment,
                                    "triples": triples,
                                }, ensure_ascii=False) + "\n")
                                cache_file.flush()
                            completed += 1
                            if completed % 25 == 0 or completed == len(pending_chunks):
                                print(f"Extracted {completed}/{len(pending_chunks)} pending chunks...", file=sys.stderr, flush=True)
        finally:
            if client is None:
                for c in http_clients:
                    c.close()

    metadata_edges = [
        json.loads(line)
        for line in (out / "metadata_edges.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    accepted = []
    seen_facts = set()
    for edge in metadata_edges:
        fact_key = (edge["chunk_id"], edge["fact_id"])
        if fact_key not in seen_facts:
            seen_facts.add(fact_key)
            accepted.append(edge)
    rejected = []

    for chunk in chunks:
        doc = docs_by_id[chunk["parent_id"]]
        chunk_sha = sha256_text(chunk["snippet"])
        key = (chunk["uid"], chunk_sha, doc["title"], ontology["raw_sha256"], chat_deployment)
        triples = cached.get(key, [])
        doc_context = {
            "doc_id": doc["doc_id"],
            "doc_class": doc_class,
            "title": doc["title"],
            "source_path": doc["source_path"],
        }
        for idx, triple in enumerate(triples):
            edge, reason = validate_and_build_edge(
                triple,
                ontology=ontology,
                doc_context=doc_context,
                chunk_uid=chunk["uid"],
                graph_name=prefix,
                registry=registry,
                ordinal=idx,
            )
            if reason:
                rejected.append({"chunk_uid": chunk["uid"], "reason": reason, "triple": triple})
            else:
                fact_key = (edge["chunk_id"], edge["fact_id"])
                if fact_key not in seen_facts:
                    seen_facts.add(fact_key)
                    accepted.append(edge)

    with (out / "edges.jsonl").open("w", encoding="utf-8") as handle:
        for edge in accepted:
            handle.write(json.dumps(edge, ensure_ascii=False) + "\n")
    with (out / "rejected_edges.jsonl").open("w", encoding="utf-8") as handle:
        for row in rejected:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {"accepted_edges": len(accepted), "rejected_edges": len(rejected), "chunks_processed": len(chunks)}


def chunk_index_definition(prefix):
    return {
        "name": f"{prefix}-chunks",
        "fields": [
            {"name": "uid", "type": "Edm.String", "key": True, "searchable": False, "filterable": True, "sortable": True, "facetable": False, "retrievable": True},
            {"name": "parent_id", "type": "Edm.String", "searchable": False, "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
            {"name": "source_path", "type": "Edm.String", "searchable": True, "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
            {"name": "title", "type": "Edm.String", "searchable": True, "filterable": True, "sortable": False, "facetable": False, "retrievable": True},
            {"name": "chunk_index", "type": "Edm.Int32", "searchable": False, "filterable": True, "sortable": True, "facetable": False, "retrievable": True},
            {"name": "char_start", "type": "Edm.Int32", "searchable": False, "filterable": False, "sortable": False, "facetable": False, "retrievable": True},
            {"name": "char_end", "type": "Edm.Int32", "searchable": False, "filterable": False, "sortable": False, "facetable": False, "retrievable": True},
            {"name": "metadata_json", "type": "Edm.String", "searchable": False, "filterable": False, "sortable": False, "facetable": False, "retrievable": True},
            {"name": "snippet", "type": "Edm.String", "searchable": True, "filterable": False, "sortable": False, "facetable": False, "retrievable": True, "analyzer": "en.lucene"},
        ],
        "similarity": {"@odata.type": "#Microsoft.Azure.Search.BM25Similarity", "b": 0.75, "k1": 1.2},
        "semantic": {
            "defaultConfiguration": "default-semantic",
            "configurations": [{
                "name": "default-semantic",
                "rankingOrder": "RerankerScore",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [{"fieldName": "snippet"}],
                    "prioritizedKeywordsFields": [{"fieldName": "source_path"}],
                },
            }],
        },
    }


def graph_index_definition(prefix):
    return {
        "name": f"{prefix}-graph",
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True, "retrievable": True, "searchable": False, "filterable": True, "sortable": True, "facetable": False},
            {"name": "fact_id", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": False},
            {"name": "subject", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": True, "facetable": True},
            {"name": "predicate", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": True},
            {"name": "object", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": True, "facetable": True},
            {"name": "object_string", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": True},
            {"name": "object_double", "type": "Edm.Double", "retrievable": True, "filterable": True, "sortable": True, "facetable": True},
            {"name": "object_bool", "type": "Edm.Boolean", "retrievable": True, "filterable": True},
            {"name": "object_datetime", "type": "Edm.DateTimeOffset", "retrievable": True, "filterable": True},
            {"name": "subject_type", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": True},
            {"name": "subject_id", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": True, "facetable": True},
            {"name": "object_type", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": True},
            {"name": "object_id", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": True, "facetable": True},
            {"name": "graph_name", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": True, "facetable": True},
            {"name": "confidence", "type": "Edm.Double", "retrievable": True},
            {"name": "subject_confidence", "type": "Edm.Double", "retrievable": True},
            {"name": "predicate_confidence", "type": "Edm.Double", "retrievable": True},
            {"name": "object_confidence", "type": "Edm.Double", "retrievable": True},
            {"name": "chunk_id", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": False},
            {"name": "parent_id", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": True, "sortable": False, "facetable": False},
            {"name": "subject_display_name", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": False, "sortable": False, "facetable": False},
            {"name": "object_display_name", "type": "Edm.String", "retrievable": True, "searchable": False, "filterable": False, "sortable": False, "facetable": False},
        ],
        "graphProfile": {
            "kind": "edgeList",
            "fields": {
                "assertionIdField": "id",
                "factIdField": "fact_id",
                "subjectField": "subject",
                "predicateField": "predicate",
                "objectField": "object",
                "objectStringField": "object_string",
                "objectDoubleField": "object_double",
                "objectBoolField": "object_bool",
                "objectDateTimeField": "object_datetime",
                "subjectTypeField": "subject_type",
                "subjectIdField": "subject_id",
                "objectTypeField": "object_type",
                "objectIdField": "object_id",
                "confidenceField": "confidence",
                "subjectConfidenceField": "subject_confidence",
                "predicateConfidenceField": "predicate_confidence",
                "objectConfidenceField": "object_confidence",
                "sourceChunkIdField": "chunk_id",
                "sourceParentIdField": "parent_id",
                "graphNameField": "graph_name",
                "subjectIdDisplayNameField": "subject_display_name",
                "objectIdDisplayNameField": "object_display_name",
            },
            "provenanceIndexName": f"{prefix}-chunks",
        },
    }


def upload_batches(docs, max_docs=DEFAULT_MAX_BATCH_DOCS, max_bytes=DEFAULT_MAX_BATCH_BYTES):
    batch = []
    size = len(b'{"value":[]}')
    for doc in docs:
        action = {"@search.action": "mergeOrUpload", **doc}
        encoded_len = len(json.dumps(action, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if batch and (len(batch) >= max_docs or size + encoded_len + 1 > max_bytes):
            yield batch
            batch = []
            size = len(b'{"value":[]}')
        batch.append(action)
        size += encoded_len + (1 if len(batch) > 1 else 0)
    if batch:
        yield batch


def publish_to_search(*, output_dir, search_endpoint, prefix, verify_gql=False, ontology=None, credential=None, client=None):
    from azure.identity import AzureCliCredential

    if not PREFIX_RE.fullmatch(prefix):
        raise GraphBuildError("--prefix must be 3-40 lowercase letters, digits, or dashes.")
    endpoint = validate_azure_endpoint(search_endpoint, (".search.windows.net",), "Search endpoint")
    out = safe_path(output_dir)
    chunks = [json.loads(l) for l in (out / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l]
    edges = [json.loads(l) for l in (out / "edges.jsonl").read_text(encoding="utf-8").splitlines() if l]

    cred = credential or AzureCliCredential(process_timeout=10)
    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False)
    owns_client = client is None
    token = None
    expires = 0.0

    def search_req(method, path, body=None):
        nonlocal token, expires
        if token is None or time.time() >= expires - 120:
            try:
                access = cred.get_token("https://search.azure.com/.default")
            except Exception:
                raise GraphBuildError("Azure CLI authentication failed for Search; check az login.") from None
            token = access.token
            expires = float(access.expires_on)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        for attempt in range(6):
            try:
                resp = http.request(
                    method,
                    f"{endpoint}/{path}",
                    params={"api-version": SEARCH_API_VERSION},
                    headers=headers,
                    json=body,
                )
            except httpx.HTTPError:
                if attempt < 5:
                    time.sleep(min(20.0, 2.0 ** attempt))
                    continue
                raise GraphBuildError(f"Search {method} {path} transport failed.") from None
            if resp.status_code not in RETRYABLE_STATUS:
                return resp
            if attempt < 5:
                time.sleep(min(20.0, 2.0 ** attempt))
        raise GraphBuildError(f"Search {method} {path} exhausted retries.")

    try:
        for index_def in (chunk_index_definition(prefix), graph_index_definition(prefix)):
            resp = search_req("PUT", f"indexes('{index_def['name']}')", index_def)
            if resp.status_code not in (200, 201):
                raise GraphBuildError(f"Failed creating/updating index {index_def['name']}: HTTP {resp.status_code}")

        for index_name, key_field, rows in ((f"{prefix}-chunks", "uid", chunks), (f"{prefix}-graph", "id", edges)):
            for batch in upload_batches(rows):
                pending = batch
                for attempt in range(6):
                    resp = search_req("POST", f"indexes('{index_name}')/docs/index", {"value": pending})
                    if resp.status_code not in (200, 207):
                        raise GraphBuildError(f"Upload to {index_name} failed: HTTP {resp.status_code}")
                    results = resp.json().get("value", [])
                    failed_keys = {r.get("key") for r in results if not r.get("status")}
                    if not failed_keys:
                        pending = []
                        break
                    pending = [item for item in pending if item[key_field] in failed_keys]
                    if not pending:
                        break
                    if attempt < 5:
                        time.sleep(min(20.0, 2.0 ** attempt))
                if pending:
                    raise GraphBuildError(f"{len(pending)} document(s) failed indexing in {index_name}.")

        verification = None
        if verify_gql:
            if ontology and ontology.get("predicates"):
                pred = next(iter(ontology["predicates"]))
                spec = ontology["predicates"][pred]
                gql_query = (
                    f"MATCH (s:{spec['domain']})-[r:{pred}]->(o:{spec['range']}) "
                    "RETURN s.name AS subject, r.predicate AS predicate, o.name AS object LIMIT 5"
                )
            else:
                gql_query = (
                    "MATCH (s:Document)-[r]->(o) "
                    "RETURN s.name AS subject, r.predicate AS predicate, o.name AS object LIMIT 5"
                )
            resp = search_req(
                "POST",
                f"indexes('{prefix}-graph')/docs/search.graph",
                {
                    "gql": gql_query,
                    "filter": f"graph_name eq '{prefix}'",
                    "includeStatistics": True,
                    "provenance": {
                        "kind": "hydrate",
                        "maxEvidencePerRow": 3,
                        "hydrateDocuments": {"select": "uid,title,source_path,snippet"},
                    },
                },
            )
            if resp.status_code != 200:
                raise GraphBuildError(f"GQL verification failed: HTTP {resp.status_code}")
            verification = {
                "row_count": len(resp.json().get("value", [])),
                "hydrated_docs": len(resp.json().get("@search.graphProvenanceDocuments", {})),
            }
        return {"chunks_uploaded": len(chunks), "edges_uploaded": len(edges), "gql_verification": verification}
    finally:
        if owns_client:
            http.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", required=True, help="Directory of customer Markdown (.md) files.")
    parser.add_argument("--ontology", required=True, help="Path to OWL/JSON-LD ontology file.")
    parser.add_argument("--output-dir", required=True, help="Output directory for prepared chunks/edges.")
    parser.add_argument("--prefix", required=True, help="Index/graph prefix (3-40 lowercase letters/digits/dashes).")
    parser.add_argument("--doc-class", help="Ontology class representing each Markdown document (auto-detected if omitted).")
    parser.add_argument("--entities", help="Optional CSV or JSON canonical entity/topic seed registry.")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--max-docs", type=int, help="Optional cap on Markdown files for quick pilots.")
    parser.add_argument("--max-chunks", type=int, help="Optional cap on chunks sent to LLM extraction.")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent LLM extraction workers (1-8, default: 2).")
    parser.add_argument("--prepare", action="store_true", help="Write local chunks, metadata edges, and manifest.")
    parser.add_argument("--extract", action="store_true", help="Run Foundry v1 LLM triple extraction with class validation.")
    parser.add_argument("--apply", action="store_true", help="Create/update Azure AI Search chunks + graphProfile indexes and upload.")
    parser.add_argument("--verify-gql", action="store_true", help="Run one smoke search.graph + hydrate query after --apply.")
    parser.add_argument("--foundry-endpoint", help="Azure AI Foundry endpoint (required with --extract).")
    parser.add_argument("--chat-deployment", default="chat", help="Foundry chat deployment name (default: chat).")
    parser.add_argument("--search-endpoint", help="Azure AI Search endpoint (required with --apply).")
    args = parser.parse_args(argv)

    try:
        if not PREFIX_RE.fullmatch(args.prefix):
            raise GraphBuildError("--prefix must be 3-40 lowercase letters, digits, or dashes.")
        ontology = load_ontology(args.ontology)
        _, files = discover_markdown_files(args.input_dir, max_docs=args.max_docs)
        plan = {
            "mode": "execute" if (args.prepare or args.extract or args.apply) else "offline-dry-run",
            "input_dir": str(safe_path(args.input_dir)),
            "markdown_files_discovered": len(files),
            "ontology_classes": sorted(ontology["classes"]),
            "ontology_predicates": sorted(ontology["predicates"]),
            "output_dir": str(safe_path(args.output_dir)),
            "planned_indexes": {
                "chunks": f"{args.prefix}-chunks",
                "graph": f"{args.prefix}-graph",
                "search_api_version": SEARCH_API_VERSION,
            },
            "actions": {
                "prepare_local": bool(args.prepare or args.extract or args.apply),
                "llm_extract": bool(args.extract),
                "azure_publish": bool(args.apply),
            },
        }
        print(json.dumps(plan, indent=2))

        if not (args.prepare or args.extract or args.apply):
            print("Dry-run only. Add --prepare for local chunking, --extract for LLM triples, or --apply to publish.")
            return 0

        if args.extract and not args.foundry_endpoint:
            raise GraphBuildError("--extract requires --foundry-endpoint.")
        if args.apply and not args.search_endpoint:
            raise GraphBuildError("--apply requires --search-endpoint.")

        out_path = safe_path(args.output_dir)
        manifest_file = out_path / "manifest.json"
        required_files = ("manifest.json", "documents.jsonl", "chunks.jsonl", "metadata_edges.jsonl", "edges.jsonl")
        need_prepare = args.prepare or not all((out_path / name).exists() for name in required_files)
        result = {}
        if need_prepare:
            manifest, ontology, registry = prepare_corpus(
                input_dir=args.input_dir,
                ontology_path=args.ontology,
                output_dir=args.output_dir,
                prefix=args.prefix,
                doc_class=args.doc_class,
                entities_path=args.entities,
                chunk_size=args.chunk_size,
                overlap=args.overlap,
                max_docs=args.max_docs,
            )
            result["prepare"] = manifest
        else:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if manifest.get("ontology_sha256") != ontology["raw_sha256"]:
                raise GraphBuildError("Existing output_dir manifest uses a different ontology; pass --prepare to rebuild.")
            if manifest.get("prefix") != args.prefix:
                raise GraphBuildError("Existing output_dir manifest uses a different --prefix; pass --prepare to rebuild.")
            if args.doc_class and manifest.get("doc_class") != args.doc_class:
                raise GraphBuildError("Existing output_dir manifest uses a different --doc-class; pass --prepare to rebuild.")
            registry = load_entity_registry(args.entities) if args.entities else None
            result["prepare"] = {"reused_existing": True, **manifest}

        if args.extract:
            result["extract"] = extract_edges(
                output_dir=args.output_dir,
                ontology=ontology,
                foundry_endpoint=args.foundry_endpoint,
                chat_deployment=args.chat_deployment,
                prefix=args.prefix,
                doc_class=manifest["doc_class"],
                registry=registry,
                max_chunks=args.max_chunks,
                workers=args.workers,
            )

        if args.apply:
            result["publish"] = publish_to_search(
                output_dir=args.output_dir,
                search_endpoint=args.search_endpoint,
                prefix=args.prefix,
                verify_gql=args.verify_gql,
                ontology=ontology,
            )

        print(json.dumps(result, indent=2))
        return 0
    except (GraphBuildError, OSError, ValueError, KeyError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
