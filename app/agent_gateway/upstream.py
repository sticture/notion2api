from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .media import FilePrompt, IMAGE_EXTENSIONS
from .network import NotionTransport

MODEL = "albuquerque-quinn"
PUBLIC_MODEL = "claude-opus-5-5"
PRIMARY_ORIGIN = "https://app.notion.com"
COMPAT_ORIGIN = "https://www.notion.so"
SESSION_HEADERS = {"user-agent", "notion-client-version", "notion-audit-log-platform",
                   "x-notion-cell-hint", "x-notion-cell-hint-source"}


# Body/stop-reason phrases meaning the workspace is out of AI quota (inspected, never relayed).
QUOTA_MARKERS = ("rate limit", "ratelimit", "rate_limit", "quota", "credit", "usage limit", "exhausted")


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502, rejected: bool = False, exhausted: bool = False,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.rejected = rejected
        self.diagnostics = {}
        # Workspace quota/rate limit: the gateway may continue the session in another workspace.
        self.exhausted = exhausted
        self.retry_after = retry_after


def quota_signal(response):
    if response.status_code == 429:
        return True
    if not 400 <= response.status_code < 500 or response.status_code in {401, 403, 404, 408}:
        return False
    return any(marker in response.text[:4000].lower() for marker in QUOTA_MARKERS)


