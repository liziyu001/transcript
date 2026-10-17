"""Native FC 3 Python handlers. HTTP requests are bridged to the shared ASGI app."""

import asyncio
import base64
import json
from urllib.parse import urlencode

from .api import create_app, make_service
from .models import Settings


def worker(event, context):
    body = json.loads(event) if isinstance(event, (str, bytes)) else event
    return make_service(Settings.from_env(), context, worker=True).step(body["job_id"])


async def invoke_asgi(app, event):
    request = event["requestContext"]["http"]
    headers = {k.lower(): str(v) for k, v in event.get("headers", {}).items()}
    body = event.get("body") or ""
    body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request["method"],
        "scheme": "https",
        "path": request["path"],
        "raw_path": event.get("rawPath", request["path"]).encode(),
        "query_string": event.get(
            "rawQueryString", urlencode(event.get("queryParameters") or {})
        ).encode(),
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "server": (headers.get("host", "localhost"), 443),
        "client": (request.get("sourceIp", "127.0.0.1"), 0),
    }
    sent = False
    messages = []
    wait = asyncio.Event()

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await wait.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return {
        "statusCode": start["status"],
        "headers": {k.decode(): v.decode() for k, v in start["headers"]},
        "isBase64Encoded": True,
        "body": base64.b64encode(content).decode(),
    }


def api(event, context):
    event = json.loads(event) if isinstance(event, (str, bytes)) else event
    settings = Settings.from_env()
    headers = {key.lower(): value for key, value in event.get("headers", {}).items()}
    origin = headers.get("origin")
    if origin and origin not in (settings.allowed_origin, "https://" + headers.get("host", "")):
        return {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": '{"detail":"Origin is not allowed"}',
            "isBase64Encoded": False,
        }
    app = create_app(make_service(settings, context), settings)
    response = asyncio.run(invoke_asgi(app, event))
    # FC adds CORS headers to both preflight and ordinary responses. Duplicating
    # Allow-Origin makes browsers reject them; origin validation happens above.
    response["headers"] = {
        key: value
        for key, value in response["headers"].items()
        if not key.lower().startswith("access-control-")
    }
    return response
