# Building a Customer Knowledge Graph from Markdown

`scripts/build_markdown_graph.py` turns any directory of Markdown (`.md`) documents and an OWL/JSON-LD ontology into a class-scoped Azure AI Search chunk index and `graphProfile` (`edgeList`) graph index (`api-version=2026-11-01-preview`).

It uses **Microsoft Entra ID (`AzureCliCredential`)** only—no API keys or connection strings—and defaults to an **offline dry-run** with zero writes or network calls.

---

## Prerequisites

* Python 3.14+ with `.venv` dependencies installed (`api/requirements-dev.txt` provides `httpx` and `azure-identity`)
* Active Azure sign-in (`az login`)
* Paid Azure AI Search service (`Basic` or higher, Entra-enabled) with:
  * `Search Service Contributor` and `Search Index Data Contributor` for your principal
* Azure AI Foundry resource with a GA chat deployment (for `--extract`) and `Cognitive Services OpenAI User` for your principal

---

## Step-by-Step Workflow

### 1. Offline Dry-Run (Inspect Plan Only)

Verify discovered Markdown files, ontology classes/predicates, and planned index names with no disk writes or Azure calls:

```bash
PYTHONPATH=scripts .venv/bin/python scripts/build_markdown_graph.py \
  --input-dir documents \
  --ontology .foundry-iq/fiq-speeches-9c65c09f/ontology/speeches-grounded-v7.jsonld \
  --output-dir /tmp/my-graph \
  --prefix my-graph-v1
```

### 2. Local Chunking & Deterministic Metadata Edges (`--prepare`)

Parses YAML frontmatter (`--- ... ---`), `# Title` headings, and `**Key**: Value` / `Key: Value` header lines, chunks documents with overlap, and generates deterministic metadata edges (such as `delivered` or `givenOn` when those predicates exist in the ontology):

```bash
PYTHONPATH=scripts .venv/bin/python scripts/build_markdown_graph.py \
  --input-dir documents \
  --ontology .foundry-iq/fiq-speeches-9c65c09f/ontology/speeches-grounded-v7.jsonld \
  --entities .foundry-iq/fiq-speeches-9c65c09f/entity/grounding-entities-v7.csv \
  --output-dir /tmp/my-graph \
  --prefix my-graph-v1 \
  --chunk-size 4000 --overlap 500 \
  --prepare
```

* `--doc-class`: Optional ontology class name for each document (auto-detected if omitted).
* `--entities`: Optional CSV (`id,name,type,aliases`) or JSON (`topics` / `entities`) canonical seed registry. Entity resolution is strictly scoped by `(entity_type, normalized_label)` so entities of different classes never collide.
* `--max-docs N`: Optional cap on Markdown files for quick pilots.

### 3. Controlled LLM Extraction (`--extract`)

Calls Microsoft Foundry Models v1 GA (`/openai/v1/chat/completions`) with strict `json_schema` output constrained to your ontology, validates every triple's `(domain, range)`, deduplicates by `(chunk_id, fact_id)`, and quarantines invalid triples to `rejected_edges.jsonl`.

Start with a small pilot (`--max-chunks 20`) before running the full corpus:

```bash
PYTHONPATH=scripts .venv/bin/python scripts/build_markdown_graph.py \
  --input-dir documents \
  --ontology .foundry-iq/fiq-speeches-9c65c09f/ontology/speeches-grounded-v7.jsonld \
  --entities .foundry-iq/fiq-speeches-9c65c09f/entity/grounding-entities-v7.csv \
  --output-dir /tmp/my-graph \
  --prefix my-graph-v1 \
  --max-chunks 20 --workers 2 \
  --extract \
  --foundry-endpoint https://YOUR-FOUNDRY.cognitiveservices.azure.com \
  --chat-deployment chat
```

* **Resumable cache:** Every completed chunk is flushed to `extraction_cache.jsonl` keyed by `(chunk_uid, chunk_sha256, title, ontology_sha256, deployment)`. Re-running the same command skips cached chunks automatically, while editing a document invalidates only changed chunks.
* **Throughput:** `--workers` supports `1`–`8` (default `2`). Match concurrency to your Foundry TPM/RPM quota to minimize `429` backoffs.

### 4. Create Indexes & Publish to Azure AI Search (`--apply --verify-gql`)

Creates `{prefix}-chunks` and `{prefix}-graph` (`graphProfile` `edgeList` linked via `provenanceIndexName: "{prefix}-chunks"`), uploads batches with `HTTP 207`/transient retries, and optionally runs a smoke `search.graph` query with `provenance: { kind: "hydrate" }`:

```bash
PYTHONPATH=scripts .venv/bin/python scripts/build_markdown_graph.py \
  --input-dir documents \
  --ontology .foundry-iq/fiq-speeches-9c65c09f/ontology/speeches-grounded-v7.jsonld \
  --output-dir /tmp/my-graph \
  --prefix my-graph-v1 \
  --apply --verify-gql \
  --search-endpoint https://YOUR-SEARCH.search.windows.net
```

---

## Output Directory Artifacts

| File | Purpose |
|---|---|
| `manifest.json` | Corpus counts, ontology hash, and target index names |
| `documents.jsonl` | Parsed document metadata and SHA-256 hashes |
| `chunks.jsonl` | Overlapping text chunks (`uid`, `parent_id`, `snippet`) |
| `metadata_edges.jsonl` | Deterministic edges derived from document headers |
| `extraction_cache.jsonl` | Per-chunk resumable LLM extraction checkpoint |
| `edges.jsonl` | Combined validated metadata + extracted edges ready for upload |
| `rejected_edges.jsonl` | Quarantined triples failing domain/range or endpoint checks |

---

## Enabling Graph RAG (`GQL`) in the Web App

The API checks `api/graph.json` (and `GRAPH_*` environment overrides) when `SPEECH_DATASET=full` to enable the **Standard RAG / Graph RAG (GQL)** toggle in the UI:

```json
{
  "enabled": true,
  "search_endpoint": "https://YOUR-SEARCH.search.windows.net",
  "search_api_version": "2026-11-01-preview",
  "graph_index": "my-graph-v1-graph",
  "chunks_index": "my-graph-v1-chunks",
  "graph_name": "my-graph-v1",
  "foundry_endpoint": "https://YOUR-FOUNDRY.cognitiveservices.azure.com",
  "chat_deployment": "chat"
}
```

Run locally with:

```bash
source .venv/bin/activate
python scripts/start_speech_api.py
```

In a second terminal:

```bash
cd web && npm run dev
```

Open `http://127.0.0.1:5173`, select **Graph RAG (GQL)**, and inspect **Response metadata** for the executed GQL and the **Graph** tab for **Knowledge graph traversal**.

> **Deploying to Azure Functions:** If deploying Graph RAG to the hosted Function App, ensure `api/graph.json` (or `GRAPH_*` app settings) is included in the deployment package and grant the Function App's Managed Identity `Cognitive Services OpenAI User` on the Foundry resource.

---

## Tests

```bash
PYTHONPATH=scripts .venv/bin/python -m unittest scripts/test_build_markdown_graph.py -v
PYTHONPATH=api SPEECH_DATASET=sample .venv/bin/python -m unittest discover -s api/tests -v
cd web && npm test && npm run build
```