def retry_after_seconds(response):
    try:
        value = float(response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return value if 0 < value <= 86400 else None


def edge_blocked(response):
    """An HTML edge rejection is not a Notion account/permission decision."""
    return (response.status_code == 403
            and "html" in response.headers.get("content-type", "").lower()
            and (response.headers.get("server", "").lower() == "cloudflare"
                 or "cloudflare" in response.text[:8000].lower()))


def edge_error(endpoint, retry_after=30):
    error = UpstreamError(
        "Notion's network protection temporarily blocked the upstream connection (HTTP 403). "
        "Retry shortly; signing in to Claude again will not resolve this network error.",
        503, rejected=endpoint in {"createAgentThread", "sendEventToAgentThread"},
        retry_after=retry_after)
    error.diagnostics = {"error_code": "upstream_edge_blocked", "upstream_endpoint": endpoint,
                         "upstream_status": 403, "stage": "edge"}
    return error


class Transcript:
    """Apply snapshots by entity ID; never mistake a partial answer for completion."""

    def __init__(self, after_sequence=-1, previous_turn_id=None):
        self.entities = {}
        self.cursor = None
        self.session = {}
        self.after_sequence = after_sequence
        self.sequence = after_sequence
        self.previous_turn_id = previous_turn_id
        self.turn_id = None
        self.user_sequence = after_sequence
        # Any Notion-side entity after our message (thinking, output, completion) proves the
        # workspace is serving this turn. Our own message and quota banners do not count.
        self.active = False

    def update(self, page: dict):
        self.cursor = page.get("forward_cursor", self.cursor)
        self.session = page.get("session") or self.session
        self.sequence = max(self.sequence, page.get("forward_after_sequence", -1))
        for patch in page.get("patches", []):
            if patch.get("op") == "put":
                entity = patch.get("entity", {})
                self.sequence = max(self.sequence, entity.get("sequence", -1))
                if entity.get("kind") == "user_message":
                    self.user_sequence = max(self.user_sequence, entity.get("sequence", -1))
                if entity.get("sequence", -1) > self.after_sequence and entity.get("kind") != "user_message" \
                        and not str(entity.get("id", "")).startswith("rate-limit-status"):
                    self.active = True
                # Thinking and Notion's internal execution details are not relayed.
                if entity.get("kind") in {"assistant_message", "turn_completed", "error"}:
                    self.entities[entity["id"]] = entity
            elif patch.get("op") == "remove":
                self.entities.pop(patch.get("id"), None)
            elif patch.get("op") == "session":
                self.session = patch.get("session", self.session)

    def partial_text(self):
        """Text generated so far in the current turn (may be incomplete JSON)."""
        messages = sorted(
            (e for e in self.entities.values() if e.get("kind") == "assistant_message"
             and e.get("sequence", 0) > self.after_sequence
             and e.get("sequence", 0) > self.user_sequence
             and (self.user_sequence > self.after_sequence or not self.previous_turn_id
                  or e.get("turn_id") != self.previous_turn_id)),
            key=lambda e: e.get("sequence", 0),
        )
        return "\n".join(block.get("text", "") for message in messages
                         for block in message.get("content", []) if block.get("type") == "text")

    def result(self):
        completed = [e for e in self.entities.values() if e.get("kind") == "turn_completed"
                     and e.get("sequence", 0) > self.after_sequence
                     and e.get("sequence", 0) > self.user_sequence
                     and (self.user_sequence > self.after_sequence or not self.previous_turn_id
                          or e.get("turn_id") != self.previous_turn_id)]
        if not completed:
            return None
        done = max(completed, key=lambda e: e.get("sequence", 0))
        self.turn_id = done.get("turn_id")
        if done.get("stop_reason") != "end_turn":
            reason = str(done.get("stop_reason"))
            raise UpstreamError("Notion turn did not finish normally: " + reason,
                                exhausted=any(marker in reason.lower() for marker in QUOTA_MARKERS))
        messages = sorted(
            (e for e in self.entities.values() if e.get("kind") == "assistant_message"
             and e.get("turn_id") == done.get("turn_id")
             and self.user_sequence < e.get("sequence", 0) <= done.get("sequence", 0)),
            key=lambda e: e.get("sequence", 0),
        )
        text = "\n".join(
            block.get("text", "") for message in messages
            for block in message.get("content", []) if block.get("type") == "text"
        ).strip()
        if not text:
            raise UpstreamError("Notion completed without an assistant text response")
        return text


class NotionAgent:
    def __init__(self, session_file: Path, timeout: float | None = None):
        config = json.loads(session_file.read_text())
        # Destinations are fixed official Notion origins, never request-controlled.
        if config.get("origin") != PRIMARY_ORIGIN:
            raise ValueError("Only https://app.notion.com is supported")
        self.api_origin = PRIMARY_ORIGIN
        self.space_id = config["space_id"]
        self.account_scope = hashlib.sha256((config["user_id"] + ":" + self.space_id).encode()).hexdigest()
        self.timeout = timeout or float(os.getenv("NOTION_GATEWAY_TIMEOUT", "300"))
        # Fail a turn whose transcript stops advancing instead of waiting out the full timeout.
        self.idle_timeout = float(os.getenv("NOTION_GATEWAY_IDLE_TIMEOUT", "90"))
        self.image_files = OrderedDict()
        headers = {k: v for k, v in config.get("headers", {}).items() if k.lower() in SESSION_HEADERS}
        headers.update({"origin": config["origin"], "referer": config["origin"] + "/chat",
                        "x-notion-space-id": self.space_id,
                        "x-notion-active-user-header": config["user_id"]})
        self.client = httpx.AsyncClient(
            base_url=config["origin"], headers=headers,
            cookies={c["name"]: c["value"] for c in config["cookies"]},
            timeout=httpx.Timeout(40, connect=15), follow_redirects=False,
            transport=NotionTransport(),
        )

    async def close(self):
        await self.client.aclose()

    async def post(self, endpoint: str, body: dict):
        # Read-only requests are safe to retry. Writes require session-level
        # reconciliation; never replay a generation POST because its response broke.
        remaining = getattr(self, "edge_blocked_until", 0) - time.monotonic()
        if remaining > 0:
            raise edge_error(endpoint, remaining)
        attempts = 3 if endpoint in {"getThreadTranscript", "getAvailableModels", "getCreditRateLimitStatus"} else 1
        origin = getattr(self, "api_origin", PRIMARY_ORIGIN)
        for attempt in range(attempts):
            try:
                headers = {"origin": origin, "referer": origin + "/chat"}
                response = await self.client.post(origin + "/api/v3/" + endpoint, json=body, headers=headers)
                if edge_blocked(response) and origin == PRIMARY_ORIGIN:
                    # The edge explicitly rejected this request before Notion
                    # accepted it. Only then may we retry the identical event on
                    # Notion's other fixed official API origin; no redirects.
                    origin = COMPAT_ORIGIN
                    response = await self.client.post(origin + "/api/v3/" + endpoint, json=body,
                        headers={"origin": origin, "referer": origin + "/chat"})
                if response.status_code == 200:
                    self.api_origin = origin
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                error = UpstreamError("Notion transport failed (" + type(exc).__name__ + ")")
                error.diagnostics = {"upstream_endpoint": endpoint, "transport_error": type(exc).__name__,
                                     "read_attempts": attempt + 1}
                raise error from None
            if edge_blocked(response):
                # Do not hammer an edge block or label valid gateway credentials
                # as rejected. A definitive edge rejection did not start a turn.
                self.edge_blocked_until = time.monotonic() + 30
                raise edge_error(endpoint)
            if response.status_code in {408, 500, 502, 503, 504} and attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (2 ** attempt))
                continue
            break
        if response.status_code != 200:
            status = response.status_code if response.status_code in {401, 403, 429} else 502
            # Never include arbitrary upstream response bodies, credentials or trace URLs.
            error = UpstreamError(f"Notion {endpoint} returned HTTP {response.status_code}", status,
                                  rejected=400 <= response.status_code < 500 and response.status_code != 408,
                                  exhausted=quota_signal(response), retry_after=retry_after_seconds(response))
            try:
                detail = response.json()
            except ValueError:
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            debug = detail.get("debugMessage", "")
            # A larger UTF-8 prompt may pass character limits but overflow Notion's
            # internal duplicated DynamoDB write. Do not expose arbitrary error text.
            if response.status_code == 400 and isinstance(debug, str) and re.fullmatch(
                    r"create_session DynamoDB transaction action is \d+ bytes; max is \d+", debug):
                error.status = 413
                error.args = ("Notion rejected an oversized UTF-8 context; compact the conversation or split the input",)
            error.diagnostics = {"upstream_endpoint": endpoint, "upstream_status": response.status_code,
                                 "request_bytes": len(response.request.content),
                                 "reasoning_effort": body.get("reasoningEffort")}
            for field in ("name", "errorId"):
                value = detail.get(field)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
                    error.diagnostics["upstream_" + field] = value
            raise error
        try:
            return response.json()
        except ValueError:
            raise UpstreamError("Notion returned a non-JSON response") from None

    async def verify_model(self):
        directory = await self.post("getAvailableModels", {"spaceId": self.space_id})
        model = next((m for m in directory.get("models", []) if m.get("model") == MODEL), None)
        if not model or model.get("isDisabled") or model.get("restrictedForPersonalAgent"):
            raise UpstreamError("Opus 5.5 is not enabled for this Notion workspace", 403)
        if model.get("modelMessage") != "Opus 5.5":
            raise UpstreamError("Notion model mapping changed; refusing an unverified model")
        return {"id": PUBLIC_MODEL, "upstream_id": MODEL, "name": model["modelMessage"]}

    async def start_turn(self, prompt, effort, thread_id, event_id, continuation=False):
        content = [{"type": "text", "text": [[str(prompt)]]}]
        content.extend({"type": "file", "file_id": file_id} for file_id in getattr(prompt, "file_ids", []))
        if continuation:
            # Model selection belongs to thread creation. Adding modelSelection here
            # was rejected by the actual Notion endpoint; preserve the thread's model.
            await self.post("sendEventToAgentThread", {
                "spaceId": self.space_id, "threadId": thread_id,
                "event": {"type": "user.message", "content": content},
                "surface": "full_page_chat", "clientEventId": event_id,
            })
        else:
            await self.post("createAgentThread", {
                "type": "personal_agent", "spaceId": self.space_id, "threadId": thread_id,
                "enableSuggestedEditsTools": False, "model": MODEL, "reasoningEffort": effort,
                "createdSource": "full_page_chat", "content": content,
                "policies": {"approval_mode": "review_for_me"},
                "agentMemorySettings": {"useMemories": False, "excludeChatFromMemories": True},
                "browserEnabled": False, "clientMessageId": event_id,
            })

    async def prepare_images(self, prompt, thread_id, continuation):
        """Upload before committing a pending generation; an upload cannot strand a turn."""
        target = {"type": "thread", "threadId": thread_id} if continuation else {"type": "user"}
        attached = []
        for image_id, image in prompt.images.items():
            key = (thread_id, image_id)
            file_id = self.image_files.get(key)
            if file_id is None:
                file_id = await self.upload_image(image_id, image, target)
                self.image_files[key] = file_id
                while len(self.image_files) > 128:
                    self.image_files.popitem(last=False)
            self.image_files.move_to_end(key)
            attached.append({"image_id": image_id, "file_id": file_id})
        # One text block must precede the file blocks. The mapping identifies images
        # inside tool results or multiple historical messages without exposing bytes.
        note = ("Attached files are the actual images referenced by image_id in the client messages. "
                "Inspect the images directly and associate each with its original message/tool result. "
                "Attachment map: " + json.dumps(attached, separators=(",", ":")) + "\n")
        return FilePrompt(note + prompt, [item["file_id"] for item in attached])

    async def upload_image(self, image_id, image, target):
        descriptor = await self.post("createAgentServiceFileUploadURL", {
            "spaceId": self.space_id, "target": target,
            "filename": "image-" + image_id[:16] + "." + IMAGE_EXTENSIONS[image.media_type],
            "mediaType": image.media_type, "sizeBytes": len(image.data),
        })
        try:
            file_id, upload = descriptor["file"]["id"], descriptor["upload"]
            if not isinstance(file_id, str) or not file_id:
                raise ValueError("file id")
            parts = None
            # Storage must never receive Notion cookies or incoming client credentials.
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=False,
                                         transport=NotionTransport()) as storage:
                if upload["type"] == "single_part":
                    await self.upload_part(storage, upload, image.data)
                elif upload["type"] == "multi_part":
                    part_size = upload["part_size_bytes"]
                    if type(part_size) is not int or part_size <= 0:
                        raise ValueError("part size")
                    expected = (len(image.data) + part_size - 1) // part_size
                    if len(upload["parts"]) != expected:
                        raise ValueError("part count")
                    parts = []
                    for number, part in enumerate(upload["parts"], 1):
                        if part["part_number"] != number:
                            raise ValueError("part number")
                        response = await self.upload_part(storage, part,
                            image.data[(number - 1) * part_size:number * part_size])
                        etag = response.headers.get("etag")
                        if not etag or not etag.strip():
                            raise ValueError("missing ETag")
                        parts.append({"partNumber": number, "etag": etag})
                else:
                    raise ValueError("upload type")
        except (KeyError, TypeError, ValueError):
            raise UpstreamError("Notion returned an unsupported image upload descriptor", rejected=True) from None
        result = await self.post("completeAgentServiceFileUpload", {
            "spaceId": self.space_id, "target": target, "fileId": file_id,
            **({"parts": parts} if parts is not None else {}),
        })
        if not isinstance(result, dict) or result.get("id") != file_id:
            raise UpstreamError("Notion did not confirm the uploaded image", rejected=True)
        return file_id

    @staticmethod
    async def upload_part(storage, part, data):
        url = urlsplit(part["url"])
        if (url.scheme != "https" or url.username or url.password or url.port not in {None, 443}
                or not re.fullmatch(r"(?:[a-z0-9-]+\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", url.hostname or "")
                or part["method"] != "PUT"):
            raise ValueError("upload destination")
        headers = part.get("headers", {})
        if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                or k.lower() in {"cookie", "authorization", "proxy-authorization", "host"}
                for k, v in headers.items()):
            raise ValueError("upload headers")
        try:
            response = await storage.put(part["url"], headers=headers, content=data)
        except httpx.HTTPError as exc:
            raise UpstreamError("Image storage transport failed (" + type(exc).__name__ + ")",
                                rejected=True) from None
        if not 200 <= response.status_code < 300:
            raise UpstreamError(f"Image storage upload returned HTTP {response.status_code}", rejected=True)
        return response

    async def turn_is_absent(self, thread_id, cursor, after_sequence, previous_turn_id, submitted_at=None):
        """Reconcile an unacknowledged continuation against a stable, idle transcript.

        A timeout alone is never evidence that a write failed. Require the remote
        session AND transcript to remain at the last completed checkpoint, with
        no queued input. Missing fields, new events and read errors fail closed.
        Initial thread creation has no checkpoint and cannot use this recovery.
        """
        if not cursor or after_sequence < 0 or not previous_turn_id:
            return False
        if submitted_at is not None:
            await asyncio.sleep(max(0, min(5, 5 - (time.time() - submitted_at))))
        body = {"spaceId": self.space_id, "threadId": thread_id, "direction": "forward",
                "limit": 100, "cursor": cursor}
        for attempt in range(3):
            page = await self.post("getThreadTranscript", body)
            session = page.get("session") or {}
            if (session.get("status") != "idle" or session.get("sequence") != after_sequence
                    or page.get("session_pending") is not False
                    or page.get("forward_after_sequence") != after_sequence
                    or page.get("has_more_forward") is not False
                    or page.get("patches") != []):
                return False
            if attempt < 2:
                await asyncio.sleep(1)
        return True

    async def wait_for_turn(self, thread_id, cursor=None, after_sequence=-1, previous_turn_id=None, on_text=None):
        try:
            return await self._wait_for_turn(thread_id, cursor, after_sequence, previous_turn_id, on_text)
        except TimeoutError:
            error = UpstreamError(f"Notion turn did not complete within {self.timeout:g}s; "
                                  "the original turn is preserved for retry", 504)
            error.diagnostics = {"error_code": "upstream_timeout", "stage": "poll", "timeout_s": self.timeout}
            raise error from None

    async def _wait_for_turn(self, thread_id, cursor=None, after_sequence=-1, previous_turn_id=None, on_text=None):
        async with asyncio.timeout(self.timeout):
            loop = asyncio.get_running_loop()
            progressed = loop.time()
            state = Transcript(after_sequence, previous_turn_id)
            state.cursor = cursor
            seen = ""
            marker = None
            while True:
                body = {"spaceId": self.space_id, "threadId": thread_id, "direction": "forward", "limit": 100}
                if state.cursor:
                    body["cursor"] = state.cursor
                page = await self.post("getThreadTranscript", body)
                state.update(page)
                # Drain all pages before resolving turn completion.
                if page.get("has_more_forward"):
                    continue
                if on_text:
                    partial = state.partial_text()
                    if partial != seen:
                        seen = partial
                        on_text(partial)
                try:
                    result = state.result()
                except UpstreamError as exc:
                    if state.turn_id:
                        # A failed but completed turn is safe to advance past on
                        # a changed request. An unfinished turn is not.
                        exc.completed_turn = {"cursor": state.cursor, "sequence": state.sequence,
                                              "turn_id": state.turn_id}
                    raise
                if result is not None:
                    return result, state.cursor, state.sequence, state.turn_id
                if state.session.get("pending_input"):
                    raise UpstreamError("Notion requested an internal action; local tool bridging cannot approve it")
                current = (state.sequence, state.active, len(state.partial_text()))
                if current != marker:
                    marker, progressed = current, loop.time()
                elif state.active and self.idle_timeout > 0 and loop.time() - progressed > self.idle_timeout:
                    error = UpstreamError(f"Notion turn made no progress for {self.idle_timeout:g}s; "
                                          "the original turn is preserved for retry", 504)
                    error.diagnostics = {"error_code": "upstream_stalled", "stage": "poll",
                                         "sequence": state.sequence, "session_status": state.session.get("status")}
                    raise error
                await asyncio.sleep(0.75 if on_text else 1.5)

    async def complete(self, prompt: str, effort: str = "medium"):
        """Standalone diagnostic call; client endpoints use the persistent session store."""
        thread_id = str(uuid.uuid4())
        await self.start_turn(prompt, effort, thread_id, str(uuid.uuid4()))
        text, *_ = await self.wait_for_turn(thread_id)
        return text, thread_id
