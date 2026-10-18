from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
import httpx
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .protocol import (ProtocolError, prepare_request, parse_answer, estimate_tokens, sse, content_events,
                       answer_options, finalize_answer, leading_text)
from .upstream import NotionAgent, UpstreamError, PUBLIC_MODEL
from .responses import PICKER_MODEL, convert_request, convert_answer, response_object, output_events, response_usage
from .routing import passthrough, HOP_HEADERS
from .sessions import SessionStore, client_session
from .config import ROOT, LOCAL, PROFILE, PROFILE_LOCAL, CATALOG

MAX_REQUEST_BYTES = int(os.getenv("NOTION_GATEWAY_MAX_REQUEST_BYTES", "16000000"))


def error_response(message, status=400):
    error_type = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}.get(status)
    return JSONResponse({"type": "error", "error": {"type": error_type or ("api_error" if status >= 500 else "invalid_request_error"),
                                                    "message": message}}, status_code=status)


def audit(record):
    path = LOCAL / "gateway-audit.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps({"time": time.time(), **record}) + "\n")


@asynccontextmanager
async def lifespan(app):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    key_path = LOCAL / "gateway-key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(32))
    app.state.key = key_path.read_text().strip()
    if len(app.state.key) < 24:
        raise RuntimeError("Gateway key must contain at least 24 characters")
    session_file = PROFILE_LOCAL / "notion-session.json"
    if not session_file.exists():
        raise RuntimeError("Notion profile is not configured. Run scripts/setup_gateway.py --help first.")
    app.state.upstream = NotionAgent(session_file)
    # An expired Notion session must not prevent existing provider routes from working.
    try:
        app.state.model = await app.state.upstream.verify_model()
    except (UpstreamError, httpx.HTTPError) as exc:
        app.state.model = {"id": PUBLIC_MODEL, "available": False, "error": str(exc)}
    app.state.routes = json.loads((LOCAL / "routes.json").read_text()) if (LOCAL / "routes.json").exists() else {}
    app.state.proxy_http = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), follow_redirects=False)
    app.state.lock = asyncio.Semaphore(1)
    app.state.sessions = SessionStore(PROFILE_LOCAL / "gateway-sessions.sqlite3",
                                     namespace=app.state.upstream.account_scope, adopt_legacy=PROFILE == "default")
    try:
        yield
    finally:
        await app.state.upstream.close()
        await app.state.proxy_http.aclose()
        app.state.sessions.close()


