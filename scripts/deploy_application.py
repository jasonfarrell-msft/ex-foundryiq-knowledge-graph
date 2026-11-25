#!/usr/bin/env python3
"""One-command Speech Atlas deployment; offline plan by default, --apply to deploy.

Fast mode reuses the verified Search/Foundry binding. --create-data recreates
Search, Foundry, and all 7,422 passages from the tracked immutable corpus.
Never invokes a chat model, signs in, deletes resources, or grants broad roles.
"""

import argparse
import hashlib
import ipaddress
from importlib.metadata import PackageNotFoundError, version
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener

from package_speech_api import FILES, package
from publish_app_search_corpus import (
    ARTIFACTS, PublicationError, PublicationTarget, SearchREST,
    atomic_create_json, definitions, inspect_resource, load_bundle, publish, safe_path, scan_documents,
)
from setup_resources import choose_model


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "data/search-corpus"
SEARCH_API = "2026-11-01"


class DeploymentError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def write_json(path, value):
    path = safe_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def freeze_corpus(source, destination=BUNDLE):
    """Copy only the five validated publication artifacts, never trial graphs."""
    source, destination = safe_path(source), safe_path(destination)
    bundle = load_bundle(source)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "INCOMPLETE").touch()
    for name in sorted(ARTIFACTS | {"manifest.json"}):
        shutil.copyfile(source / name, destination / name)
    (destination / "INCOMPLETE").unlink()
    try:
        copied = load_bundle(destination)
        if copied["snapshots"] != bundle["snapshots"]:
            raise DeploymentError("Frozen corpus differs from validated input.")
    except BaseException:
        (destination / "INCOMPLETE").touch()
        raise
    return copied


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True, type=lambda value: str(uuid.UUID(value)))
    parser.add_argument("--resource-group", required=True, help="Dedicated hosting RG; existing unrelated resources remain untouched.")
    parser.add_argument("--location", default="eastus2", help="SWA and Flex hosting region.")
    parser.add_argument("--data-location", default="eastus", help="New Search/Foundry region for --create-data.")
    parser.add_argument("--data-resource-group", default="rg-foundry-graph-test", help="Existing Search RG in fast mode.")
    parser.add_argument("--suffix", help="Optional 8-12 lowercase hexadecimal naming suffix.")
    parser.add_argument("--create-data", action="store_true")
    parser.add_argument("--publisher-ip", help="Your public IPv4; only this /32 is temporarily admitted during cold publication.")
    parser.add_argument("--chat-model", default="gpt-5.6-luna")
    parser.add_argument("--chat-capacity", type=int, default=50, help="Matches the working sample's 50K TPM allocation; not a spending cap.")
    parser.add_argument("--principal-id", help="Publisher service-principal object ID; otherwise use the signed-in user.")
    parser.add_argument("--principal-type", choices=("User", "ServicePrincipal"), default="User")
    parser.add_argument("--bundle", type=Path, default=BUNDLE)
    parser.add_argument("--freeze-corpus", type=Path, help="Offline one-time export from a previously verified preparation.")
    parser.add_argument("--close-bootstrap", action="store_true", help="Recovery: close only this deployment's owned Search bootstrap.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    for value in (args.resource_group, args.data_resource_group):
        if not re.fullmatch(r"[A-Za-z0-9_().-]{1,90}", value) or value.endswith("."):
            parser.error("Resource group names must be safe ASCII Azure names.")
    for value in (args.location, args.data_location):
        if not re.fullmatch(r"[a-z0-9]{2,32}", value):
            parser.error("Use an Azure region name such as eastus2.")
    if args.suffix and not re.fullmatch(r"[a-f0-9]{8,12}", args.suffix):
        parser.error("Suffix must be 8-12 lowercase hexadecimal characters.")
    if not 1 <= args.chat_capacity <= 1000 or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", args.chat_model):
        parser.error("Invalid chat model or capacity.")
    if args.principal_id:
        args.principal_id = str(uuid.UUID(args.principal_id))
    if args.publisher_ip:
        try:
            address = ipaddress.IPv4Address(args.publisher_ip)
            if not address.is_global:
                raise ValueError()
            args.publisher_ip = str(address)
        except ValueError:
            parser.error("Publisher IP must be a globally routable IPv4 address, not a network range.")
    if args.create_data and args.apply and not args.close_bootstrap and not args.publisher_ip:
        parser.error("--create-data --apply requires --publisher-ip; never admit all public IPs.")
    if args.principal_type == "ServicePrincipal" and not args.principal_id:
        parser.error("--principal-type ServicePrincipal requires --principal-id.")
    return args


def profile(args):
    suffix = args.suffix or hashlib.sha256(f"{args.subscription}/{args.resource_group.lower()}".encode()).hexdigest()[:10]
    owner = hashlib.sha256(f"{args.subscription}/{args.resource_group.lower()}/{suffix}".encode()).hexdigest()[:20]
    return {
        "suffix": suffix, "owner": owner, "site": f"swa-fiq-speeches-{suffix}",
        "function": f"func-fiq-speeches-{suffix}", "storage": f"stfiqweb{suffix}",
        "search": f"srch-fiq-speeches-{suffix}", "foundry": f"ai-fiq-speeches-{suffix}",
    }


def target_and_config(args, names, bundle):
    if args.create_data:
        prefix = f"speeches-{names['suffix']}-{bundle['manifest_sha256'][7:19]}"
        target = PublicationTarget(
            endpoint=f"https://{names['search']}.search.windows.net",
            index=prefix + "-index", knowledge_source=prefix + "-source",
            knowledge_base=prefix + "-kb",
            model_uri=f"https://{names['foundry']}.cognitiveservices.azure.com",
            model_name=args.chat_model,
        )
        return target, None
    config = json.loads((ROOT / "api/corpus.json").read_text())
    if not (
        config.get("preparation_manifest_sha256") == bundle["manifest_sha256"]
        and config.get("coverage_verified") is True and config.get("mode") == "full"
        and config.get("api_version") == "2026-10-01"
        and config.get("speeches") == bundle["catalog"] and config.get("topics") == bundle["topics"]
        and config.get("source_count") == len(bundle["catalog"])
        and config.get("chunk_count") == len(bundle["docs"])
        and config.get("canonical_count") == bundle["manifest"]["canonical_speech_count"]
    ):
        raise DeploymentError("Fast-mode binding differs from the frozen corpus.")
    target = PublicationTarget(
        endpoint=config["endpoint"], index=config["content_index"],
        knowledge_source=config["knowledge_source"], knowledge_base=config["knowledge_base"],
    )
    return target, config


def plan(args, names, bundle, target):
    return {
        "mode": "apply" if args.apply else "offline-plan", "azure_writes": args.apply,
        "resource_group": args.resource_group, "hosting_region": args.location,
        "data": "create-and-publish" if args.create_data else "reuse-verified-existing",
        "search_endpoint": target.endpoint, "source_count": len(bundle["catalog"]),
        "chunk_count": len(bundle["docs"]), "model_queries": 0,
        "resources": names, "corpus_manifest": bundle["manifest_sha256"],
        "steps": [
            "Validate sign-in, regions, APIs, ownership; build/test frontend",
            "Cold mode: validate GA chat model/quota, create Search/Foundry, publish every passage and verify twice",
            "Provision private hosting; link empty Function; verify exclusive SWA authentication",
            "Verify Search bootstrap closed before publishing API; deploy allowlisted Python ZIP",
            "Deploy frontend with in-memory credential; rotate credential even after failure",
            "Verify anonymous catalog, managed-identity Search probe, direct Function 401 and exact frontend assets",
        ],
        "estimated_hosting_baseline_usd_month": 30.90 if args.create_data else 23.60,
        "cost_exclusions": "Search capacity, model usage, DNS, storage, Functions and private-link traffic; not a quote or spending cap.",
        "bootstrap": "Cold mode admits one explicit /32 temporarily. Interrupted runs may leave it open; Azure tags and SECURITY-EXCEPTION.json track recovery. Use --close-bootstrap --apply.",
        "foundry_network": "Private endpoint plus deny-by-default firewall; trusted Azure services exception with scoped Search managed identity for synthesis.",
    }


class Runner:
    def __init__(self, subscription):
        self.subscription = subscription

    def az(self, *args, timeout=1800):
        subscription = [] if args[0] == "cloud" else ["--subscription", self.subscription]
        command = ["az", *args, *subscription, "--only-show-errors", "--output", "json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise DeploymentError(f"Azure CLI {' '.join(args[:3])} failed; inspect scoped deployment operations.")
        try:
            return json.loads(result.stdout or "{}")
        except ValueError:
            raise DeploymentError("Azure CLI returned an invalid response.") from None

    def command(self, command, cwd=ROOT, env=None, timeout=1800):
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise DeploymentError(f"{Path(command[0]).name} {command[1] if len(command) > 1 else ''} failed; run that validation locally for details.")


def resource_id(args, kind, name, group=None):
    return f"/subscriptions/{args.subscription}/resourceGroups/{group or args.resource_group}/providers/{kind}/{name}"


def assert_owned(resource, names):
    if resource.get("tags", {}).get("recreationOwner") != names["owner"]:
        raise DeploymentError("Resource-name collision with an unowned resource; use a dedicated RG or another suffix.")


def close_bootstrap(runner, args, names, state):
    rid = resource_id(args, "Microsoft.Search/searchServices", names["search"])
    exists = runner.az("group", "exists", "--name", args.resource_group)
    resources = runner.az("resource", "list", "--resource-group", args.resource_group) if exists else []
    search = next((row for row in resources if row["id"].lower() == rid.lower()), None)
    if search is not None:
        assert_owned(search, names)
        runner.az("resource", "update", "--ids", rid, "--api-version", SEARCH_API,
                  "--set", "properties.publicNetworkAccess=Disabled", "properties.networkRuleSet.ipRules=[]",
                  "tags.bootstrapAccess=closed")
        actual = runner.az("resource", "show", "--ids", rid, "--api-version", SEARCH_API)
        if actual["properties"].get("publicNetworkAccess") != "Disabled":
            raise DeploymentError("Search bootstrap closure could not be verified.")
    write_json(state / "SECURITY-EXCEPTION.json", {"status": "closed", "search_resource_id": rid})


def ensure_python(argv):
    if sys.version_info < (3, 14):
        raise DeploymentError("--apply requires Python 3.14 or later; use python3.14.")
    try:
        from azure.identity import AzureCliCredential  # noqa: F401
        import httpx  # noqa: F401
        import azure.functions  # noqa: F401
        import aiohttp  # noqa: F401
        requirements = (ROOT / "api/requirements.txt").read_text().splitlines()
        if all(version(name.strip()) == expected.strip()
               for name, expected in (line.split("==", 1) for line in requirements if line.strip())):
            return
    except (ImportError, PackageNotFoundError):
        pass
    environment = safe_path(ROOT / ".venv")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "api/requirements-dev.txt")],
                   check=True, capture_output=True)
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), *argv])


