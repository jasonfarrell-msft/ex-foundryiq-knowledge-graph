"""Loopback ASGI adapter for the same HTTP handlers deployed by Azure Functions."""

import azure.functions as func

from function_app import ask, catalog, health


ROUTES = {("GET", "/api/health"): health, ("GET", "/api/catalog"): catalog,
          ("POST", "/api/ask"): ask}


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    headers = {key.decode("latin1"): value.decode("latin1")
               for key, value in scope["headers"]}
    payload = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        payload.extend(message.get("body", b""))
        if len(payload) > 12000:
            response = func.HttpResponse('{"error":"Request body is too large."}',
                                         status_code=413, mimetype="application/json")
            break
        if not message.get("more_body"):
            handler = ROUTES.get((scope["method"], scope["path"]))
            if handler:
                # The adapter is only served on 127.0.0.1 by the local launcher.
                request = func.HttpRequest(
                    scope["method"], "http://127.0.0.1:7071" + scope["path"],
                    headers=headers, body=bytes(payload),
                )
                response = await handler(request)
            else:
                response = func.HttpResponse('{"error":"Not found."}', status_code=404,
                                             mimetype="application/json")
            break
    response_headers = [(key.lower().encode(), value.encode())
                        for key, value in response.headers.items()]
    if not any(key == b"content-type" for key, _ in response_headers):
        response_headers.append((
            b"content-type",
            f"{response.mimetype}; charset={response.charset}".encode("latin1"),
        ))
    await send({"type": "http.response.start", "status": response.status_code,
                "headers": response_headers})
    await send({"type": "http.response.body", "body": response.get_body()})
