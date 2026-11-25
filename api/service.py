"""Bounded, keyless retrieval with evidence-scoped graph views."""

import asyncio
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

import httpx


DATASET = os.environ.get("SPEECH_DATASET", "full")
if DATASET not in ("full", "sample"):
    raise ValueError("SPEECH_DATASET must be full or sample.")
CONFIG = json.loads(Path(__file__).with_name(
    "corpus.json" if DATASET == "full" else "sample.json"
).read_text())
FULL_CORPUS = DATASET == "full"
if FULL_CORPUS and (
    CONFIG.get("coverage_verified") is not True
    or CONFIG.get("source_count") != len(CONFIG["speeches"])
    or len({row["id"] for row in CONFIG["speeches"]}) != CONFIG["source_count"]
):
    raise ValueError("Full-corpus publication must be verified before serving.")
COVERAGE = {
    "label": "Full presidential speech corpus" if FULL_CORPUS else "Ten-speech sample",
    "source_count": len(CONFIG["speeches"]),
    "full_corpus_available": FULL_CORPUS,
}
if FULL_CORPUS:
    COVERAGE.update(canonical_count=CONFIG["canonical_count"], chunk_count=CONFIG["chunk_count"])
WARNINGS = ([
    "All source records are indexed; each answer uses a bounded selection, not an exhaustive corpus comparison.",
    "The graph shows canonical speeches, cited passages and case-insensitive whole-word topic phrase matches, not validated semantic relationships.",
    "Passages are readable HTML-to-text projections; original transcripts remain unchanged.",
] if FULL_CORPUS else [
    "Only the published ten-speech sample is connected, not the full 1,060 documents.",
    "Extracted graph relationships may contain errors; speaker links are incomplete.",
    "Graph relationships share cited passages; they do not prove every answer claim.",
])
MAX_QUESTION = 2000
MAX_EDGES = 60
MAX_REFERENCES = 20
KEY = re.compile(r"[A-Za-z0-9_=\-]{1,2048}\Z")
PREDICATES = {
    "delivered": ("Person", "Speech"),
    "givenOn": ("Speech", "SpeechDate"),
    "addressesTopic": ("Speech", "Topic"),
    "mentionsPerson": ("Speech", "Person"),
    "mentionsOrganization": ("Speech", "Organization"),
    "mentionsPlace": ("Speech", "Place"),
}
CATALOG = {(row["id"] if FULL_CORPUS else CONFIG["blob_prefix"] + row["id"] + ".md"): row
           for row in CONFIG["speeches"]}


class ServiceError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def question_from(body):
    if not isinstance(body, dict) or set(body) != {"question"}:
        raise ValueError("Send a JSON object containing only question.")
    question = body["question"]
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= MAX_QUESTION:
        raise ValueError("Question must contain 1 to 2,000 characters.")
    if any(ord(c) < 32 and c not in "\n\t\r" for c in question):
        raise ValueError("Question contains unsupported control characters.")
    return question.strip()


def cited_references(raw, answer):
    markers = set(re.findall(r"\[ref_id:([A-Za-z0-9_-]+)\]", answer))
    selected, warnings, seen = [], [], set()
    references = raw.get("references") or []
    for ref in references:
        identifier = str(ref.get("id", ""))
        if identifier not in markers or identifier in seen:
            continue
        seen.add(identifier)
        data = ref.get("sourceData") or {}
        if not isinstance(data, dict):
            continue
        key = data.get("uid") or ref.get("docKey")
        if not isinstance(key, str) or not KEY.fullmatch(key):
            continue
        selected.append((identifier, key))
        if len(selected) == MAX_REFERENCES:
            break
    mapped = {identifier for identifier, _ in selected}
    if markers - mapped:
        warnings.append("Some answer citations could not be mapped to indexed passage IDs.")
    if len(markers) > MAX_REFERENCES:
        warnings.append("Evidence is limited to 20 cited passages.")
    if not selected:
        warnings.append("No mapped passage citations; no backing graph can be shown.")
    return selected, warnings


def normalize_graph(rows, citations, truncated):
    by_chunk = {}
    for citation in citations:
        by_chunk.setdefault(citation["chunk_id"], []).append(citation["id"])
    nodes, edges, omitted = {}, {}, False
    for row in rows:
        evidence = by_chunk.get(row.get("chunk_id"))
        predicate = row.get("predicate")
        if (not evidence or predicate not in PREDICATES
                or (row.get("subject_type"), row.get("object_type")) != PREDICATES[predicate]):
            omitted = True
            continue
        if not all(isinstance(row.get(k), str) and row[k] for k in (
            "id", "subject_id", "object_id", "subject", "object"
        )):
            omitted = True
            continue
        identifiers = []
        for end in ("subject", "object"):
            identifier = row[end + "_type"] + ":" + row[end + "_id"]
            nodes[identifier] = {
                "id": identifier, "label": row[end][:500], "type": row[end + "_type"],
            }
            identifiers.append(identifier)
        confidence = row.get("edge_confidence")
        if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            confidence = None
        edges[row["id"]] = {
            "id": row["id"], "source": identifiers[0], "target": identifiers[1],
            "label": predicate, "evidence_ids": evidence, "confidence": confidence,
        }
    return {
        "nodes": list(nodes.values()), "edges": list(edges.values()),
        "truncated": truncated, "scope": "cited_passages",
    }, omitted


