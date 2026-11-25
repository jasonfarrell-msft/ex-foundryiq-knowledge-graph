import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import setup_resources as setup

SUB = "00000000-0000-0000-0000-000000000001"


class SetupTests(unittest.TestCase):
    def test_default_plan_is_offline(self):
        output = io.StringIO()
        with patch.object(setup, "az") as cli, patch.object(setup, "deploy") as deploy:
            with contextlib.redirect_stdout(output):
                self.assertEqual(setup.main(["--subscription", SUB]), 0)
            cli.assert_not_called()
            deploy.assert_not_called()
        plan = json.JSONDecoder().raw_decode(output.getvalue())[0]
        self.assertEqual(plan["region"], "eastus")
        self.assertEqual(plan["search"]["sku"], "basic")
        self.assertIn("Unknown", plan["costs"])
        self.assertLessEqual(len(plan["newResources"]["storage"]), 24)

    def test_apply_requires_explicit_public_demo_consent(self):
        with patch.object(setup, "az") as cli, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup.main(["--subscription", SUB, "--apply"]), 1)
            cli.assert_not_called()

    def test_existing_planned_resource_stops_before_any_write(self):
        args = SimpleNamespace(subscription=SUB, prefix="graphdemo", chat_model="chat", embedding_model="embed")
        resource_names = setup.names(SUB, args.prefix, "shared-rg")
        collision = setup.graph_arguments(args, resource_names)[1]
        with patch.object(setup, "check_management_apis"), patch.object(setup, "az", side_effect=[
            {"state": "Enabled"}, {"name": "AzureCloud"}, True, [{"id": collision.upper()}],
        ]) as cli:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                setup.deploy(args, resource_names)
        self.assertFalse(any("create" in call.args for call in cli.call_args_list))

    def test_custom_group_and_location_plan_is_offline(self):
        for flag in ("--resource-group", "--rg"):
            with self.subTest(flag=flag):
                output = io.StringIO()
                with patch.object(setup, "az") as cli, contextlib.redirect_stdout(output):
                    self.assertEqual(setup.main([
                        "--subscription", SUB, flag, "shared_graph-rg", "--location", "West US 3",
                    ]), 0)
                    cli.assert_not_called()
                plan = json.JSONDecoder().raw_decode(output.getvalue())[0]
                self.assertEqual(plan["newResources"]["resourceGroup"], "shared_graph-rg")
                self.assertEqual(plan["region"], "westus3")

    def test_invalid_group_and_location_stop_before_network(self):
        for arguments in (["--rg", "bad/group"], ["--rg", "group."], ["--location", "westus3;command"]):
            with self.subTest(arguments=arguments):
                with patch.object(setup, "az") as cli, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(setup.main(["--subscription", SUB, *arguments]), 1)
                    cli.assert_not_called()

    def test_catalog_selects_foundry_ga_default_and_checks_quota(self):
        model = {
            "name": "chat", "format": "OpenAI", "version": "2026-07-09",
            "isDefaultVersion": True, "lifecycleStatus": "GenerallyAvailable",
            "skus": [{"name": "GlobalStandard", "usageName": "quota",
                      "capacity": {"minimum": None, "maximum": 100, "step": None}}],
        }
        catalog = [{"kind": "OpenAI", "model": model}, {"kind": "AIServices", "model": model}]
        usage = [{"name": {"value": "quota"}, "limit": 10, "currentValue": 0}]
        self.assertEqual(setup.choose_model(catalog, "chat", None, "GlobalStandard", 10, usage), "2026-07-09")
        usage[0]["currentValue"] = 1
        with self.assertRaisesRegex(RuntimeError, "quota"):
            setup.choose_model(catalog, "chat", None, "GlobalStandard", 10, usage)
        model["lifecycleStatus"] = "Preview"
        with self.assertRaisesRegex(RuntimeError, "GA version"):
            setup.choose_model(catalog, "chat", None, "GlobalStandard", 1, usage)

    def test_unsupported_management_api_stops_before_group_creation(self):
        args = SimpleNamespace(subscription=SUB)
        with patch.object(setup, "az", side_effect=[
            {"state": "Enabled"}, {"name": "AzureCloud"},
            {"resourceTypes": [{"resourceType": "searchServices", "apiVersions": ["2025-05-01"]}]},
        ]) as cli:
            with self.assertRaisesRegex(RuntimeError, "management API"):
                setup.deploy(args, setup.names(SUB, "graphdemo"))
        self.assertFalse(any("create" in call.args for call in cli.call_args_list))

    def test_prefix_and_subscription_validation_prevents_argument_injection(self):
        with patch.object(setup, "az") as cli, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup.main(["--subscription", SUB, "--prefix", "demo/evil", "--apply", "--allow-public-demo"]), 1)
            cli.assert_not_called()

    def test_mocked_deployment_creates_or_reuses_group_and_propagates_location(self):
        resource_names = setup.names(SUB, "graphdemo", "shared_graph-rg")
        temporary_paths = []
        args = SimpleNamespace(
            subscription=SUB, principal_id=SUB, principal_type="User",
            prefix="graphdemo", search_sku="basic", model_sku="GlobalStandard",
            chat_model="chatmodel", embedding_model="embedmodel",
            chat_version=None, embedding_version=None, chat_capacity=10,
            embedding_capacity=1, location="westus3",
        )

        def cli(*arguments, **kwargs):
            if arguments[:2] == ("account", "show"):
                return {"state": "Enabled"}
            if arguments[:2] == ("cloud", "show"):
                return {"name": "AzureCloud"}
            if arguments[:2] == ("group", "exists"):
                return group_exists
            if arguments[:2] == ("group", "create"):
                return {"tags": {"setupRun": arguments[-1].split("=", 1)[1]}}
            if arguments[:2] == ("resource", "list"):
                return [{"id": f"/subscriptions/{SUB}/resourceGroups/shared_graph-rg/providers/Microsoft.KeyVault/vaults/unrelated"}] if group_exists else []
            if arguments[:2] == ("provider", "show"):
                namespace = arguments[arguments.index("--namespace") + 1]
                return {"resourceTypes": [
                    {"resourceType": kind, "apiVersions": [version]}
                    for kind, version in setup.REQUIRED_APIS[namespace].items()
                ]}
            if arguments[:2] == ("bicep", "build"):
                path = Path(arguments[arguments.index("--outfile") + 1])
                temporary_paths.append(path)
                path.write_text("{}", encoding="utf-8")
                return {}
            if arguments[:3] == ("deployment", "group", "create"):
                parameter_path = Path(arguments[arguments.index("--parameters") + 1][1:])
                parameters = json.loads(parameter_path.read_text())["parameters"]
                self.assertEqual(parameters["location"]["value"], "westus3")
                ids = setup.graph_arguments(args, resource_names)[1:6:2]
                return {"properties": {
                    "provisioningState": "Succeeded",
                    "outputs": {key: {"value": value} for key, value in zip(
                        ("searchResourceId", "storageResourceId", "modelResourceId"), ids
                    )},
                }}
            return {}

        for group_exists in (False, True):
            with self.subTest(group_exists=group_exists), tempfile.TemporaryDirectory() as directory:
                args.output = Path(directory) / "manifest.json"
                with patch.object(setup, "az", side_effect=cli) as commands, patch.object(
                    setup, "choose_model", side_effect=["chatversion", "embedversion"]
                ), contextlib.redirect_stdout(io.StringIO()):
                    setup.deploy(args, resource_names)
                creates = [call for call in commands.call_args_list if call.args[:2] == ("group", "create")]
                self.assertEqual(len(creates), 0 if group_exists else 1)
                if creates:
                    self.assertEqual(creates[0].args[creates[0].args.index("--location") + 1], "westus3")
                regional_calls = [
                    call for call in commands.call_args_list
                    if call.args[:3] in (("cognitiveservices", "model", "list"), ("cognitiveservices", "usage", "list"))
                ]
                for call in regional_calls:
                    self.assertEqual(call.args[call.args.index("--location") + 1], "westus3")
                manifest = json.loads(args.output.read_text())
                self.assertEqual(manifest["graphArguments"], setup.graph_arguments(args, resource_names))
                self.assertEqual(manifest["chatModelVersion"], "chatversion")
                self.assertEqual(manifest["location"], "westus3")
                self.assertEqual(manifest["resourceGroupCreated"], not group_exists)
                self.assertNotIn("accessToken", manifest)
        self.assertTrue(temporary_paths)
        self.assertFalse(temporary_paths[0].exists())


if __name__ == "__main__":
    unittest.main()
