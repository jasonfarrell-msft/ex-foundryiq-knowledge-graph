import os
import json
import unittest
from unittest.mock import patch

os.environ.setdefault("SPEECH_DATASET", "sample")

import httpx

import service
import test_api


SPEECHES = {
    name: {"id": name, "title": name, "speaker": "Speaker", "date": "2000-01-01",
           "transcript_sha256": "sha256:" + digit * 64, "canonical_id": "canonical-" + name}
    for name, digit in (("source-a", "a"), ("source-b", "b"))
}
TOPICS = [
    {"id": "jobs", "name": "employment", "aliases": ["jobs"]},
    {"id": "aid", "name": "aid", "aliases": []},
]


def citation(source="source-a", identifier="0", text="Jobs and employment."):
    return {"id": identifier, "chunk_id": source + "-000000", "source_id": source,
            "canonical_id": "canonical-" + source,
            "title": source, "text": text}


class PhraseGraphTests(unittest.TestCase):
    def test_exact_unicode_word_boundaries_and_shared_alias_identity(self):
        graph = service.passage_graph([
            citation(text="JOBS were created, not paid aid."),
            citation("source-b", "1", "Employment grew. Maid and naïdaid do not match aid."),
        ], TOPICS, "employment")
        topic = next(n for n in graph["nodes"] if n["id"] == "Topic:jobs")
        self.assertEqual(topic["label"], "employment")
        edges = [e for e in graph["edges"] if e["target"] == "Topic:jobs"]
        self.assertEqual(len(edges), 2)
        self.assertEqual({e["source"] for e in edges},
                         {"Passage:source-a-000000", "Passage:source-b-000000"})
        aid = [e for e in graph["edges"] if e["target"] == "Topic:aid"]
        self.assertEqual(len(aid), 2)  # Both passages contain a standalone "aid".
        self.assertIsNone(edges[0]["confidence"])
        self.assertFalse(service.phrase_match("paid maid naïdaid", "aid"))
        self.assertEqual(graph["basis"], "source_and_exact_topic_phrase_matches")

    def test_no_phrase_match_does_not_invent_topics_and_duplicates_merge_evidence(self):
        first = citation(text="An unrelated passage.")
        second = {**first, "id": "1"}
        graph = service.passage_graph([first, second], TOPICS, "employment")
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["evidence_ids"], ["0", "1"])

    def test_graph_cap_preserves_containment_and_prioritizes_asked_topic(self):
        topics = [{"id": str(i), "name": f"word{i}", "aliases": []} for i in range(70)]
        graph = service.passage_graph([
            citation(text=" ".join(topic["name"] for topic in topics)),
        ], topics, "word69")
        self.assertTrue(graph["truncated"])
        self.assertEqual(len(graph["edges"]), service.MAX_EDGES)
        self.assertEqual(graph["edges"][0]["label"], "containsPassage")
        self.assertEqual(graph["edges"][1]["target"], "Topic:69")
        self.assertTrue(all(e["evidence_ids"] == ["0"] for e in graph["edges"]))

    def test_duplicate_sources_share_canonical_speech_but_keep_passage_provenance(self):
        first = citation()
        second = {**citation("source-b", "1"), "canonical_id": first["canonical_id"]}
        graph = service.passage_graph([first, second], TOPICS, "jobs")
        speeches = [node for node in graph["nodes"] if node["type"] == "Speech"]
        self.assertEqual(len(speeches), 1)
        containment = [edge for edge in graph["edges"] if edge["label"] == "containsPassage"]
        self.assertEqual({edge["source"] for edge in containment}, {speeches[0]["id"]})
        self.assertEqual({edge["target"] for edge in containment},
                         {"Passage:source-a-000000", "Passage:source-b-000000"})
        self.assertEqual({tuple(edge["evidence_ids"]) for edge in containment}, {("0",), ("1",)})


class FullRetrievalTests(unittest.IsolatedAsyncioTestCase):
    run_service = test_api.RetrievalTests.run_service

    async def test_full_request_uses_search_index_and_only_verified_source_passages(self):
        config = {**service.CONFIG, "topics": TOPICS}
        coverage = {"source_count": 1060, "full_corpus_available": True}
        for invalid in (None, "source", "hash", "key"):
            calls = []

            def handler(req):
                calls.append(req)
                if "/retrieve" in req.url.path:
                    body = json.loads(req.content)
                    self.assertEqual(body["maxOutputSizeInTokens"], 12000)
                    self.assertNotIn("maxOutputSize", body)
                    params = body["knowledgeSourceParams"][0]
                    self.assertEqual(params["kind"], "searchIndex")
                    self.assertNotIn("graphParameters", params)
                    return httpx.Response(200, json={
                        "response": [{"content": [{"type": "text", "text": "Answer [ref_id:0]"}]}],
                        "references": [{"id": "0", "sourceData": {"uid": "source-a-000000"}}],
                    })
                self.assertIn(config["content_index"], req.url.path)
                chunk = {
                    "uid": "source-a-000000", "source_id": "source-a",
                    "chunk_index": 0, "snippet": "Jobs were created.",
                    "transcript_sha256": SPEECHES["source-a"]["transcript_sha256"],
                }
                if invalid == "source":
                    chunk["source_id"] = "unapproved"
                elif invalid == "hash":
                    chunk["transcript_sha256"] = "wrong"
                elif invalid == "key":
                    chunk["chunk_index"] = 1
                return httpx.Response(200, json={"value": [chunk]})

            with self.subTest(invalid=invalid), patch.multiple(
                service, FULL_CORPUS=True, CONFIG=config, CATALOG=SPEECHES,
                COVERAGE=coverage, WARNINGS=[],
            ):
                result = await self.run_service(handler)
            self.assertEqual(len(calls), 2)
            self.assertTrue(result["coverage"]["full_corpus_available"])
            if invalid is None:
                self.assertEqual(result["citations"][0]["source_id"], "source-a")
                self.assertEqual(len(result["graph"]["edges"]), 2)
                self.assertFalse(result["partial"])
            else:
                self.assertFalse(result["citations"])
                self.assertFalse(result["graph"]["edges"])
                self.assertTrue(result["partial"])


if __name__ == "__main__":
    unittest.main()
