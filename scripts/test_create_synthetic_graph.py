import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

import create_synthetic_graph as graph

SUB = "00000000-0000-0000-0000-000000000001"
ARGUMENTS = [
    "--search-resource-id", f"/subscriptions/{SUB}/resourceGroups/demo/providers/Microsoft.Search/searchServices/searchdemo",
    "--storage-resource-id", f"/subscriptions/{SUB}/resourceGroups/demo/providers/Microsoft.Storage/storageAccounts/storagedemo",
    "--model-resource-id", f"/subscriptions/{SUB}/resourceGroups/demo/providers/Microsoft.CognitiveServices/accounts/modeldemo",
    "--chat-deployment", "chat", "--chat-model", "gpt-5.6-luna",
    "--embedding-deployment", "embedding", "--embedding-model", "text-embedding-3-small",
]


class GraphTests(unittest.TestCase):
    def test_dry_run_is_offline_and_has_graph_payload(self):
        output = io.StringIO()
        with patch.object(graph, "az") as cli, patch.object(graph, "apply") as apply:
            with contextlib.redirect_stdout(output):
                self.assertEqual(graph.main(ARGUMENTS), 0)
            cli.assert_not_called()
            apply.assert_not_called()
        plan = json.JSONDecoder().raw_decode(output.getvalue())[0]
        source = plan["create"]["knowledgeSource"]
        schema = json.loads(source["azureBlobParameters"]["graphExtraction"]["ontology"]["value"])
        self.assertEqual(len(schema["@graph"]), 5)
        self.assertEqual(plan["create"]["knowledgeBase"]["knowledgeSources"], [{"name": source["name"]}])
        self.assertNotIn("apiKey", output.getvalue())
        self.assertIn("Unknown", output.getvalue())

    def test_rejects_resource_url_injection_before_network(self):
        arguments = ARGUMENTS.copy()
        arguments[1] += "?redirect=https://example.com"
        with patch.object(graph, "az") as cli, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(graph.main(arguments + ["--apply"]), 1)
            cli.assert_not_called()

    def test_cli_error_does_not_disclose_output_or_inherited_credentials(self):
        result = Mock(returncode=1, stdout="secret", stderr="secret")
        with patch.dict(graph.os.environ, {"AZURE_STORAGE_KEY": "secret"}):
            with patch.object(graph.subprocess, "run", return_value=result) as run:
                with self.assertRaisesRegex(RuntimeError, "failed") as error:
                    graph.az("storage", "container", "create")
                self.assertNotIn("secret", str(error.exception))
                self.assertNotIn("AZURE_STORAGE_KEY", run.call_args.kwargs["env"])

    def test_create_uses_conditional_header_and_disallows_redirect(self):
        with patch.object(graph, "az", return_value={"accessToken": "private", "expires_on": 9999999999}):
            client = graph.Search("searchdemo", SUB)
            response = Mock()
            response.read.return_value = b'{"name":"new"}'
            client.opener = MagicMock()
            client.opener.open.return_value.__enter__.return_value = response
            client.request("PUT", "knowledgesources('new')", {"name": "new"}, create=True)
            request = client.opener.open.call_args.args[0]
            self.assertEqual(request.get_header("If-none-match"), "*")
            self.assertEqual(request.get_header("Authorization"), "Bearer private")
        self.assertIsNone(graph.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_active_is_not_complete_and_failed_ingestion_stops(self):
        client = Mock()
        client.request.side_effect = [
            {"synchronizationStatus": "active"},
            {"lastSynchronizationState": {"endTime": "done", "itemsUpdatesFailed": 1}},
        ]
        with patch.object(graph.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "failures"):
                graph.wait_for_graph(client, "demo-ks", 10)
        self.assertEqual(client.request.call_count, 2)

    def test_completion_requires_returned_graph_index(self):
        client = Mock()
        client.request.side_effect = [
            {"lastSynchronizationState": {"endTime": "done", "itemsUpdatesFailed": 0}},
            {"azureBlobParameters": {"createdResources": {"graphIndex": "returned-index"}}},
        ]
        self.assertEqual(graph.wait_for_graph(client, "demo-ks", 10), "returned-index")


if __name__ == "__main__":
    unittest.main()
