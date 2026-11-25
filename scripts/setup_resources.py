#!/usr/bin/env python3
r"""Provision resources for create_synthetic_graph.py (Python 3.10+, az, Bicep).

Default: offline resource/cost plan, no CLI/network calls or files written.
  python3 scripts/setup_resources.py --subscription YOUR_SUBSCRIPTION_ID

Apply the public-endpoint synthetic-demo exception explicitly:
  python3 scripts/setup_resources.py --subscription YOUR_SUBSCRIPTION_ID \
    --resource-group my-graph-rg --location eastus --apply --allow-public-demo

Requires pre-existing permission to create a resource group, Search, Storage,
Foundry accounts/deployments and resource-scoped role assignments. No Owner or
Contributor grants are made. Azure CLI must already be signed in. For a
service principal, specify --principal-id OBJECT_ID --principal-type ServicePrincipal.
Models must be generally available in the selected location with sufficient quota.
GlobalStandard can process outside the selected region; DataZoneStandard keeps
processing within the deployment's supported data zone.
Basic Search remains billable; free semantic/retrieval plans are separate from
the Search SKU. Graph preview support is checked by the graph script, not ARM.
Model capacity units are model-specific throughput allocations, not spend caps.

An existing resource group is reused without changing its tags or location.
Unrelated resources are preserved; planned resource-name collisions are refused.
On failure, no automatic rollback or rerun occurs; inspect the deployment before
cleanup. Never delete a reused group to clean up this demo.
The absence check and group creation are not atomic: don't run this concurrently.
After setup, the nonsecret manifest and printed command connect the graph script.
RBAC propagation may take several minutes; setup is not proof of ingestion.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_APIS = {
    "Microsoft.Search": {"searchServices": "2026-09-01-preview"},
    "Microsoft.Storage": {"storageAccounts": "2026-09-01", "storageAccounts/blobServices": "2026-09-01"},
    "Microsoft.CognitiveServices": {"accounts": "2026-09-01", "accounts/deployments": "2026-09-01"},
    "Microsoft.Authorization": {"roleAssignments": "2022-04-01"},
}


def az(*arguments, timeout=120):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("AZURE_STORAGE_")}
    try:
        result = subprocess.run(
            ["az", *arguments, "--only-show-errors", "--output", "json"],
            capture_output=True, text=True, env=env, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Azure CLI missing or timed out. Inspect state before retrying.") from None
    if result.returncode:
        raise RuntimeError(
            f"Azure CLI {' '.join(arguments[:3])} failed. "
            "Check permissions, quota, policy and deployment operations in the portal."
        )
    return json.loads(result.stdout or "{}")


def names(subscription, prefix, resource_group=None):
    seed = f"{subscription}/{prefix}"
    if resource_group:
        seed += f"/{resource_group.lower()}"
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:10]
    return {
        "resourceGroup": resource_group or f"{prefix}-{suffix}-rg",
        "search": f"srch-{prefix}-{suffix}",
        "storage": f"st{prefix.replace('-', '')}{suffix}",
        "foundry": f"ai-{prefix}-{suffix}",
    }


def choose_model(catalog, name, version, sku_name, capacity, usage, location="eastus"):
    candidates = [
        row["model"] for row in catalog
        if row.get("kind") == "AIServices"
        and row.get("model", {}).get("name") == name
        and row["model"].get("format") == "OpenAI"
        and row["model"].get("lifecycleStatus") == "GenerallyAvailable"
        and (row["model"].get("version") == version if version else row["model"].get("isDefaultVersion"))
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"No unambiguous GA version of {name} in {location}; specify a supported version.")
    model = candidates[0]
    skus = [sku for sku in model.get("skus", []) if sku.get("name") == sku_name]
    if len(skus) != 1:
        raise RuntimeError(f"{name} does not support {sku_name} in {location}.")
    sku = skus[0]
    limits = sku.get("capacity") or {}
    minimum = limits.get("minimum") or 1
    step = limits.get("step") or 1
    if (
        capacity < minimum
        or capacity > (limits.get("maximum") or capacity)
        or (capacity - minimum) % step
        or (limits.get("allowedValues") and capacity not in limits["allowedValues"])
    ):
        raise RuntimeError(f"Requested {name} capacity does not satisfy catalog limits.")
    matches = [
        item for item in usage
        if item.get("name", {}).get("value", "").lower() == (sku.get("usageName") or "").lower()
    ]
    if len(matches) != 1 or matches[0].get("limit", 0) - matches[0].get("currentValue", 0) < capacity:
        raise RuntimeError(f"Insufficient or unverified {location} quota for {name}/{sku_name}.")
    return str(model["version"])


def graph_arguments(args, resource_names):
    base = f"/subscriptions/{args.subscription}/resourceGroups/{resource_names['resourceGroup']}/providers"
    return [
        "--search-resource-id", f"{base}/Microsoft.Search/searchServices/{resource_names['search']}",
        "--storage-resource-id", f"{base}/Microsoft.Storage/storageAccounts/{resource_names['storage']}",
        "--model-resource-id", f"{base}/Microsoft.CognitiveServices/accounts/{resource_names['foundry']}",
        "--chat-deployment", "chat", "--chat-model", args.chat_model,
        "--embedding-deployment", "embedding", "--embedding-model", args.embedding_model,
        "--prefix", args.prefix,
    ]


def check_resource_collisions(args, resource_names):
    existing = az(
        "resource", "list", "--subscription", args.subscription,
        "--resource-group", resource_names["resourceGroup"],
    )
    planned = {resource_id.lower() for resource_id in graph_arguments(args, resource_names)[1:6:2]}
    if any(resource["id"].lower() in planned for resource in existing):
        raise RuntimeError("A planned resource already exists in this group. Use another --prefix; no resources are overwritten.")


def check_management_apis(subscription):
    for namespace, resources in REQUIRED_APIS.items():
        provider = az("provider", "show", "--namespace", namespace, "--subscription", subscription)
        types = {
            item["resourceType"].lower(): {version.lower() for version in item.get("apiVersions", [])}
            for item in provider.get("resourceTypes", [])
        }
        for resource_type, version in resources.items():
            if version.lower() not in types.get(resource_type.lower(), set()):
                raise RuntimeError(f"Required management API {namespace}/{resource_type} {version} is not available in this subscription.")


def deploy(args, resource_names):
    account = az("account", "show", "--subscription", args.subscription)
    if account.get("state") != "Enabled" or az("cloud", "show").get("name") != "AzureCloud":
        raise RuntimeError("Requires an enabled subscription in Azure public cloud.")
    check_management_apis(args.subscription)
    if az("group", "exists", "--subscription", args.subscription, "--name", resource_names["resourceGroup"]):
        check_resource_collisions(args, resource_names)
    principal = args.principal_id
    if not principal:
        if args.principal_type != "User":
            raise RuntimeError("ServicePrincipal requires an explicit --principal-id.")
        principal = az("ad", "signed-in-user", "show")["id"]
    uuid.UUID(principal)
    catalog = az("cognitiveservices", "model", "list", "--location", args.location, "--subscription", args.subscription)
    usage = az("cognitiveservices", "usage", "list", "--location", args.location, "--subscription", args.subscription)
    chat_version = choose_model(catalog, args.chat_model, args.chat_version, args.model_sku, args.chat_capacity, usage, args.location)
    embedding_version = choose_model(catalog, args.embedding_model, args.embedding_version, args.model_sku, args.embedding_capacity, usage, args.location)
    print(f"Checked {args.location} catalog/quota: {args.chat_model} {chat_version}; {args.embedding_model} {embedding_version}.")
    parameters = {
        "networkApproval": "approved-synthetic-demo",
        "location": args.location,
        "searchName": resource_names["search"], "storageName": resource_names["storage"],
        "foundryName": resource_names["foundry"], "principalId": principal,
        "principalType": args.principal_type, "searchSku": args.search_sku,
        "chatModel": args.chat_model, "chatVersion": chat_version,
        "embeddingModel": args.embedding_model, "embeddingVersion": embedding_version,
        "modelSku": args.model_sku, "chatCapacity": args.chat_capacity,
        "embeddingCapacity": args.embedding_capacity,
    }
    owner = str(uuid.uuid4())
    with tempfile.TemporaryDirectory(prefix="graph-resources-") as temporary:
        template = Path(temporary) / "main.json"
        az("bicep", "build", "--file", str(ROOT / "infra/main.bicep"), "--outfile", str(template))
        parameter_file = Path(temporary) / "parameters.json"
        parameter_file.write_text(json.dumps({
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {name: {"value": value} for name, value in parameters.items()},
        }), encoding="utf-8")
        # Recheck immediately before creating; no concurrent use is supported.
        group_created = not az(
            "group", "exists", "--subscription", args.subscription,
            "--name", resource_names["resourceGroup"],
        )
        if group_created:
            group = az(
                "group", "create", "--subscription", args.subscription,
                "--name", resource_names["resourceGroup"], "--location", args.location,
                "--tags", "purpose=synthetic-entity-graph", f"setupRun={owner}",
            )
            if group.get("tags", {}).get("setupRun") != owner:
                raise RuntimeError("Resource group ownership is uncertain; refusing deployment.")
        check_resource_collisions(args, resource_names)
        print(f"{'Created' if group_created else 'Reusing'} resource group {resource_names['resourceGroup']}.")
        arguments = [
            "--subscription", args.subscription, "--resource-group", resource_names["resourceGroup"],
            "--name", f"{args.prefix}-resources", "--template-file", str(template),
            "--parameters", f"@{parameter_file}", "--mode", "Incremental",
        ]
        started = time.monotonic()
        print("Validating incremental resource deployment.", flush=True)
        az("deployment", "group", "validate", *arguments, timeout=600)
        print("Deploying Search, Storage, Foundry models and scoped RBAC.", flush=True)
        result = az("deployment", "group", "create", *arguments, timeout=1800)
        if result.get("properties", {}).get("provisioningState") != "Succeeded":
            raise RuntimeError("ARM did not report successful provisioning.")
    outputs = result["properties"]["outputs"]
    for key, expected in zip(("searchResourceId", "storageResourceId", "modelResourceId"), graph_arguments(args, resource_names)[1:6:2]):
        if outputs.get(key, {}).get("value", "").lower() != expected.lower():
            raise RuntimeError("Deployment outputs did not match the planned resources.")
    manifest = {
        "resourceNames": resource_names, "subscription": args.subscription,
        "location": args.location, "resourceGroupCreated": group_created,
        "chatModelVersion": chat_version, "embeddingModelVersion": embedding_version,
        "principalId": principal, "modelSku": args.model_sku,
        "versionUpgradeOption": "NoAutoUpgrade",
        "graphArguments": graph_arguments(args, resource_names),
        "status": "ARM provisioning succeeded; ingestion/RBAC propagation not yet verified",
    }
    with args.output.open("x", encoding="utf-8") as destination:
        json.dump(manifest, destination, indent=2)
        destination.write("\n")
    print(f"Resources provisioned in {time.monotonic() - started:.1f}s. Manifest: {args.output}")
    print("No graph was ingested. RBAC can take several minutes to propagate.")
    print("Next, review the graph plan:")
    print(shlex.join([sys.executable, str(ROOT / "scripts/create_synthetic_graph.py"), *manifest["graphArguments"]]))
    print("Add --apply --query to that command to create and query the synthetic graph.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--subscription", required=True, help="Azure subscription UUID; no global account switch.")
    parser.add_argument("--prefix", default="graphdemo", help="Resource-name prefix, 3-10 lowercase letters/digits/dashes.")
    parser.add_argument("--resource-group", "--rg", dest="resource_group", help="Reuse this group or create it if absent; default is a generated name.")
    parser.add_argument("--location", default="eastus", help="Location for new resources and a new group; default: eastus. Existing group metadata is preserved.")
    parser.add_argument("--search-sku", choices=("basic", "standard", "standard2"), default="basic")
    parser.add_argument("--chat-model", default="gpt-5.6-luna")
    parser.add_argument("--embedding-model", default="text-embedding-3-small")
    parser.add_argument("--chat-version")
    parser.add_argument("--embedding-version")
    parser.add_argument("--model-sku", choices=("GlobalStandard", "DataZoneStandard", "Standard"), default="GlobalStandard")
    parser.add_argument("--chat-capacity", type=int, default=10)
    parser.add_argument("--embedding-capacity", type=int, default=1)
    parser.add_argument("--principal-id", help="Object ID receiving runtime roles; defaults to signed-in user at apply.")
    parser.add_argument("--principal-type", choices=("User", "ServicePrincipal"), default="User")
    parser.add_argument("--output", type=Path, default=Path("graph-resources.json"), help="New nonsecret output manifest; never overwritten.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-public-demo", action="store_true", help="Explicitly approve public endpoints, scoped grants and unknown resource costs.")
    args = parser.parse_args(argv)
    resource_names = None
    try:
        args.subscription = str(uuid.UUID(args.subscription))
        if args.resource_group and (
            not re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", args.resource_group)
            or args.resource_group.endswith(".")
        ):
            raise ValueError("--resource-group must be 1-90 letters/digits/underscores/parentheses/dashes/dots, not ending in a dot.")
        args.location = "".join(args.location.lower().split())
        if not re.fullmatch(r"[a-z][a-z0-9]{1,49}", args.location):
            raise ValueError("--location must be an Azure region name, such as eastus or westus3.")
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", args.prefix) or not 3 <= len(args.prefix) <= 10:
            raise ValueError("--prefix must be 3-10 lowercase letters/digits/single dashes, starting with a letter.")
        if args.principal_id:
            args.principal_id = str(uuid.UUID(args.principal_id))
        if args.chat_capacity < 1 or args.embedding_capacity < 1:
            raise ValueError("Model capacity must be positive.")
        for name in (args.chat_model, args.embedding_model, args.chat_version or "default", args.embedding_version or "default"):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ValueError("Invalid model/version name.")
        if args.apply and not args.allow_public_demo:
            raise ValueError("--apply requires --allow-public-demo. This is not production infrastructure.")
        resource_names = names(args.subscription, args.prefix, args.resource_group)
        print(json.dumps({
            "mode": "apply" if args.apply else "offline dry-run",
            "region": args.location, "newResources": resource_names,
            "managementApiVersions": REQUIRED_APIS,
            "resourceGroupPolicy": "Create if absent; otherwise preserve group metadata and unrelated resources. Refuse planned resource collisions.",
            "search": {"sku": args.search_sku, "replicas": 1, "partitions": 1, "semanticPlan": "free", "retrievalPlan": "free"},
            "storage": "LRS, private blobs, HTTPS/TLS 1.2, no shared keys, 7-day soft delete, no versioning/HNS",
            "foundry": {"kind": "AIServices", "sku": "S0", "modelSku": args.model_sku,
                        "chat": args.chat_model, "chatCapacity": args.chat_capacity,
                        "embedding": args.embedding_model, "embeddingCapacity": args.embedding_capacity,
                        "versions": "Resolve default GA versions or validate explicit versions during apply."},
            "rbac": {"caller": "Search Service Contributor + Search Index Data Contributor on Search; Blob Data Contributor on Storage; OpenAI User on Foundry",
                     "searchIdentity": "Blob Data Reader on Storage; OpenAI User on Foundry",
                     "principal": args.principal_id or "signed-in user (resolved only on apply)"},
            "network": "Public HTTPS endpoints, Entra-only. Explicit synthetic-demo exception; no private endpoints.",
            "costs": "Unknown dollar estimate. Paid Search accrues continuously; Storage/soft-delete retention and model usage billed. No dollar cap.",
            "dataResidency": "GlobalStandard processing may occur outside the selected region.",
            "cleanup": "Manual, only after verifying ownership; never delete a reused group or unrelated resources. No automatic deletes or retries.",
            "graph": "Run printed create_synthetic_graph.py command after successful setup; no Topic Graph native executable required.",
        }, indent=2))
        if args.apply:
            if args.output.exists() or not args.output.parent.is_dir():
                raise ValueError("Output manifest already exists or parent directory is missing; choose --output.")
            deploy(args, resource_names)
        else:
            print("No CLI/network calls or files written. Review and rerun with --apply --allow-public-demo.")
        return 0
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        if args.apply and resource_names:
            print(
                f"Partial resources may remain in {resource_names['resourceGroup']}. "
                "Inspect the deployment before retrying or manually cleaning up; no deletion performed.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