def phrase_match(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.IGNORECASE)


def passage_graph(citations, topics, question):
    nodes, edges, matches = {}, {}, []
    for citation in citations:
        speech_id = "Speech:" + citation["canonical_id"]
        passage_id = "Passage:" + citation["chunk_id"]
        nodes[speech_id] = {"id": speech_id, "label": citation["title"], "type": "Speech"}
        nodes[passage_id] = {
            "id": passage_id, "label": "Passage " + citation["id"], "type": "Passage",
        }
        edge_id = "passage:" + citation["chunk_id"]
        if edge_id in edges:
            edges[edge_id]["evidence_ids"].append(citation["id"])
        else:
            edges[edge_id] = {
                "id": edge_id, "source": speech_id, "target": passage_id,
                "label": "containsPassage", "evidence_ids": [citation["id"]], "confidence": None,
            }
        for topic in topics:
            phrases = [topic["name"], *topic.get("aliases", [])]
            matched = next((match for phrase in phrases
                            if phrase and (match := phrase_match(citation["text"], phrase))), None)
            if matched is not None:
                matches.append((citation, topic, matched.group(),
                                any(phrase_match(question, phrase) for phrase in phrases if phrase)))
    sources_by_topic = {}
    for citation, topic, _, _ in matches:
        sources_by_topic.setdefault(topic["id"], set()).add(citation["source_id"])
    matches.sort(key=lambda item: (
        not item[3], -len(sources_by_topic[item[1]["id"]]), item[1]["name"], item[0]["chunk_id"],
    ))
    truncated = False
    for citation, topic, literal, _ in matches:
        edge_id = "phrase:" + citation["chunk_id"] + ":" + topic["id"]
        if edge_id in edges:
            if citation["id"] not in edges[edge_id]["evidence_ids"]:
                edges[edge_id]["evidence_ids"].append(citation["id"])
            continue
        if len(edges) >= MAX_EDGES:
            truncated = True
            continue
        topic_id = "Topic:" + topic["id"]
        nodes[topic_id] = {"id": topic_id, "label": topic["name"], "type": "Topic"}
        edges[edge_id] = {
            "id": edge_id, "source": "Passage:" + citation["chunk_id"], "target": topic_id,
            "label": 'contains phrase "' + literal + '"', "evidence_ids": [citation["id"]],
            "confidence": None,
        }
    return {
        "nodes": list(nodes.values()), "edges": list(edges.values()), "truncated": truncated,
        "scope": "cited_passages", "basis": "source_and_exact_topic_phrase_matches",
    }


def approved_speech(chunk):
    if not FULL_CORPUS:
        return CATALOG.get(chunk.get("blob_url"))
    speech = CATALOG.get(chunk.get("source_id"))
    index = chunk.get("chunk_index")
    if (speech is None or not isinstance(index, int) or isinstance(index, bool) or index < 0
            or chunk.get("uid") != f'{speech["id"]}-{index:06d}'
            or chunk.get("transcript_sha256") != speech["transcript_sha256"]):
        return None
    return speech