def deploy_template(runner, args, template, parameters, workspace, name):
    path = workspace / (name + "-parameters.json")
    atomic_create_json(path, {"parameters": {key: {"value": value} for key, value in parameters.items()}})
    deployment = runner.az(
        "deployment", "group", "create", "--resource-group", args.resource_group,
        "--name", name, "--template-file", str(ROOT / "infra" / template),
        "--parameters", "@" + str(path), "--mode", "Incremental",
    )
    return {key: value["value"] for key, value in deployment["properties"].get("outputs", {}).items()}


def secure_frontend_deploy(runner, args, names, web):
    secret = runner.az("staticwebapp", "secrets", "list", "--name", names["site"],
                       "--resource-group", args.resource_group)["properties"]["apiKey"]
    if not isinstance(secret, str) or not secret:
        raise DeploymentError("Missing deployment credential.")
    try:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("SWA_")}
        env["SWA_CLI_DEPLOYMENT_TOKEN"] = secret
        runner.command(
            [str(web / "node_modules/.bin/swa"), "deploy", "web", "--env", "production", "--no-use-keychain"],
            cwd=web, env=env,
        )
    finally:
        # Never expose the reset response: Azure returns the new credential.
        runner.az("staticwebapp", "secrets", "reset-api-key", "--name", names["site"],
                  "--resource-group", args.resource_group)


