import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import deploy_application as deploy


SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
BASE = ["--subscription", SUBSCRIPTION, "--resource-group", "rg-recreated"]


class DeploymentTests(unittest.TestCase):
    def test_provider_catalog_need_not_advertise_nested_deployment_types(self):
        required = {
            "Microsoft.Search": {"searchServices": "2026-11-01"},
            "Microsoft.CognitiveServices": {"accounts": "2026-11-01"},
            "Microsoft.Web": {"staticSites": "2025-05-01", "sites": "2026-08-01"},
            "Microsoft.Network": {"privateEndpoints": "2026-07-01"},
            "Microsoft.Storage": {"storageAccounts": "2026-09-01"},
        }
        runner = Mock()
        runner.az.side_effect = lambda *args: {"resourceTypes": [
            {"resourceType": kind, "apiVersions": [version], "locations": ["East US 2"]}
            for kind, version in required[args[-1]].items()
        ]}
        deploy.ensure_apis(runner, "eastus2")

    def test_cloud_inspection_omits_unsupported_subscription_flag_but_resource_calls_are_scoped(self):
        runner = deploy.Runner(SUBSCRIPTION)
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="{}")) as run:
            runner.az("cloud", "show")
            self.assertNotIn("--subscription", run.call_args.args[0])
            runner.az("account", "show")
            self.assertIn("--subscription", run.call_args.args[0])
            self.assertIn(SUBSCRIPTION, run.call_args.args[0])

    def test_offline_plan_never_constructs_cloud_runner(self):
        bundle = {"manifest_sha256": "sha256:" + "a" * 64, "catalog": [None] * 1060, "docs": [None] * 7422}
        output = io.StringIO()
        with patch.object(deploy, "load_bundle", return_value=bundle), patch.object(
            deploy, "Runner", side_effect=AssertionError("offline cloud access")
        ), contextlib.redirect_stdout(output):
            self.assertEqual(deploy.main([*BASE, "--create-data"]), 0)
        plan = json.loads(output.getvalue())
        self.assertFalse(plan["azure_writes"])
        self.assertEqual(plan["chunk_count"], 7422)
        self.assertEqual(plan["model_queries"], 0)

    def test_recovery_does_not_depend_on_corpus_or_build(self):
        with patch.object(deploy, "load_bundle", side_effect=AssertionError("corpus read")), patch.object(
            deploy, "apply"
        ) as apply:
            self.assertEqual(deploy.main([*BASE, "--close-bootstrap", "--apply"]), 0)
        self.assertEqual(apply.call_args.args[2:], (None, None, None))

    def test_old_installed_sdk_restores_pinned_project_environment_before_apply(self):
        with patch.object(deploy, "version", return_value="0.0.0"), patch(
            "subprocess.run"
        ) as run, patch("os.execv") as execute:
            deploy.ensure_python(BASE)
        self.assertTrue(any("-r" in call.args[0] and str(deploy.ROOT / "api/requirements-dev.txt") in call.args[0]
                            for call in run.call_args_list))
        self.assertEqual(execute.call_args.args[1][-len(BASE):], BASE)

    def test_cold_apply_requires_one_public_ipv4_not_a_range(self):
        for extra in ([], ["--publisher-ip", "0.0.0.0/0"], ["--publisher-ip", "10.1.1.1"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                deploy.arguments([*BASE, "--create-data", "--apply", *extra])
        args = deploy.arguments([*BASE, "--create-data", "--apply", "--publisher-ip", "93.184.216.34"])
        self.assertEqual(args.publisher_ip, "93.184.216.34")

    def test_names_and_new_target_are_deterministic_without_mutating_defaults(self):
        args = deploy.arguments([*BASE, "--create-data"])
        names = deploy.profile(args)
        target, config = deploy.target_and_config(args, names, {"manifest_sha256": "sha256:" + "a" * 64})
        self.assertEqual(names, deploy.profile(args))
        self.assertNotEqual(names, deploy.profile(deploy.arguments([*BASE, "--suffix", "bbbbbbbb"])))
        self.assertIsNone(config)
        self.assertIn(names["search"], target.endpoint)
        self.assertIn(names["foundry"], target.models[0]["azureOpenAIParameters"]["resourceUri"])
        self.assertIsNone(target.models[0]["azureOpenAIParameters"]["apiKey"])
        self.assertNotEqual(deploy.PublicationTarget().endpoint, target.endpoint)

    def test_frontend_deployment_rotates_token_after_failure_and_keeps_it_out_of_argv(self):
        args = deploy.arguments(BASE)
        runner = Mock()
        runner.az.return_value = {"properties": {"apiKey": "fake-deployment-token"}}
        runner.command.side_effect = deploy.DeploymentError("deployment failed")
        with patch.dict("os.environ", {"SWA_CLI_API_LOCATION": "/unrelated/private-api"}), self.assertRaises(deploy.DeploymentError):
            deploy.secure_frontend_deploy(runner, args, deploy.profile(args), Path("/web"))
        self.assertEqual(runner.az.call_args.args[:3], ("staticwebapp", "secrets", "reset-api-key"))
        self.assertNotIn("fake-deployment-token", runner.command.call_args.args[0])
        self.assertEqual(runner.command.call_args.kwargs["env"]["SWA_CLI_DEPLOYMENT_TOKEN"], "fake-deployment-token")
        self.assertNotIn("SWA_CLI_API_LOCATION", runner.command.call_args.kwargs["env"])

    def test_fast_binding_rejects_catalog_or_topic_drift_before_cloud_access(self):
        config = json.loads((deploy.ROOT / "api/corpus.json").read_text())
        bundle = {
            "manifest_sha256": config["preparation_manifest_sha256"],
            "catalog": config["speeches"], "topics": config["topics"],
            "docs": [None] * config["chunk_count"],
            "manifest": {"canonical_speech_count": config["canonical_count"]},
        }
        args = deploy.arguments(BASE)
        with tempfile.TemporaryDirectory() as directory, patch.object(deploy, "ROOT", Path(directory).resolve()):
            api = Path(directory) / "api"
            api.mkdir()
            binding = api / "corpus.json"
            binding.write_text(json.dumps(config))
            deploy.target_and_config(args, deploy.profile(args), bundle)
            for field in ("speeches", "topics"):
                changed = {**config, field: config[field][:-1]}
                binding.write_text(json.dumps(changed))
                with self.subTest(field=field), self.assertRaises(deploy.DeploymentError):
                    deploy.target_and_config(args, deploy.profile(args), bundle)

    def test_bootstrap_closure_refuses_foreign_resources_and_verifies_owned_state(self):
        args = deploy.arguments(BASE)
        names = deploy.profile(args)
        resource = {"id": deploy.resource_id(args, "Microsoft.Search/searchServices", names["search"]), "tags": {}}
        runner = Mock()
        runner.az.side_effect = [True, [resource]]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(deploy.DeploymentError):
                deploy.close_bootstrap(runner, args, names, Path(directory).resolve())
            self.assertFalse(any(call.args[:2] == ("resource", "update") for call in runner.az.call_args_list))
            resource["tags"]["recreationOwner"] = names["owner"]
            runner.az.reset_mock()
            runner.az.side_effect = [True, [resource], {}, {"properties": {"publicNetworkAccess": "Disabled"}}]
            deploy.close_bootstrap(runner, args, names, Path(directory).resolve())
            self.assertEqual(json.loads((Path(directory) / "SECURITY-EXCEPTION.json").read_text())["status"], "closed")
            runner.az.side_effect = [True, [resource], {}, {"properties": {"publicNetworkAccess": "Enabled"}}]
            with self.assertRaises(deploy.DeploymentError):
                deploy.close_bootstrap(runner, args, names, Path(directory).resolve())

    def test_verification_retries_model_free_reads_eleven_times(self):
        outputs = {"staticSiteUrl": "https://example.azurestaticapps.net", "functionUrl": "https://example.azurewebsites.net"}
        fetch, sleep = Mock(return_value=(503, b"")), Mock()
        with patch.object(deploy, "fetch", fetch), self.assertRaises(deploy.DeploymentError):
            deploy.verify_deployment(outputs, {"speeches": [{"id": "source"}]}, Path("/web"), sleep)
        self.assertEqual(fetch.call_count, 11)
        self.assertEqual(sleep.call_count, 10)
        self.assertFalse(any("/api/ask" in call.args[0] for call in fetch.call_args_list))

    def test_freezing_copies_only_reviewed_artifacts_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory).resolve() / "source", Path(directory).resolve() / "data"
            source.mkdir()
            for name in deploy.ARTIFACTS | {"manifest.json", "trial.json", ".env"}:
                (source / name).write_text("{}")
            with patch.object(deploy, "load_bundle", return_value={"snapshots": {"same": True}}):
                deploy.freeze_corpus(source, destination)
                self.assertEqual({path.name for path in destination.iterdir()}, deploy.ARTIFACTS | {"manifest.json"})
                with self.assertRaises(FileExistsError):
                    deploy.freeze_corpus(source, destination)

    def test_owned_private_endpoint_cannot_switch_data_modes_before_any_provisioning(self):
        args = deploy.arguments([*BASE, "--create-data", "--apply", "--publisher-ip", "93.184.216.34"])
        names = deploy.profile(args)
        target, _ = deploy.target_and_config(args, names, {"manifest_sha256": "sha256:" + "a" * 64})
        resource = {
            "id": "/existing-endpoint", "name": "pe-fiq-web-search-" + names["suffix"],
            "tags": {"recreationOwner": names["owner"]},
        }
        runner = Mock()

        def response(*command, **kwargs):
            return {
                ("cloud", "show"): {"name": "AzureCloud"},
                ("account", "show"): {"state": "Enabled"},
                ("functionapp", "list-flexconsumption-locations"): [{"name": "eastus2"}],
                ("group", "exists"): True,
                ("resource", "list"): [resource],
                ("resource", "show"): {"properties": {
                    "privateLinkServiceConnections": [{"properties": {"privateLinkServiceId": "/old-search"}}],
                }},
            }.get(command[:2], {})

        runner.az.side_effect = response
        with tempfile.TemporaryDirectory() as directory, patch.object(
            deploy, "ROOT", Path(directory).resolve()
        ), patch.object(deploy, "ensure_apis"):
            web = Path(directory) / "web"
            web.mkdir()
            (web / "swa-cli.config.json").write_text(json.dumps({
                "configurations": {"web": {"appLocation": ".", "outputLocation": "dist"}},
            }))
            with self.assertRaisesRegex(deploy.DeploymentError, "different data"):
                deploy.apply(args, names, {}, target, None, runner)
        self.assertFalse(any(call.args[:3] == ("deployment", "group", "create")
                             for call in runner.az.call_args_list))

    def test_cold_pipeline_closes_bootstrap_and_hardens_auth_before_api_publication(self):
        args = deploy.arguments([*BASE, "--create-data", "--apply", "--publisher-ip", "93.184.216.34"])
        names = deploy.profile(args)
        bundle = {"manifest_sha256": "sha256:" + "a" * 64}
        target, _ = deploy.target_and_config(args, names, bundle)
        config = json.loads((deploy.ROOT / "api/corpus.json").read_text())
        events = []
        outputs = {
            "staticSiteUrl": "https://test.azurestaticapps.net",
            "functionUrl": "https://test.azurewebsites.net", "functionId": "/function",
            "privateEndpointSubnetId": "/subnet", "virtualNetworkId": "/network",
        }

        class FakeRunner:
            def command(self, command, **kwargs):
                events.append(("command", command))

            def az(self, *command, **kwargs):
                events.append(("az", command))
                if command[:2] == ("cloud", "show"):
                    return {"name": "AzureCloud"}
                if command[:2] == ("account", "show"):
                    return {"state": "Enabled"}
                if command[:2] == ("functionapp", "list-flexconsumption-locations"):
                    return [{"name": "eastus2"}]
                if command[:2] == ("group", "exists"):
                    return False
                if "https://graph.microsoft.com/v1.0/me?$select=id" in command:
                    return {"id": SUBSCRIPTION}
                if command[:3] == ("staticwebapp", "backends", "show"):
                    return []
                if command[:3] == ("rest", "--method", "get"):
                    return {"properties": {
                        "platform": {"enabled": True}, "httpSettings": {"requireHttps": True},
                        "globalValidation": {"requireAuthentication": True, "unauthenticatedClientAction": "Return401"},
                        "identityProviders": {"azureStaticWebApps": {
                            "enabled": True, "registration": {"clientId": "test.azurestaticapps.net"},
                        }},
                    }}
                return {}

        def publish(bundle, rest, report, **kwargs):
            deploy.atomic_create_json(kwargs["api_config"], config)
            deploy.atomic_create_json(report, {"status": "verified"})
            events.append(("published-data", None))

        def template(runner, args, name, parameters, workspace, deployment):
            events.append((deployment, parameters))
            return outputs

        with tempfile.TemporaryDirectory() as directory, patch.object(
            deploy, "ROOT", Path(directory).resolve()
        ), patch.object(deploy, "ensure_apis"), patch.object(
            deploy, "choose_model", return_value="2026-07-09"
        ), patch.object(deploy, "deploy_template", side_effect=template), patch.object(
            deploy, "publish", side_effect=publish
        ), patch.object(deploy, "close_bootstrap", side_effect=lambda *args: events.append(("closed", None))), patch.object(
            deploy, "SearchREST", return_value=SimpleNamespace(request=lambda *args: SimpleNamespace(status_code=200))
        ), patch("azure.identity.AzureCliCredential"), patch("httpx.Client"), patch.object(
            deploy, "secure_frontend_deploy"
        ), patch.object(deploy, "verify_deployment"):
            api = Path(directory) / "api"
            api.mkdir()
            web = Path(directory) / "web"
            web.mkdir()
            (web / "swa-cli.config.json").write_text(json.dumps({
                "configurations": {"web": {"appLocation": ".", "outputLocation": "dist"}},
            }))
            for name in deploy.FILES:
                if name != "corpus.json":
                    (api / name).write_text("{}")
            deploy.apply(args, names, bundle, target, None, FakeRunner())
        closed = next(i for i, event in enumerate(events) if event[0] == "closed")
        auth = next(i for i, event in enumerate(events) if event[0] == "speech-auth")
        publication = next(i for i, event in enumerate(events) if event[0] == "az" and event[1][:4]
                           == ("functionapp", "deployment", "source", "config-zip"))
        self.assertLess(closed, auth)
        self.assertLess(auth, publication)
        self.assertTrue(events[auth][1]["hardenLinkedAuthentication"])


if __name__ == "__main__":
    unittest.main()
