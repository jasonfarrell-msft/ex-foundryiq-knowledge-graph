import asyncio
import json
import os
from urllib.parse import urlparse

import azure.functions as func
import httpx
from azure.core.exceptions import AzureError
from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

from service import (
    CONFIG, COVERAGE, MAX_EDGES, MAX_QUESTION, WARNINGS,
    SearchService, ServiceError, question_from,
)


app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
inflight = asyncio.Lock()


def local_development():
    return (
        os.environ.get("SPEECH_LOCAL_DEVELOPMENT") == "1"
        and not os.environ.get("WEBSITE_INSTANCE_ID")
        and not os.environ.get("WEBSITE_HOSTNAME")
    )

def azure_credential():
    return (
        AzureCliCredential(process_timeout=5) if local_development()
        else ManagedIdentityCredential(
            client_id=os.environ.get("AZURE_CLIENT_ID"),
            retry_total=0, connection_timeout=3, read_timeout=3,
        )
    )


def authorized(req):
    if local_development():
        return urlparse(req.url).hostname in ("localhost", "127.0.0.1", "::1")
    # EasyAuth validates the linked SWA, including its anonymous visitor requests.
    # The platform configuration must continue to reject direct backend access.
    return os.environ.get("WEBSITE_AUTH_ENABLED", "").lower() == "true"


def reply(body, status=200, retry_after=None):
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return func.HttpResponse(
        json.dumps(body, allow_nan=False), status_code=status,
        mimetype="application/json", headers=headers,
    )


def guard(req):
    if not authorized(req):
        return reply({"error": "The protected application gateway is unavailable."}, 403)
    origin = req.headers.get("origin")
    allowed = (
        {"http://localhost:5173", "http://127.0.0.1:5173",
         "http://localhost:4280", "http://127.0.0.1:4280"}
        if local_development() else {os.environ.get("APP_ALLOWED_ORIGIN", "")}
    )
    if origin and origin not in allowed:
        return reply({"error": "Request origin is not allowed."}, 403)
    return None


@app.route(route="health", methods=["GET"])
async def health(req: func.HttpRequest) -> func.HttpResponse:
    denied = guard(req)
    if denied is not None:
        return denied
    probe = req.params.get("probe")
    if probe is not None and probe != "search":
        return reply({"error": "Only the search connectivity probe is supported."}, 400)
    connectivity = "not_probed"
    if probe == "search":
        if inflight.locked():
            return reply({"error": "Another request is running. Please wait."}, 429, 5)
        try:
            async with asyncio.timeout(10), inflight:
                credential = azure_credential()
                async with credential, httpx.AsyncClient(
                    timeout=httpx.Timeout(8, connect=3),
                    follow_redirects=False,
                    transport=httpx.AsyncHTTPTransport(retries=0),
                ) as client:
                    await SearchService(credential, client).probe()
            connectivity = "search_verified"
        except ServiceError as error:
            return reply({"error": str(error)}, error.status, error.retry_after)
        except (AzureError, ImportError):
            return reply({"error": "Azure authentication is unavailable for the connectivity check."}, 503)
        except TimeoutError:
            return reply({"error": "Search connectivity check exceeded its deadline."}, 504)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return reply({"error": "Search connectivity check could not be completed."}, 502)
    return reply({
        "status": "ok", "configured": True, "coverage": COVERAGE,
        "limits": {"question_characters": MAX_QUESTION, "graph_edges": MAX_EDGES},
        "warnings": WARNINGS, "azure_connectivity": connectivity,
    })


@app.route(route="catalog", methods=["GET"])
async def catalog(req: func.HttpRequest) -> func.HttpResponse:
    denied = guard(req)
    if denied is not None:
        return denied
    return reply({"speeches": CONFIG["speeches"], "coverage": COVERAGE, "warnings": WARNINGS})


@app.route(route="ask", methods=["POST"])
async def ask(req: func.HttpRequest) -> func.HttpResponse:
    denied = guard(req)
    if denied is not None:
        return denied
    if req.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return reply({"error": "Content-Type must be application/json."}, 415)
    if len(req.get_body()) > 12000:
        return reply({"error": "Request body is too large."}, 413)
    try:
        question = question_from(req.get_json())
    except (ValueError, TypeError):
        return reply({"error": "Send only question, containing 1 to 2,000 characters."}, 400)
    if inflight.locked():
        return reply({"error": "Another question is running. Please wait."}, 429, 5)
    try:
        async with inflight, asyncio.timeout(38):
            credential = azure_credential()
            async with credential, httpx.AsyncClient(
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0),
            ) as client:
                result = await SearchService(credential, client).ask(question)
                return reply(result)
    except ServiceError as error:
        return reply({"error": str(error)}, error.status, error.retry_after)
    except TimeoutError:
        return reply({"error": "Retrieval exceeded the request deadline. No automatic retry was made; Azure work may continue."}, 504)
    except AzureError:
        return reply({"error": "Azure authentication is unavailable. Check local az login or the Function App identity."}, 503)
    except ImportError:
        return reply({"error": "Azure authentication dependencies are unavailable. Check the API deployment."}, 503)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return reply({"error": "Retrieval could not be completed. No automatic retry was made."}, 502)