def fetch(url):
    if not re.fullmatch(r"https://[a-z0-9.-]+\.(?:azurestaticapps.net|azurewebsites.net)(?:/[A-Za-z0-9_./?=-]*)?", url):
        raise DeploymentError("Unexpected verification destination.")
    try:
        with build_opener(NoRedirect()).open(url, timeout=15) as response:
            return response.status, response.read(4 * 1024 * 1024)
    except HTTPError as error:
        return error.code, b""
    except (URLError, TimeoutError):
        return 0, b""


def verify_deployment(outputs, config, web, sleep=time.sleep):
    for attempt in range(11):
        try:
            base = outputs["staticSiteUrl"]
            status, raw = fetch(base + "/api/catalog")
            catalog = json.loads(raw) if status == 200 else {}
            expected = {item["id"] for item in config["speeches"]}
            actual = [item["id"] for item in catalog.get("speeches", [])]
            if len(actual) != len(expected) or set(actual) != expected:
                raise DeploymentError("Deployed catalog differs from all-source binding.")
            status, raw = fetch(base + "/api/health?probe=search")
            if status != 200 or json.loads(raw).get("azure_connectivity") != "search_verified":
                raise DeploymentError("Deployed managed-identity Search probe failed.")
            if fetch(outputs["functionUrl"] + "/api/health")[0] != 401:
                raise DeploymentError("Direct Function ingress must return 401.")
            for path in [web / "dist/index.html", *sorted((web / "dist/assets").iterdir())]:
                relative = "/" if path.name == "index.html" else "/assets/" + path.name
                status, remote = fetch(base + relative)
                if status != 200 or remote != path.read_bytes():
                    raise DeploymentError("Deployed frontend does not match the tested build.")
            return
        except (DeploymentError, ValueError, KeyError):
            if attempt == 10:
                raise DeploymentError("Deployment verification exhausted 11 model-free attempts.") from None
            sleep(15)