class SearchService:
    def __init__(self, credential, client):
        self.credential = credential
        self.client = client
        self.token = None
        self.deadline = time.monotonic() + 35

    async def request(self, path, body):
        if self.token is None:
            self.token = await self.credential.get_token("https://search.azure.com/.default")
        response = await asyncio.wait_for(self.client.post(
            CONFIG["endpoint"] + "/" + path,
            params={"api-version": CONFIG["api_version"]},
            json=body,
            headers={
                "Authorization": "Bearer " + self.token.token,
                "x-ms-client-request-id": str(uuid.uuid4()),
                "User-Agent": "speech-atlas/1.0",
            },
        ), timeout=max(0.1, self.deadline - time.monotonic()))
        if response.status_code == 429:
            value = response.headers.get("retry-after", "")
            retry = min(int(value), 300) if value.isdigit() else 60
            raise ServiceError(429, "Azure is busy. Please wait before asking again.", retry)
        if response.status_code not in (200, 206):
            raise ServiceError(502, f"Azure Search rejected the request (HTTP {response.status_code}). No automatic retry was made.")
        return response.json(), response.status_code == 206

    async def probe(self):
        raw, _ = await self.request(
            "indexes('" + CONFIG["content_index"] + "')/docs/search",
            {"search": "*", "top": 1, "select": "uid"},
        )
        if not isinstance(raw, dict) or not raw.get("value"):
            raise ServiceError(503, "The configured Search index has no accessible passages.")

    async def ask(self, question):
        started = time.monotonic()
        identifier = str(uuid.uuid4())
        source_params = {
            "kind": "searchIndex" if FULL_CORPUS else "azureBlob",
            "knowledgeSourceName": CONFIG["knowledge_source"],
            "includeReferences": True, "includeReferenceSourceData": True,
            "alwaysQuerySource": True, "failOnError": True,
        }
        if not FULL_CORPUS:
            source_params["graphParameters"] = {
                "includeQuery": False,
                "provenance": {
                    "kind": "hydrate",
                    "hydrateDocuments": {"select": "uid,snippet,blob_url", "top": 8},
                },
            }
        raw, partial = await self.request(
            "knowledgebases('" + CONFIG["knowledge_base"] + "')/retrieve",
            {
                "messages": [{"role": "user", "content": [{"type": "text", "text": question}]}],
                "outputMode": "answerSynthesis",
                "retrievalReasoningEffort": {"kind": "low"},
                "maxRuntimeInSeconds": 20,
                "maxOutputDocuments": 8,
                ("maxOutputSizeInTokens" if FULL_CORPUS else "maxOutputSize"): 12000,
                "includeActivity": True,
                "knowledgeSourceParams": [source_params],
            },
        )
        answer = "\n\n".join(
            block["text"]
            for message in raw.get("response", [])
            for block in message.get("content", [])
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        warnings = list(WARNINGS)
        if partial:
            warnings.append("Azure returned a partial retrieval response.")
        if not answer:
            warnings.append("Azure returned no synthesized answer.")
        for activity in raw.get("activity") or []:
            if any(activity.get(key) for key in ("error", "warning", "errors", "warnings")):
                partial = True
                if (activity.get("type") == "graphSearch"
                        and "graph_search result was dropped" in str(activity.get("warning", ""))):
                    warnings.append(
                        "Azure excluded its graph-query result from answer synthesis because of "
                        "output limits. The displayed graph is loaded separately from cited passages."
                    )
                else:
                    warnings.append("A retrieval step reported an error or warning.")
        references, mapping_warnings = cited_references(raw, answer)
        warnings.extend(mapping_warnings)
        citations, graph = [], {
            "nodes": [], "edges": [], "truncated": False, "scope": "cited_passages",
        }
        if references:
            try:
                # Only hydrate known index keys, never a service-returned URL.
                chunks, _ = await self.request(
                    "indexes('" + CONFIG["content_index"] + "')/docs/search",
                    {"search": "*", "top": MAX_REFERENCES,
                     "filter": "search.in(uid, '" + "|".join(sorted(
                         {key for _, key in references}
                     )) + "', '|')",
                     "select": ("uid,snippet,source_id,transcript_sha256,chunk_index" if FULL_CORPUS
                                else "uid,snippet,blob_url,snippet_parent_id")},
                )
                indexed = {row["uid"]: row for row in chunks.get("value", [])}
                for ref_id, key in references:
                    chunk = indexed.get(key)
                    speech = approved_speech(chunk) if chunk else None
                    if speech is None:
                        partial = True
                        continue
                    citations.append({
                        "id": ref_id, "title": speech["title"],
                        "speaker": speech["speaker"], "date": speech["date"],
                        "chunk_id": key, "text": str(chunk.get("snippet", ""))[:12000],
                        "source_id": speech["id"],
                        **({"canonical_id": speech["canonical_id"]} if FULL_CORPUS else {}),
                    })
                if len(citations) != len(references):
                    warnings.append("Some citations were excluded: passage or verified source identity not found.")
                keys = sorted({citation["chunk_id"] for citation in citations})
                if keys and FULL_CORPUS:
                    graph = passage_graph(citations, CONFIG["topics"], question)
                    if graph["truncated"]:
                        warnings.append("Graph limited to 60 passage and exact phrase relationships.")
                elif keys:
                    result, _ = await self.request(
                        "indexes('" + CONFIG["graph_index"] + "')/docs/search",
                        {"search": "*", "top": MAX_EDGES + 1, "orderby": "id asc",
                         "filter": "search.in(chunk_id, '" + "|".join(keys) + "', '|')",
                         "select": "id,subject,subject_id,subject_type,predicate,object,"
                                   "object_id,object_type,chunk_id,edge_confidence"},
                    )
                    rows = result.get("value", [])
                    truncated = len(rows) > MAX_EDGES
                    graph, omitted = normalize_graph(rows[:MAX_EDGES], citations, truncated)
                    if truncated:
                        warnings.append("Graph limited to 60 relationship assertions from cited passages.")
                    if omitted:
                        warnings.append("Graph rows with missing evidence or invalid entity types were excluded.")
            except (ServiceError, httpx.HTTPError, TimeoutError):
                partial = True
                warnings.append("Evidence or graph lookup failed; the answer is retained without inventing relationships.")
        if mapping_warnings or not answer:
            partial = True
        if not graph["edges"]:
            warnings.append("No graph relationships were available for the mapped cited passages.")
        return {
            "request_id": identifier, "question": question, "answer": answer or None,
            "citations": citations, "graph": graph, "warnings": list(dict.fromkeys(warnings)),
            "coverage": COVERAGE, "partial": partial,
            "timing_ms": round((time.monotonic() - started) * 1000),
        }
