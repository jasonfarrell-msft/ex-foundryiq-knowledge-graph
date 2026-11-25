import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import azure.functions as func
import httpx

os.environ.setdefault("SPEECH_DATASET", "sample")

import function_app
import local_server
from service import (
    CATALOG, CONFIG, MAX_EDGES, SearchService, ServiceError,
    cited_references, normalize_graph, question_from,
)


KEY = "chunk_1"
BLOB, SPEECH = next(iter(CATALOG.items()))


def graph_row(**changes):
    return {
        "id": "edge_1", "subject": SPEECH["title"], "subject_id": "speech_1",
        "subject_type": "Speech", "predicate": "addressesTopic",
        "object": "employment", "object_id": "topic_1", "object_type": "Topic",
        "chunk_id": KEY, "edge_confidence": None, **changes,
    }


def request(method="POST", body=None, headers=None, url="http://127.0.0.1:7071/api/ask", params=None):
    return func.HttpRequest(
        method, url, headers=headers or {"Content-Type": "application/json"},
        body=json.dumps(body or {"question": "What themes?"}).encode(), params=params,
    )


class ValidationTests(unittest.TestCase):
    def test_questions_are_bounded_and_not_query_config(self):
        self.assertEqual(question_from({"question": "  A question?  "}), "A question?")
        for value in [None, [], {"question": ""}, {"question": "x" * 2001},
                      {"question": 42}, {"question": "q", "index": "other"},
                      {"question": "hello\x00"}]:
            with self.assertRaises(ValueError):
                question_from(value)

    def test_only_cited_valid_keys_are_mapped(self):
        raw = {"references": [
            {"id": "0", "sourceData": {"uid": KEY}},
            {"id": "1", "sourceData": {"uid": "uncited"}},
            {"id": "2", "sourceData": {"uid": "x') or true"}},
        ]}
        refs, warnings = cited_references(raw, "Answer [ref_id:0] [ref_id:2]")
        self.assertEqual(refs, [("0", KEY)])
        self.assertTrue(warnings)

    def test_graph_preserves_identity_evidence_and_discards_unrelated_rows(self):
        graph, omitted = normalize_graph(
            [graph_row(), graph_row(id="edge_other", chunk_id="uncited")],
            [{"id": "0", "chunk_id": KEY}], False,
        )
        self.assertTrue(omitted)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["nodes"][0]["id"], "Speech:speech_1")
        self.assertEqual(graph["edges"][0]["evidence_ids"], ["0"])
        self.assertIsNone(graph["edges"][0]["confidence"])
        graph, _ = normalize_graph(
            [graph_row(edge_confidence=float("nan"))], [{"id": "0", "chunk_id": KEY}], False
        )
        self.assertIsNone(graph["edges"][0]["confidence"])

    def test_unknown_predicates_and_wrong_endpoint_types_are_omitted(self):
        for row in [graph_row(predicate="madeUp"), graph_row(object_type="Speech")]:
            graph, omitted = normalize_graph([row], [{"id": "0", "chunk_id": KEY}], False)
            self.assertTrue(omitted)
            self.assertFalse(graph["edges"])

    def test_cloud_cannot_enable_local_bypass_and_forged_principal_is_not_enough(self):
        principal = base64.b64encode(json.dumps({
            "userId": "reader", "userRoles": ["authenticated", "speech_reader"],
        }).encode()).decode()
        with patch.dict(os.environ, {"SPEECH_LOCAL_DEVELOPMENT": "1",
                                    "WEBSITE_INSTANCE_ID": "azure"}, clear=True):
            self.assertFalse(function_app.authorized(request(headers={
                "x-ms-client-principal": principal,
            })))
        with patch.dict(os.environ, {"SPEECH_LOCAL_DEVELOPMENT": "1"}, clear=True):
            self.assertTrue(function_app.authorized(request()))
            self.assertFalse(function_app.authorized(request(
                url="http://not-loopback.example/api/ask"
            )))

    def test_anonymous_visitors_need_platform_boundary_but_not_user_roles(self):
        with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "true"}, clear=True):
            self.assertTrue(function_app.authorized(request()))
            self.assertTrue(function_app.authorized(request(headers={
                "x-ms-client-principal": "not-a-user-credential",
            })))
        for platform in ("", "false"):
            with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": platform}, clear=True):
                self.assertFalse(function_app.authorized(request()))
        with patch.dict(os.environ, {"SPEECH_LOCAL_DEVELOPMENT": "1"}, clear=True):
            denied = function_app.guard(request(headers={"Origin": "https://evil.example"}))
            self.assertEqual(denied.status_code, 403)


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_probe_only_reads_fixed_index_without_model_retrieval(self):
        calls = []

        def handler(req):
            calls.append(req)
            self.assertIn(CONFIG["content_index"], req.url.path)
            self.assertNotIn("/retrieve", req.url.path)
            self.assertEqual(json.loads(req.content), {"search": "*", "top": 1, "select": "uid"})
            return httpx.Response(200, json={"value": [{"uid": KEY}]})

        credential = SimpleNamespace(get_token=AsyncMock(
            return_value=SimpleNamespace(token="test-token")
        ))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await SearchService(credential, client).probe()
        self.assertEqual(len(calls), 1)
        credential.get_token.assert_awaited_once_with("https://search.azure.com/.default")

    async def test_health_search_probe_is_opt_in_and_does_not_synthesize(self):
        with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "true"}, clear=True), \
                patch.object(SearchService, "probe", new_callable=AsyncMock) as probe, \
                patch.object(SearchService, "ask", new_callable=AsyncMock) as synthesize:
            normal = await function_app.health(request(method="GET"))
            self.assertEqual(json.loads(normal.get_body())["azure_connectivity"], "not_probed")
            probe.assert_not_awaited()
            invalid = await function_app.health(request(method="GET", params={"probe": "other"}))
            self.assertEqual(invalid.status_code, 400)
            checked = await function_app.health(request(method="GET", params={"probe": "search"}))
            self.assertEqual(checked.status_code, 200)
            self.assertEqual(json.loads(checked.get_body())["azure_connectivity"], "search_verified")
            probe.assert_awaited_once()
            synthesize.assert_not_awaited()

    async def test_cloud_ask_initializes_real_identity_transport_without_model_calls(self):
        question = "Can you compare the common themes between Obama and Trump?"
        with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "true"}, clear=True), \
                patch.object(SearchService, "ask", new_callable=AsyncMock,
                             return_value={"answer": "Offline fixture"}) as retrieve:
            response = await function_app.ask(request(body={"question": question}))
        self.assertEqual(response.status_code, 200)
        retrieve.assert_awaited_once_with(question)

    async def test_missing_identity_transport_returns_safe_service_error(self):
        with patch.dict(os.environ, {"WEBSITE_AUTH_ENABLED": "true"}, clear=True), \
                patch.object(function_app, "ManagedIdentityCredential",
                             side_effect=ImportError("private dependency diagnostic")), \
                patch.object(SearchService, "ask", new_callable=AsyncMock) as retrieve:
            response = await function_app.ask(request())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private dependency diagnostic", response.get_body().decode())
        retrieve.assert_not_awaited()

    async def run_service(self, handler):
        credential = SimpleNamespace(get_token=AsyncMock(
            return_value=SimpleNamespace(token="test-token")
        ))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await SearchService(credential, client).ask("Question?")
        self.assertEqual(credential.get_token.await_count, 1)
        return result

    def answer_response(self):
        return {
            "response": [{"content": [{"type": "text", "text": "Answer [ref_id:0]"}]}],
            "references": [{"id": "0", "sourceData": {
                "uid": KEY, "snippet": "Untrusted KB text", "blob_url": BLOB,
            }}],
        }

    async def test_real_request_contract_hydrates_index_before_graph(self):
        calls = []

        def handler(req):
            calls.append(req)
            body = json.loads(req.content)
            if "/retrieve" in req.url.path:
                self.assertEqual(body["outputMode"], "answerSynthesis")
                self.assertEqual(body["maxRuntimeInSeconds"], 20)
                self.assertEqual(body["maxOutputSize"], 12000)
                self.assertNotIn("maxOutputSizeInTokens", body)
                self.assertEqual(body["knowledgeSourceParams"][0]["kind"], "azureBlob")
                return httpx.Response(200, json=self.answer_response())
            if CONFIG["content_index"] in req.url.path:
                self.assertEqual(body["filter"], "search.in(uid, 'chunk_1', '|')")
                return httpx.Response(200, json={"value": [{
                    "uid": KEY, "snippet": "Actual indexed evidence.", "blob_url": BLOB,
                }]})
            self.assertEqual(body["filter"], "search.in(chunk_id, 'chunk_1', '|')")
            self.assertEqual(body["top"], MAX_EDGES + 1)
            return httpx.Response(200, json={"value": [graph_row()]})

        result = await self.run_service(handler)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["citations"][0]["text"], "Actual indexed evidence.")
        self.assertEqual(result["graph"]["edges"][0]["evidence_ids"], ["0"])
        self.assertFalse(result["coverage"]["full_corpus_available"])

    async def test_unknown_source_is_not_presented_as_sample_evidence(self):
        calls = []

        def handler(req):
            calls.append(req)
            if "/retrieve" in req.url.path:
                return httpx.Response(200, json=self.answer_response())
            return httpx.Response(200, json={"value": [{
                "uid": KEY, "snippet": "Other source.", "blob_url": BLOB + "?other",
            }]})

        result = await self.run_service(handler)
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["citations"])
        self.assertFalse(result["graph"]["edges"])
        self.assertTrue(result["partial"])

    async def test_graph_failure_keeps_answer_and_reports_partial(self):
        def handler(req):
            if "/retrieve" in req.url.path:
                return httpx.Response(200, json=self.answer_response())
            if CONFIG["content_index"] in req.url.path:
                return httpx.Response(200, json={"value": [{
                    "uid": KEY, "snippet": "Evidence", "blob_url": BLOB,
                }]})
            return httpx.Response(503)

        result = await self.run_service(handler)
        self.assertEqual(result["answer"], "Answer [ref_id:0]")
        self.assertTrue(result["partial"])
        self.assertFalse(result["graph"]["edges"])

    async def test_dropped_graph_warning_explains_separate_passage_graph(self):
        def handler(req):
            if "/retrieve" in req.url.path:
                response = self.answer_response()
                response["activity"] = [{
                    "type": "graphSearch",
                    "warning": "The graph_search result was dropped because it did not fit within the configured retrieval output limits.",
                }]
                return httpx.Response(200, json=response)
            if CONFIG["content_index"] in req.url.path:
                return httpx.Response(200, json={"value": [{
                    "uid": KEY, "snippet": "Evidence", "blob_url": BLOB,
                }]})
            return httpx.Response(200, json={"value": [graph_row()]})

        result = await self.run_service(handler)
        self.assertTrue(result["partial"])
        self.assertTrue(result["graph"]["edges"])
        self.assertTrue(any("loaded separately" in warning for warning in result["warnings"]))

    async def test_no_citations_means_no_graph_not_unrelated_fallback(self):
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"response": [{
                "content": [{"type": "text", "text": "No citations."}],
            }]})

        result = await self.run_service(handler)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["partial"])
        self.assertFalse(result["graph"]["edges"])

    async def test_throttling_is_not_retried_and_preserves_safe_retry_after(self):
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(429, headers={"Retry-After": "30"})

        with self.assertRaises(ServiceError) as error:
            await self.run_service(handler)
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.retry_after, 30)
        self.assertEqual(len(calls), 1)

    async def test_edge_cap_is_reported(self):
        def handler(req):
            if "/retrieve" in req.url.path:
                return httpx.Response(200, json=self.answer_response())
            if CONFIG["content_index"] in req.url.path:
                return httpx.Response(200, json={"value": [{
                    "uid": KEY, "snippet": "Evidence", "blob_url": BLOB,
                }]})
            return httpx.Response(200, json={"value": [
                graph_row(id=f"edge_{i}") for i in range(MAX_EDGES + 1)
            ]})

        result = await self.run_service(handler)
        self.assertEqual(len(result["graph"]["edges"]), MAX_EDGES)
        self.assertTrue(result["graph"]["truncated"])

    async def test_http_request_validation_never_calls_azure(self):
        with patch.dict(os.environ, {"SPEECH_LOCAL_DEVELOPMENT": "1"}, clear=True):
            for body in [{"question": ""}, {"question": "ok", "endpoint": "evil"}]:
                result = await function_app.ask(request(body=body))
                self.assertEqual(result.status_code, 400)
            result = await function_app.ask(request(headers={"Content-Type": "text/plain"}))
            self.assertEqual(result.status_code, 415)

    async def test_loopback_adapter_uses_same_handlers_and_guards(self):
        with patch.dict(os.environ, {"SPEECH_LOCAL_DEVELOPMENT": "1"}, clear=True):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=local_server.app),
                base_url="http://127.0.0.1:7071",
            ) as client:
                health = await client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual((await client.get("/api/catalog")).json()["coverage"]["source_count"], 10)
                self.assertEqual((await client.post("/api/ask", json={"question": ""})).status_code, 400)
                self.assertEqual((await client.get(
                    "/api/health", headers={"Origin": "https://evil.example"},
                )).status_code, 403)
                self.assertEqual((await client.post(
                    "/api/ask", content=b"x" * 12001,
                    headers={"Content-Type": "application/json"},
                )).status_code, 413)


if __name__ == "__main__":
    unittest.main()