def ensure_apis(runner, location):
    required = {
        "Microsoft.Search": {"searchServices": SEARCH_API},
        "Microsoft.CognitiveServices": {"accounts": "2026-11-01"},
        "Microsoft.Web": {"staticSites": "2025-05-01", "sites": "2026-08-01"},
        "Microsoft.Network": {"privateEndpoints": "2026-07-01"},
        "Microsoft.Storage": {"storageAccounts": "2026-09-01"},
    }
    for namespace, types in required.items():
        provider = runner.az("provider", "show", "--namespace", namespace)
        available = {item["resourceType"]: item.get("apiVersions", []) for item in provider["resourceTypes"]}
        if any(version not in available.get(kind, []) for kind, version in types.items()):
            raise DeploymentError("Required GA resource API is unavailable in this subscription.")
        if namespace == "Microsoft.Web":
            sites = next(item for item in provider["resourceTypes"] if item["resourceType"] == "staticSites")
            if location not in {value.replace(" ", "").lower() for value in sites.get("locations", [])}:
                raise DeploymentError("Choose a hosting region offered by both Static Web Apps and Flex Consumption.")


def apply(args, names, bundle, target, config, runner=None):
    runner = runner or Runner(args.subscription)
    state = safe_path(ROOT / ".deployments" / names["owner"])
    state.mkdir(parents=True, exist_ok=True)
    if args.close_bootstrap:
        close_bootstrap(runner, args, names, state)
        return
    exception = state / "SECURITY-EXCEPTION.json"
    if exception.exists() and json.loads(exception.read_text()).get("status") != "closed":
        print("Closing the previous tracked Search bootstrap before any other work.", flush=True)
        close_bootstrap(runner, args, names, state)
    if runner.az("cloud", "show")["name"] != "AzureCloud" or runner.az("account", "show")["state"] != "Enabled":
        raise DeploymentError("An existing sign-in to an enabled Azure public-cloud subscription is required.")
    ensure_apis(runner, args.location)
    locations = runner.az("functionapp", "list-flexconsumption-locations")
    if args.location not in {row["name"] for row in locations}:
        raise DeploymentError("Hosting region does not support Flex Consumption.")
    web = ROOT / "web"
    settings = json.loads((web / "swa-cli.config.json").read_text())["configurations"]["web"]
    if (settings.get("appLocation") != "." or settings.get("outputLocation") != "dist"
            or settings.get("apiLocation") not in (None, "")):
        raise DeploymentError("SWA CLI configuration must deploy only web/dist; the API must remain a linked Function.")
    if not (web / "node_modules/.bin/swa").exists():
        runner.command(["npm", "ci", "--replace-registry-host=never"], cwd=web)
    runner.command(["npm", "test"], cwd=web)
    runner.command(["npm", "run", "build"], cwd=web)
    runner.command([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "api/tests")],
                   env={**os.environ, "PYTHONPATH": str(ROOT / "api"), "SPEECH_DATASET": "sample"})
    for template in ("speech-app.bicep", "application-data.bicep", "foundry-private-endpoint.bicep"):
        runner.az("bicep", "build", "--file", str(ROOT / "infra" / template), "--stdout")
    exists = runner.az("group", "exists", "--name", args.resource_group)
    resources = runner.az("resource", "list", "--resource-group", args.resource_group) if exists else []
    planned_names = {
        names["site"], names["function"], names["storage"], "asp-fiq-web-" + names["suffix"],
        "vnet-fiq-web-" + names["suffix"], "nsg-fiq-web-" + names["suffix"],
        "pe-fiq-web-blob-" + names["suffix"], "pe-fiq-web-search-" + names["suffix"],
        "privatelink.blob.core.windows.net", "privatelink.search.windows.net",
    }
    if args.create_data:
        planned_names |= {names["search"], names["foundry"], "pe-fiq-web-foundry-" + names["suffix"],
                          "privatelink.cognitiveservices.azure.com"}
    for resource in resources:
        if resource["name"] in planned_names:
            assert_owned(resource, names)
        if resource["name"] == "pe-fiq-web-search-" + names["suffix"]:
            endpoint = runner.az("resource", "show", "--ids", resource["id"], "--api-version", "2026-07-01")
            connections = endpoint["properties"].get("privateLinkServiceConnections", [])
            data_group = args.resource_group if args.create_data else args.data_resource_group
            search_name = target.endpoint.split("://", 1)[1].split(".", 1)[0]
            expected = resource_id(args, "Microsoft.Search/searchServices", search_name, data_group)
            if len(connections) != 1 or connections[0]["properties"]["privateLinkServiceId"].lower() != expected.lower():
                raise DeploymentError("Existing private endpoint uses different data; use a fresh RG when changing data modes.")
    if any(resource["name"] == names["site"] for resource in resources):
        backends = runner.az("staticwebapp", "backends", "show", "--name", names["site"],
                             "--resource-group", args.resource_group)
        expected_function = resource_id(args, "Microsoft.Web/sites", names["function"])
        if backends and (len(backends) != 1 or backends[0]["backendResourceId"].lower() != expected_function.lower()):
            raise DeploymentError("Existing site has a foreign backend; no resources were changed.")
    if not exists:
        runner.az("group", "create", "--name", args.resource_group, "--location", args.location)
    from azure.identity import AzureCliCredential
    import httpx
    data_group = args.resource_group if args.create_data else args.data_resource_group
    search_name = target.endpoint.split("://", 1)[1].split(".", 1)[0]
    hosting = {
        "location": args.location, "suffix": names["suffix"], "searchName": search_name,
        "searchResourceGroup": data_group, "recreationOwner": names["owner"],
        "hardenLinkedAuthentication": False,
    }
    with tempfile.TemporaryDirectory(prefix="speech-atlas-deploy-") as directory:
        workspace = Path(directory).resolve()
        bootstrap = False
        try:
            if args.create_data:
                principal = args.principal_id or runner.az(
                    "rest", "--method", "get", "--url", "https://graph.microsoft.com/v1.0/me?$select=id"
                )["id"]
                catalog = runner.az("cognitiveservices", "model", "list", "--location", args.data_location)
                usage = runner.az("cognitiveservices", "usage", "list", "--location", args.data_location)
                if any(resource["name"] == names["foundry"] for resource in resources):
                    deployed = runner.az("cognitiveservices", "account", "deployment", "list",
                                         "--name", names["foundry"], "--resource-group", args.resource_group)
                    default = [row["model"] for row in catalog if row.get("kind") == "AIServices"
                               and row.get("model", {}).get("name") == args.chat_model
                               and row["model"].get("isDefaultVersion")
                               and row["model"].get("lifecycleStatus") == "GenerallyAvailable"]
                    if len(default) == 1:
                        sku = next((row for row in default[0].get("skus", []) if row["name"] == "GlobalStandard"), {})
                        for deployment in deployed:
                            model = deployment.get("properties", {}).get("model", {})
                            if (deployment["name"] == "chat" and model.get("name") == args.chat_model
                                    and model.get("version") == default[0]["version"]
                                    and deployment.get("sku", {}).get("name") == "GlobalStandard"):
                                for row in usage:
                                    if row.get("name", {}).get("value", "").lower() == sku.get("usageName", "").lower():
                                        row["currentValue"] -= deployment["sku"]["capacity"]
                version = choose_model(catalog, args.chat_model, None, "GlobalStandard", args.chat_capacity, usage, args.data_location)
                write_json(state / "SECURITY-EXCEPTION.json", {
                    "status": "closure-required", "owner": names["owner"],
                    "search_resource_id": resource_id(args, "Microsoft.Search/searchServices", names["search"]),
                    "recovery": "--subscription YOUR_SUBSCRIPTION --resource-group YOUR_RG --close-bootstrap --apply",
                })
                bootstrap = True
                deploy_template(runner, args, "application-data.bicep", {
                    "location": args.data_location, "searchName": names["search"], "foundryName": names["foundry"],
                    "principalId": principal, "principalType": args.principal_type,
                    "chatModel": args.chat_model, "chatVersion": version, "chatCapacity": args.chat_capacity,
                    "publisherIp": args.publisher_ip, "recreationOwner": names["owner"],
                }, workspace, "speech-data")
            with AzureCliCredential(subscription=args.subscription, process_timeout=30) as credential, httpx.Client(
                timeout=60, follow_redirects=False, trust_env=False
            ) as client:
                rest = SearchREST(client, credential, target.endpoint)
                if args.create_data:
                    for attempt in range(11):
                        response = rest.request("GET", "/indexes")
                        if response.status_code == 200:
                            break
                        if attempt == 10:
                            raise DeploymentError("Publisher role/network readiness exhausted 11 attempts.")
                        time.sleep(15)
                    model_file = workspace / "keyless-model.json"
                    atomic_create_json(model_file, {"models": target.models})
                    publish(bundle, rest, workspace / "verification.json", target=target,
                            api_config=workspace / "corpus.json", model_file=model_file)
                    config = json.loads((workspace / "corpus.json").read_text())
                    shutil.copyfile(workspace / "verification.json", state / "publication-verification.json")
                else:
                    scan_documents(rest, bundle, index=target.index)
                    for kind, definition in definitions(bundle, target).items():
                        if not inspect_resource(rest, kind, definition):
                            raise DeploymentError("A verified existing Search resource is missing; use --create-data for cold recreation.")
                outputs = deploy_template(runner, args, "speech-app.bicep", hosting, workspace, "speech-hosting")
                if args.create_data:
                    deploy_template(runner, args, "foundry-private-endpoint.bicep", {
                        "foundryName": names["foundry"], "subnetId": outputs["privateEndpointSubnetId"],
                        "virtualNetworkId": outputs["virtualNetworkId"], "location": args.location,
                        "suffix": names["suffix"], "recreationOwner": names["owner"],
                    }, workspace, "speech-foundry-network")
                    close_bootstrap(runner, args, names, state)
                    bootstrap = False
            backends = runner.az("staticwebapp", "backends", "show", "--name", names["site"], "--resource-group", args.resource_group)
            if backends:
                if len(backends) != 1 or backends[0]["backendResourceId"].lower() != outputs["functionId"].lower():
                    raise DeploymentError("Static Web App is linked to a different backend; refusing relink.")
            else:
                runner.az("staticwebapp", "backends", "link", "--name", names["site"], "--resource-group", args.resource_group,
                          "--backend-resource-id", outputs["functionId"], "--backend-region", args.location)
            hosting["hardenLinkedAuthentication"] = True
            outputs = deploy_template(runner, args, "speech-app.bicep", hosting, workspace, "speech-auth")
            auth = runner.az("rest", "--method", "get", "--url", outputs["functionId"]
                             + "/config/authsettingsV2?api-version=2026-08-01")["properties"]
            providers = auth["identityProviders"]
            if not (auth["platform"]["enabled"] and auth["globalValidation"]["requireAuthentication"]
                    and auth["globalValidation"]["unauthenticatedClientAction"] == "Return401"
                    and auth["httpSettings"]["requireHttps"]
                    and providers["azureStaticWebApps"]["enabled"]
                    and providers["azureStaticWebApps"]["registration"]["clientId"]
                    == outputs["staticSiteUrl"].removeprefix("https://")
                    and not any(provider.get("enabled") for name, provider in providers.items()
                                if name != "azureStaticWebApps")):
                raise DeploymentError("Linked authentication was not hardened; no application code will be published.")
            api = workspace / "api"
            api.mkdir()
            for name in FILES:
                if name != "corpus.json":
                    shutil.copyfile(ROOT / "api" / name, api / name)
            atomic_create_json(api / "corpus.json", config)
            archive = workspace / "api.zip"
            package(archive, api=api)
            runner.az("functionapp", "deployment", "source", "config-zip", "--resource-group", args.resource_group,
                      "--name", names["function"], "--src", str(archive), "--build-remote", "true")
            secure_frontend_deploy(runner, args, names, web)
            verify_deployment(outputs, config, web)
            write_json(state / "deployment.json", {
                "status": "verified-model-free", "outputs": outputs, "corpus_manifest": bundle["manifest_sha256"],
                "source_count": 1060, "chunk_count": 7422, "model_queries": 0,
            })
            print("Verified deployment: " + outputs["staticSiteUrl"])
        finally:
            if bootstrap:
                try:
                    close_bootstrap(runner, args, names, state)
                except Exception:
                    print("SECURITY: Search bootstrap closure is unverified. See SECURITY-EXCEPTION.json and run --close-bootstrap --apply.", file=sys.stderr)
                    raise DeploymentError("Bootstrap cleanup failed; no verified deployment can be claimed.") from None


def main(argv=None):
    args = arguments(argv)
    try:
        if args.freeze_corpus:
            if args.apply:
                raise DeploymentError("Corpus export is offline only.")
            freeze_corpus(args.freeze_corpus, args.bundle)
            print("Frozen all-source corpus exported and verified; no Azure calls.")
            return 0
        names = profile(args)
        # Recovery must not depend on corpus, build, model quota, or publication.
        if args.close_bootstrap:
            if args.apply:
                apply(args, names, None, None, None)
            else:
                print("Offline plan: close only the owned Search bootstrap; add --apply to execute.")
            return 0
        bundle = load_bundle(args.bundle)
        target, config = target_and_config(args, names, bundle)
        print(json.dumps(plan(args, names, bundle, target), indent=2), flush=True)
        if args.apply:
            ensure_python(sys.argv[1:] if argv is None else argv)
            apply(args, names, bundle, target, config)
        return 0
    except (DeploymentError, PublicationError, OSError, ValueError, KeyError, TypeError, ImportError,
            subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, (DeploymentError, PublicationError)) else type(error).__name__
        print("Deployment failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
