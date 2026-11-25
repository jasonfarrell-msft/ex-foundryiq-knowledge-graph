#!/usr/bin/env python3
r"""Create a tiny managed entity graph, not a local Topic Graph.

Python 3.10+ and a signed-in Azure CLI are required; no pip packages or keys.
Run --help for arguments. Default execution prints an offline plan only.
Pass --apply to create a private container, two blobs, a knowledge source and
a NEW knowledge base on existing resources. No infrastructure, roles or network
settings are changed. Use --query to run one direct GQL query after ingestion.

Example (replace the resource IDs with your existing resources):
  python3 scripts/create_synthetic_graph.py \
    --search-resource-id /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.Search/searchServices/SEARCH \
    --storage-resource-id /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.Storage/storageAccounts/STORAGE \
    --model-resource-id /subscriptions/SUB/resourceGroups/RG/providers/Microsoft.CognitiveServices/accounts/FOUNDRY \
    --chat-deployment CHAT --chat-model MODEL \
    --embedding-deployment EMBEDDING --embedding-model text-embedding-3-small

Preconditions: paid Entra-only Search with system-assigned managed identity;
Blob soft delete enabled, versioning and hierarchical namespace disabled.
Caller: Search Service Contributor, Search Index Data Contributor, Storage
Blob Data Contributor, and management read access to the supplied resources.
Search identity: Storage Blob Data Reader on Storage and Cognitive Services
OpenAI User on the model resource. Effective downstream access and preview
availability are verified by ingestion, not by management-plane reads.
Private resources require working private connectivity from this host and Search.

API contract:
https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-entity-graph
This feature requires 2026-11-01-preview; GA does not support this operation.
The attached ontology cannot be removed. No ingestion cache is configured, so
later ontology updates are unsupported. Cleanup is manual: knowledge base,
knowledge source (owns generated Search objects), then the sample container.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_VERSION = "2026-11-01-preview"
DOCUMENTS = {
    "roster.txt": (
        "Avery Chen works for Contoso Robotics.\n"
        "Contoso Robotics is located in Seattle.\n"
        "Jordan Davis works for Fabrikam Analytics.\n"
        "Fabrikam Analytics is located in Austin.\n"
    ),
    "avery.txt": (
        "Avery Chen is employed by Contoso Robotics, "
        "an organization located in Seattle.\n"
    ),
}
GQL = (
    "MATCH (person:Person)-[job]->(org:Organization)-[location]->(city:Place) "
    "WHERE job.predicate = 'worksFor' AND location.predicate = 'locatedIn' "
    "AND city.name = 'Seattle' RETURN person.name AS person, "
    "org.name AS organization, city.name AS city LIMIT 10"
)


def resource_parts(value, provider, resource_type):
    match = re.fullmatch(
        r"/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)/"
        r"providers/" + re.escape(provider) + "/" + re.escape(resource_type)
        + r"/([a-zA-Z0-9-]+)",
        value,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Expected an Azure {provider}/{resource_type} resource ID.")
    subscription, group, name = match.groups()
    uuid.UUID(subscription)
    return ["--subscription", subscription, "--resource-group", group, "--name", name]


def az(*arguments):
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("AZURE_STORAGE_")
    }
    try:
        result = subprocess.run(
            ["az", *arguments, "--only-show-errors", "--output", "json"],
            capture_output=True, text=True, env=env, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Azure CLI is missing or timed out; check az login.") from None
    if result.returncode:
        # CLI errors can contain credentials supplied by inherited configuration.
        raise RuntimeError(
            f"Azure CLI {' '.join(arguments[:3])} failed. "
            "Check sign-in, permissions, connectivity and resource names."
        )
    return json.loads(result.stdout or "{}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Search:
    def __init__(self, name, subscription):
        self.endpoint = f"https://{name}.search.windows.net"
        self.subscription = subscription
        self.opener = urllib.request.build_opener(NoRedirect())
        self.token = None
        self.expires = 0

    def request(self, method, path, payload=None, create=False, absent_ok=False):
        if time.time() >= self.expires - 120:
            token = az(
                "account", "get-access-token", "--subscription", self.subscription,
                "--resource", "https://search.azure.com",
            )
            self.token = token["accessToken"]
            self.expires = int(token["expires_on"])
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "synthetic-entity-graph-example",
            "x-ms-client-request-id": str(uuid.uuid4()),
            "Prefer": "return=representation",
        }
        if create:
            headers["If-None-Match"] = "*"
        request = urllib.request.Request(
            f"{self.endpoint}/{path}?api-version={API_VERSION}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers, method=method,
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 404 and absent_ok:
                return None
            raise RuntimeError(
                f"Search {method} failed with HTTP {error.code}. "
                "No write is retried automatically; inspect resource state before rerunning."
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError(
                "Search connection failed or timed out. A submitted write may have "
                "succeeded; inspect resource state before rerunning."
            ) from None


def ontology():
    nodes = [
        {"@id": f"ex:{name}", "@type": "owl:Class", "rdfs:comment": description}
        for name, description in (
            ("Person", "A person."), ("Organization", "An organization."),
            ("Place", "A geographic place."),
        )
    ]
    for name, domain, target, description in (
        ("worksFor", "Person", "Organization", "A person's employer."),
        ("locatedIn", "Organization", "Place", "The city where an organization is located."),
    ):
        nodes.append({
            "@id": f"ex:{name}", "@type": "owl:ObjectProperty",
            "rdfs:domain": {"@id": f"ex:{domain}"},
            "rdfs:range": {"@id": f"ex:{target}"}, "rdfs:comment": description,
        })
    return {
        "@context": {
            "owl": "http://www.w3.org/2002/07/owl#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "ex": "https://schema.search.azure.com/entities#",
        },
        "@graph": nodes,
    }


def payloads(args):
    model_name = args.model_resource_id.rsplit("/", 1)[1]
    endpoint = f"https://{model_name}.openai.azure.com"

    def model(deployment, name):
        return {
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "resourceUri": endpoint, "deploymentId": deployment, "modelName": name,
            },
        }

    chat = model(args.chat_deployment, args.chat_model)
    source = {
        "name": f"{args.prefix}-ks", "kind": "azureBlob",
        "description": "Fictional employees, employers and employer cities.",
        "resultsProcessing": "none",
        "azureBlobParameters": {
            "connectionString": f"ResourceId={args.storage_resource_id}",
            "containerName": args.prefix,
            "ingestionParameters": {
                "contentExtractionMode": "minimal", "disableImageVerbalization": True,
                "chatCompletionModel": chat,
                "embeddingModel": model(args.embedding_deployment, args.embedding_model),
            },
            "graphExtraction": {
                "graphName": args.prefix, "pageDegreeOfParallelism": 2,
                "ontology": {
                    "kind": "inline", "name": "employees-v1",
                    "value": json.dumps(ontology(), separators=(",", ":")),
                },
            },
        },
    }
    kb = {
        "name": f"{args.prefix}-kb", "knowledgeSources": [{"name": source["name"]}],
        "models": [chat], "retrievalReasoningEffort": {"kind": "low"},
        "outputMode": "extractiveData",
        "retrievalInstructions": (
            "Use graph_search for entity relationship questions. Answer the relationship "
            "question by traversing the graph, rather than using ordinary text search."
        ),
    }
    return source, kb


def preflight(args):
    search_flags = resource_parts(args.search_resource_id, "Microsoft.Search", "searchServices")
    storage_flags = resource_parts(args.storage_resource_id, "Microsoft.Storage", "storageAccounts")
    model_flags = resource_parts(args.model_resource_id, "Microsoft.CognitiveServices", "accounts")
    if az("cloud", "show").get("name") != "AzureCloud":
        raise RuntimeError("This example supports Azure public cloud only.")
    service = az("search", "service", "show", *search_flags)
    if (
        service.get("sku", {}).get("name", "").lower() in ("", "free")
        or service.get("disableLocalAuth") is not True
        or not service.get("identity", {}).get("principalId")
    ):
        raise RuntimeError("Search must be paid, Entra-only, and have a system-assigned identity.")
    storage = az("storage", "account", "show", *storage_flags)
    if storage.get("isHnsEnabled"):
        raise RuntimeError("This entity graph example does not support ADLS Gen2.")
    # This command uses the management plane; it does not retrieve Storage keys.
    properties = az(
        "storage", "account", "blob-service-properties", "show",
        "--subscription", storage_flags[1], "--resource-group", storage_flags[3],
        "--account-name", storage_flags[5],
    )
    if not properties.get("deleteRetentionPolicy", {}).get("enabled") or properties.get("isVersioningEnabled"):
        raise RuntimeError("Enable Blob soft delete and disable Blob versioning before ingestion.")
    for deployment, expected in (
        (args.chat_deployment, args.chat_model),
        (args.embedding_deployment, args.embedding_model),
    ):
        actual = az(
            "cognitiveservices", "account", "deployment", "show", *model_flags,
            "--deployment-name", deployment,
        )
        if actual.get("properties", {}).get("model", {}).get("name") != expected:
            raise RuntimeError(f"Deployment {deployment} does not match model {expected}.")
    client = Search(search_flags[5], search_flags[1])
    for kind, name in (("knowledgesources", f"{args.prefix}-ks"), ("knowledgebases", f"{args.prefix}-kb")):
        if client.request("GET", f"{kind}('{name}')", absent_ok=True) is not None:
            raise RuntimeError(f"{name} already exists; choose another --prefix.")
    print("Read-only checks passed. Downstream RBAC/network/API support is not yet proven.")
    print(f"Reusing Search in {service.get('location', 'unknown location')}; no resource groups created.")
    return client, storage_flags


def wait_for_graph(client, source_name, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.request("GET", f"knowledgesources('{source_name}')/status")
        last = status.get("lastSynchronizationState") or {}
        if last.get("endTime") and not status.get("currentSynchronizationState"):
            if last.get("itemsUpdatesFailed", 0) or last.get("errors"):
                raise RuntimeError("Ingestion reported failures; inspect knowledge source/indexer status.")
            if "itemsUpdatesFailed" in last:
                source = client.request("GET", f"knowledgesources('{source_name}')")
                graph = source.get("azureBlobParameters", {}).get("createdResources", {}).get("graphIndex")
                if graph:
                    return graph
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        "Timed out waiting for completed graph extraction. Ingestion may continue and "
        "incur charges; no resources were deleted."
    )


def apply(args, source, kb):
    started = time.monotonic()
    client, storage_flags = preflight(args)
    storage_arguments = [
        "--subscription", storage_flags[1], "--account-name", storage_flags[5],
        "--auth-mode", "login",
    ]
    print(f"Creating private container {args.prefix} (no overwrite).", flush=True)
    az(
        "storage", "container", "create", *storage_arguments,
        "--name", args.prefix, "--public-access", "off", "--fail-on-exist",
    )
    print(f"Container created after {time.monotonic() - started:.1f}s.", flush=True)
    with tempfile.TemporaryDirectory(prefix="synthetic-graph-") as temporary:
        for name, content in DOCUMENTS.items():
            path = Path(temporary) / name
            path.write_text(content, encoding="utf-8")
            az(
                "storage", "blob", "upload", *storage_arguments,
                "--container-name", args.prefix, "--name", name,
                "--file", str(path), "--overwrite", "false", "--content-type", "text/plain",
            )
    print("Two fictional documents uploaded; creating graph-enabled source.", flush=True)
    client.request("PUT", f"knowledgesources('{source['name']}')", source, create=True)
    print("Source submitted. Waiting for extraction; model/storage charges apply.", flush=True)
    graph = wait_for_graph(client, source["name"], args.timeout)
    print(f"Extraction complete after {time.monotonic() - started:.1f}s; graph index: {graph}")
    client.request("PUT", f"knowledgebases('{kb['name']}')", kb, create=True)
    saved = client.request("GET", f"knowledgebases('{kb['name']}')")
    if saved.get("name") != kb["name"] or [
        reference.get("name") for reference in saved.get("knowledgeSources", [])
    ] != [source["name"]]:
        raise RuntimeError("Knowledge base read-back did not match the requested source.")
    print(f"Knowledge base {kb['name']} verified. Total elapsed: {time.monotonic() - started:.1f}s.")
    print("Extraction completion is not proof of relationship accuracy.")
    if args.query:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", graph):
            raise RuntimeError("Unexpected graph index name; refusing to construct query URL.")
        result = client.request("POST", f"indexes('{graph}')/docs/search.graph", {
            "gql": GQL, "includeStatistics": True, "includeDiagnostics": True,
            "provenance": {
                "kind": "hydrate", "maxEvidencePerRow": 10,
                "hydrateDocuments": {"select": "uid,snippet,snippet_parent_id,blob_url"},
            },
        })
        print("Direct GQL result (expect Avery Chen / Contoso Robotics / Seattle):")
        print(json.dumps(result, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in (
        "search-resource-id", "storage-resource-id", "model-resource-id",
        "chat-deployment", "chat-model", "embedding-deployment", "embedding-model",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--prefix", default="synthetic-employees", help="New container/graph prefix; use a new name per run.")
    parser.add_argument("--apply", action="store_true", help="Approve the displayed writes and unknown usage costs.")
    parser.add_argument("--query", action="store_true", help="Run one direct GQL query after extraction.")
    parser.add_argument("--timeout", type=int, default=600, help="Polling limit in seconds, not a spending cap.")
    args = parser.parse_args(argv)
    try:
        for value, provider, kind in (
            (args.search_resource_id, "Microsoft.Search", "searchServices"),
            (args.storage_resource_id, "Microsoft.Storage", "storageAccounts"),
            (args.model_resource_id, "Microsoft.CognitiveServices", "accounts"),
        ):
            resource_parts(value, provider, kind)
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", args.prefix) or not 3 <= len(args.prefix) <= 40:
            raise ValueError("--prefix must be 3-40 lowercase letters/digits/single dashes, starting with a letter.")
        if not 1 <= args.timeout <= 3600:
            raise ValueError("--timeout must be 1-3600 seconds.")
        for name in (args.chat_deployment, args.chat_model, args.embedding_deployment, args.embedding_model):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
                raise ValueError("Model/deployment names must contain only letters, digits, dots, dashes or underscores.")
        source, kb = payloads(args)
        print(json.dumps({
            "mode": "apply" if args.apply else "offline dry-run",
            "apiVersion": API_VERSION,
            "reuse": [args.search_resource_id, args.storage_resource_id, args.model_resource_id],
            "create": {
                "privateContainer": args.prefix, "blobs": DOCUMENTS,
                "knowledgeSource": source, "knowledgeBase": kb,
                "managedSearchObjects": "Source-owned data source, indexer, skillset, chunk index and graph index.",
            },
            "costs": {
                "entityGraphFeature": "No additional feature charge during preview per Microsoft documentation.",
                "chatAndEmbeddingTokens": "Unknown; charged on ingestion.",
                "storageTransactionsAndRetention": "Unknown; ongoing until cleaned up.",
                "searchCapacity": "Existing paid service remains billable; this script does not resize it.",
                "optionalQuery": "One direct GQL query if selected; Search usage, no query-generation model call.",
            },
            "pollingLimitSeconds": args.timeout,
            "warning": "No dollar cap. Client timeout does not stop server ingestion. No automatic cleanup.",
            "cleanupOrder": [kb["name"], source["name"], f"Blob container {args.prefix}"],
        }, indent=2))
        if args.apply:
            apply(args, source, kb)
        else:
            print("No CLI/network calls or writes made. Review the plan, then rerun with --apply.")
        return 0
    except (ValueError, RuntimeError, KeyError):
        error = sys.exc_info()[1]
        print(f"Stopped: {error}", file=sys.stderr)
        if args.apply:
            print(
                f"Partial resources may remain: {args.prefix}-kb, {args.prefix}-ks, "
                f"container {args.prefix}. Check state before rerunning. Cleanup is manual.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
