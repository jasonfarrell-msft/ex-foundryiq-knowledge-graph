import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("SPEECH_DATASET", "full")

import httpx

import function_app
import service
from test_api import request


GRAPH_CONFIG = {
    "search_endpoint": "https://example-search.search.windows.net",
    "search_api_version": "2026-11-01-preview",
    "graph_index": "president-graph-v2-graph",
    "chunks_index": "president-graph-v2-chunks",
    "graph_name": "president-graph-v2",
    "foundry_endpoint": "https://example-foundry.cognitiveservices.azure.com",
    "chat_deployment": "chat",
}


class GraphRequestValidationTests(unittest.TestCase):
    def test_ask_request_from_accepts_optional_mode_and_rejects_unknown_fields(self):
        self.assertEqual(service.ask_request_from({"question": "  Hello? "}), ("Hello?", "rag"))
        self.assertEqual(service.ask_request_from({"question": "Hello?", "mode": "graph"}), ("Hello?", "graph"))
        for bad in (
            {"question": "Hello?", "mode": "other"},
            {"question": "Hello?", "mode": "graph", "extra": 1},
            {"mode": "graph"},
        ):
            with self.assertRaises(ValueError):
                service.ask_request_from(bad)

    def test_gql_literal_escapes_quotes_and_strips_control_chars(self):
        self.assertEqual(service.gql_literal("Iraq's\nWar"), "'Iraq\\'s War'")

    def test_resolve_catalog_targets_aligns_tied_farewell_address_to_washington(self):
        with patch.dict(service.CONFIG, {
            "speeches": [
                {"id": "lincoln-1", "title": "November 19, 1863: Gettysburg Address", "speaker": "Abraham Lincoln", "date": "1863-11-19"},
                {"id": "lincoln-2", "title": "February 11, 1861: Farewell Address", "speaker": "Abraham Lincoln", "date": "1861-02-11"},
                {"id": "wash-1", "title": "September 19, 1796: Farewell Address", "speaker": "George Washington", "date": "1796-09-19"},
            ]
        }):
            speakers, titles, _, hint_groups = service.resolve_catalog_targets(
                ["Lincoln", "Washington"], ["Gettysburg Address", "Farewell Address"]
            )
        self.assertEqual(speakers, ["Abraham Lincoln", "George Washington"])
        self.assertEqual(hint_groups, [
            ["November 19, 1863: Gettysburg Address"],
            ["September 19, 1796: Farewell Address"],
        ])
        self.assertEqual(titles, ["November 19, 1863: Gettysburg Address", "September 19, 1796: Farewell Address"])


class GraphRagServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_ask_graph_plans_gql_hydrates_chunks_and_synthesizes_answer(self):
        calls = []

        def handler(req):
            calls.append(req)
            body = json.loads(req.content)
            if req.url.path.endswith("/openai/v1/chat/completions"):
                if "response_format" in body:
                    return httpx.Response(200, json={
                        "choices": [{
                            "message": {
                                "content": json.dumps({
                                    "intent": "shared_topics",
                                    "predicate": "addressesTopic",
                                    "speakers": ["Abraham Lincoln", "George Washington"],
                                    "speech_hints": ["Gettysburg Address", "Farewell Address"],
                                    "terms": ["national unity"],
                                    "fallback_query": "Lincoln Washington national unity",
                                })
                            }
                        }]
                    })
                self.assertIn("[ref_id:0]", body["messages"][1]["content"])
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "Both addressed national unity [ref_id:0]."}}]
                })

            if req.url.path.endswith("/docs/search.graph"):
                if "RETURN t.name AS topic" in body["gql"]:
                    return httpx.Response(200, json={"value": [{"topic": "national unity"}]})
                return httpx.Response(200, json={
                    "value": [{"subject": "November 19, 1863: Gettysburg Address", "predicate": "addressesTopic", "object": "national unity"}],
                    "@search.graphProvenanceDocuments": {
                        "speech-gettysburg-000000": {
                            "uid": "speech-gettysburg-000000",
                            "parent_id": "speech-gettysburg",
                            "title": "November 19, 1863: Gettysburg Address",
                            "snippet": "Four score and seven years ago...",
                        }
                    },
                })

            if GRAPH_CONFIG["chunks_index"] in req.url.path:
                return httpx.Response(200, json={
                    "value": [{
                        "uid": "speech-wash-000000",
                        "parent_id": "speech-wash",
                        "title": "April 30, 1789: First Inaugural Address",
                        "snippet": "Fellow Citizens of the Senate...",
                    }]
                })

            self.assertIn(GRAPH_CONFIG["graph_index"], req.url.path)
            return httpx.Response(200, json={
                "value": [{
                    "id": "edge-1",
                    "subject": "November 19, 1863: Gettysburg Address",
                    "subject_id": "speech-gettysburg",
                    "subject_type": "Speech",
                    "predicate": "addressesTopic",
                    "object": "national unity",
                    "object_id": "topic-unity",
                    "object_type": "Topic",
                    "chunk_id": "speech-gettysburg-000000",
                    "confidence": 0.98,
                }]
            })

        credential = SimpleNamespace(get_token=AsyncMock(
            return_value=SimpleNamespace(token="test-token")
        ))
        with patch.object(service, "GRAPH_CONFIG", GRAPH_CONFIG):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await service.SearchService(credential, client).ask(
                    "Compare Lincoln and Washington on national unity.", mode="graph"
                )

        self.assertEqual(result["mode"], "graph")
        self.assertEqual(result["graph"]["basis"], "foundry_graph_index")
        self.assertEqual(result["graph"]["edges"][0]["confidence"], 0.98)
        self.assertEqual(result["citations"][0]["chunk_id"], "speech-gettysburg-000000")
        self.assertIn("national unity", result["answer"])
        self.assertGreaterEqual(len(result["gql_queries"]), 2)
        scopes = {c.args[0] for c in credential.get_token.await_args_list}
        self.assertEqual(scopes, {"https://search.azure.com/.default", "https://ai.azure.com/.default"})

    async def test_ask_graph_handles_non_object_planner_json_and_edge_lookup_failure(self):
        credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="test-token")))
        with patch.object(service, "GRAPH_CONFIG", GRAPH_CONFIG):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})
            )) as client:
                with self.assertRaises(service.ServiceError) as err:
                    await service.SearchService(credential, client).ask("Hello?", mode="graph")
                self.assertEqual(err.exception.status, 502)

        def partial_handler(req):
            body = json.loads(req.content)
            if req.url.path.endswith("/openai/v1/chat/completions"):
                if "response_format" in body:
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                        "intent": "topic_search", "predicate": "addressesTopic",
                        "speakers": [], "speech_hints": [], "terms": ["banking"], "fallback_query": "banking",
                    })}}]})
                return httpx.Response(200, json={"choices": [{"message": {"content": "Partial [ref_id:0]."}}]})
            if req.url.path.endswith("/docs/search.graph"):
                return httpx.Response(200, json={
                    "value": [{"subject": "Speech 1", "predicate": "addressesTopic", "object": "banking"}],
                    "@search.graphProvenanceDocuments": {
                        "speech-1-000000": {"uid": "speech-1-000000", "parent_id": "speech-1", "title": "Speech 1", "snippet": "Banking."}
                    },
                })
            return httpx.Response(503)

        with patch.object(service, "GRAPH_CONFIG", GRAPH_CONFIG):
            async with httpx.AsyncClient(transport=httpx.MockTransport(partial_handler)) as client:
                res = await service.SearchService(credential, client).ask("Which speeches mention banking?", mode="graph")
        self.assertTrue(res["partial"])
        self.assertEqual(res["answer"], "Partial [ref_id:0].")
        self.assertEqual(res["graph"]["edges"], [])

    async def test_ask_graph_chunk_fallback_when_gql_returns_no_chunks(self):
        def fallback_handler(req):
            body = json.loads(req.content)
            if req.url.path.endswith("/openai/v1/chat/completions"):
                if "response_format" in body:
                    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                        "intent": "topic_search", "predicate": "addressesTopic",
                        "speakers": [], "speech_hints": [], "terms": ["unmatched"], "fallback_query": "unmatched",
                    })}}]})
                return httpx.Response(200, json={"choices": [{"message": {"content": "Fallback [ref_id:0]."}}]})
            if req.url.path.endswith("/docs/search.graph"):
                return httpx.Response(200, json={"value": []})
            if GRAPH_CONFIG["chunks_index"] in req.url.path:
                return httpx.Response(200, json={"value": [{"uid": "speech-1-000000", "parent_id": "speech-1", "title": "Speech 1", "snippet": "Text."}]})
            return httpx.Response(200, json={"value": []})

        credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="test-token")))
        with patch.object(service, "GRAPH_CONFIG", GRAPH_CONFIG):
            async with httpx.AsyncClient(transport=httpx.MockTransport(fallback_handler)) as client:
                res = await service.SearchService(credential, client).ask("Unmatched theme?", mode="graph")
        self.assertTrue(any("keyword/semantic search" in w for w in res["warnings"]))
        self.assertEqual(res["citations"][0]["chunk_id"], "speech-1-000000")

    async def test_function_app_routes_graph_mode_and_reports_health_flag(self):
        with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "true"}, clear=True), \
                patch.object(service, "GRAPH_CONFIG", GRAPH_CONFIG), \
                patch.object(function_app, "GRAPH_CONFIG", GRAPH_CONFIG), \
                patch.object(service.SearchService, "ask_graph", new_callable=AsyncMock,
                             return_value={"answer": "Graph answer", "mode": "graph"}) as ask_graph:
            health_resp = await function_app.health(request(method="GET"))
            self.assertTrue(json.loads(health_resp.get_body())["graph_rag_available"])

            ask_resp = await function_app.ask(request(body={"question": "Compare speeches", "mode": "graph"}))
            self.assertEqual(ask_resp.status_code, 200)
            ask_graph.assert_awaited_once_with("Compare speeches")

            bad_mode = await function_app.ask(request(body={"question": "Compare speeches", "mode": "invalid"}))
            self.assertEqual(bad_mode.status_code, 400)


if __name__ == "__main__":
    unittest.main()