app = FastAPI(title="Notion local coding gateway (experimental)", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_auth(request, call_next):
    host = urlsplit("http://" + request.headers.get("host", "")).hostname
    if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return error_response("Loopback host required", 403)
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        return error_response("Loopback clients only", 403)
    if request.headers.get("origin"):
        return error_response("Browser-origin requests are not supported", 403)
    if request.url.path.startswith(("/v1/", "/codex/", "/claude/")):
        supplied = (request.headers.get("x-notion-gateway-key") if request.url.path.startswith("/codex/")
                    else request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")) or ""
        if not hmac.compare_digest(supplied, request.app.state.key):
            return error_response("Invalid local gateway key", 401)
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "ok", "model": app.state.model, "tool_mode": "text-json-bridge", "usage": "estimated",
            "session_mode": "persistent-client-thread", "profile": PROFILE}


@app.head("/api/hello")
async def hello():
    return Response(status_code=200)


@app.get("/v1/models")
@app.get("/claude/v1/models")
async def models():
    return {"data": [{"type": "model", "id": PICKER_MODEL, "display_name": "Notion Opus 5.5",
                      "created_at": "2026-10-18T00:00:00Z"}], "has_more": False,
            "first_id": PICKER_MODEL, "last_id": PICKER_MODEL}


@app.post("/v1/messages")
@app.post("/claude/v1/messages")
async def messages(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            return error_response("Request exceeds local gateway size limit", 413)
    try:
        body = json.loads(raw)
        if request.url.path.startswith("/claude/") and body.get("model") != PICKER_MODEL:
            return await passthrough(request, bytes(raw), "claude", "v1/messages")
        prompt, tools, choice, effort = prepare_request(body)
        options = answer_options(body)
    except ProtocolError as exc:
        return error_response(str(exc))
    except (ValueError, TypeError, KeyError, AttributeError):
        return error_response("Invalid or unsupported Messages request; text and custom tools only")
    message_id = "msg_" + uuid.uuid4().hex
    usage = {"input_tokens": estimate_tokens(prompt), "output_tokens": 0}
    message = {"id": message_id, "type": "message", "role": "assistant", "model": body["model"],
               "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
    session_key, session_source = client_session(request.headers, body, "messages")
    if options["stop_sequences"] or options["schema"] is not None:
        # Side requests (e.g. auto-mode safety classifier) reuse the main session header;
        # keep them out of the main Notion thread so its history is never resynced.
        session_key, session_source = uuid.uuid4().hex, "side-request"

    def render(text):
        content, stop = parse_answer(text, tools, choice)
        content, stop, stop_sequence = finalize_answer(content, stop, options)
        return {**message, "content": content, "stop_reason": stop, "stop_sequence": stop_sequence,
                "usage": {**usage, "output_tokens": estimate_tokens(content)}}

    async def execute(progress=None):
        started = time.monotonic()
        audit({"id": message_id, "event": "started", "protocol": "messages", "session_hash": session_key,
               "session_source": session_source})
        try:
            async with app.state.lock:
                result, info = await app.state.sessions.run(app.state.upstream, session_key, prompt, effort, render,
                                                            progress)
            result = {**result, "id": message_id}
            audit({"id": message_id, "event": "completed", "model": PUBLIC_MODEL,
                   **info, "session_source": session_source, "duration_s": round(time.monotonic() - started, 2),
                   "request_sha256": hashlib.sha256(raw).hexdigest(), "input_chars": len(prompt),
                   "tools": [c["name"] for c in result["content"] if c["type"] == "tool_use"],
                   "stop_reason": result["stop_reason"]})
            return result
        except BaseException as exc:
            audit({"id": message_id, "event": "failed", "error_class": type(exc).__name__})
            raise

    headers = {"x-notion-usage-estimated": "true", "x-notion-tool-mode": "text-json-bridge"}
    if not body.get("stream", False):
        try:
            return JSONResponse(await execute(), headers=headers)
        except (UpstreamError, ProtocolError, TimeoutError) as exc:
            return error_response(str(exc) or "Notion turn timed out", getattr(exc, "status", 502))

    async def stream():
        updates = asyncio.Queue()
        # Emulated stop sequences / schemas rewrite the text, so only stream plain answers live.
        live = not options["stop_sequences"] and options["schema"] is None
        task = asyncio.create_task(execute((lambda partial: updates.put_nowait(partial)) if live else None))
        streamed = ""
        try:
            yield sse("message_start", message=message)
            while not task.done():
                getter = asyncio.ensure_future(updates.get())
                finished, _ = await asyncio.wait({task, getter}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                if getter not in finished:
                    getter.cancel()
                    if not finished:
                        yield sse("ping")
                    continue
                partial = getter.result()
                while not updates.empty():
                    partial = updates.get_nowait()
                text = leading_text(partial)
                if len(text) > len(streamed) and text.startswith(streamed):
                    if not streamed:
                        yield sse("content_block_start", index=0, content_block={"type": "text", "text": ""})
                    yield sse("content_block_delta", index=0, delta={"type": "text_delta", "text": text[len(streamed):]})
                    streamed = text
            result = task.result()
            content, start = result["content"], 0
            if streamed:
                # Only the leading text is streamed early; tool input is still validated before exposure.
                first = content[0] if content else None
                if first and first["type"] == "text" and first["text"].startswith(streamed):
                    if first["text"][len(streamed):]:
                        yield sse("content_block_delta", index=0,
                                  delta={"type": "text_delta", "text": first["text"][len(streamed):]})
                    content = content[1:]
                yield sse("content_block_stop", index=0)
                start = 1
            # Buffer and validate the complete JSON before exposing executable tool input.
            for event in content_events(content, start):
                yield event
            yield sse("message_delta", delta={"stop_reason": result["stop_reason"],
                                              "stop_sequence": result.get("stop_sequence")},
                      usage=result["usage"])
            yield sse("message_stop")
        except (UpstreamError, ProtocolError, TimeoutError) as exc:
            yield sse("error", error={"type": "api_error", "message": str(exc) or "Notion turn timed out"})
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={**headers, "cache-control": "no-cache", "x-accel-buffering": "no"})


@app.head("/claude/api/hello")
async def claude_hello():
    return Response(status_code=200)


@app.post("/claude/v1/messages/count_tokens")
async def count_tokens(request: Request):
    raw = await request.body()
    body = json.loads(raw)
    if body.get("model") == PICKER_MODEL:
        return Response(status_code=404)
    return await passthrough(request, raw, "claude", "v1/messages/count_tokens")


@app.get("/codex/models")
async def codex_models(request: Request):
    snapshot_path = LOCAL / "codex-models.json"
    snapshot = json.loads((snapshot_path if snapshot_path.exists() else CATALOG).read_text())
    route = app.state.routes.get("codex")
    if route and request.headers.get("authorization"):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS | {"cookie", "x-api-key"}}
        try:
            res = await app.state.proxy_http.get(route["base_url"].rstrip("/") + "/models",
                                                 headers=headers, params=list(request.query_params.multi_items()), timeout=15)
            data = res.json() if res.status_code == 200 else {}
            if isinstance(data.get("models"), list) and data["models"]:
                custom = json.loads(CATALOG.read_text())["models"][0]
                return JSONResponse({"models": [m for m in data["models"] if m.get("slug") != PICKER_MODEL] + [custom]})
        except (httpx.HTTPError, ValueError):
            pass
    return JSONResponse(snapshot, headers={"x-notion-catalog-source": "local-snapshot"})


@app.post("/codex/responses/compact")
async def compact(request: Request):
    raw = await request.body()
    if json.loads(raw).get("model") == PICKER_MODEL:
        return error_response("Use local compaction for Notion", 400)
    return await passthrough(request, raw, "codex", "responses/compact")


@app.post("/v1/responses")
@app.post("/codex/responses")
async def responses(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            return error_response("Request exceeds local gateway size limit", 413)
    try:
        body = json.loads(raw)
        if request.url.path.startswith("/codex/") and body.get("model") != PICKER_MODEL:
            return await passthrough(request, bytes(raw), "codex", "responses")
        if body.get("model") not in {PICKER_MODEL, PUBLIC_MODEL}:
            raise ProtocolError("Unsupported Notion model")
        prompt, tools, choice, effort, mapping = convert_request(body)
    except ProtocolError as exc:
        return error_response(str(exc))
    except (ValueError, TypeError, KeyError, AttributeError):
        return error_response("Invalid or unsupported Responses request")
    response_id = "resp_" + uuid.uuid4().hex
    if os.getenv("NOTION_GATEWAY_DEBUG_REQUESTS") == "1":
        # Opt-in synthetic-client diagnostics only; not enabled by the installed command.
        fd = os.open(LOCAL / "debug-responses-request.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(body))
    audit({"id": response_id, "event": "started", "protocol": "responses", "input_chars": len(prompt),
           "input_bytes": len(prompt.encode()), "request_bytes": len(raw), "tool_names": list(mapping)})
    initial = response_object(response_id, body["model"])
    session_key, session_source = client_session(request.headers, body, "responses")

    def render(text):
        output = convert_answer(text, tools, choice, mapping)
        return response_object(response_id, body["model"], output, response_usage(prompt, output), "completed")

    async def execute():
        started = time.monotonic()
        try:
            async with app.state.lock:
                result, info = await app.state.sessions.run(app.state.upstream, session_key, prompt, effort, render)
            result = {**result, "id": response_id, "created_at": initial["created_at"]}
            audit({"id": response_id, "event": "completed", "protocol": "responses", "model": body["model"],
                   **info, "session_source": session_source, "duration_s": round(time.monotonic() - started, 2),
                   "input_chars": len(prompt), "tools": [x["name"] for x in result["output"] if x["type"] != "message"]})
            return result
        except BaseException as exc:
            audit({"id": response_id, "event": "failed", "error_class": type(exc).__name__, "protocol": "responses"})
            raise

    if not body.get("stream", False):
        try:
            return JSONResponse(await execute(), headers={"x-notion-usage-estimated": "true"})
        except (UpstreamError, ProtocolError, TimeoutError) as exc:
            return error_response(str(exc) or "Notion timed out", getattr(exc, "status", 502))

    async def stream():
        sequence = 0

        def emit(event):
            nonlocal sequence
            event = {**event, "sequence_number": sequence}
            sequence += 1
            return "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"

        task = asyncio.create_task(execute())
        try:
            yield emit({"type": "response.created", "response": initial})
            yield emit({"type": "response.in_progress", "response": initial})
            while not task.done():
                finished, _ = await asyncio.wait({task}, timeout=5)
                if not finished:
                    yield ": ping\n\n"
            result = task.result()
            for event in output_events(result["output"]):
                yield emit(event)
            yield emit({"type": "response.completed", "response": result})
        except (UpstreamError, ProtocolError, TimeoutError) as exc:
            yield emit({"type": "response.failed", "response": {**initial, "status": "failed",
                        "error": {"code": "server_error", "message": str(exc) or "Notion timed out"}}})
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"cache-control": "no-cache",
                             "x-accel-buffering": "no", "x-notion-usage-estimated": "true"})
