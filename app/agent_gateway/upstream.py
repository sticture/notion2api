from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

import httpx

MODEL = "albuquerque-quinn"
PUBLIC_MODEL = "claude-opus-5-5"
SESSION_HEADERS = {"user-agent", "notion-client-version", "notion-audit-log-platform",
                   "x-notion-cell-hint", "x-notion-cell-hint-source"}


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502, rejected: bool = False):
        super().__init__(message)
        self.status = status
        self.rejected = rejected


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

    def update(self, page: dict):
        self.cursor = page.get("forward_cursor", self.cursor)
        self.session = page.get("session") or self.session
        self.sequence = max(self.sequence, page.get("forward_after_sequence", -1))
        for patch in page.get("patches", []):
            if patch.get("op") == "put":
                entity = patch.get("entity", {})
                self.sequence = max(self.sequence, entity.get("sequence", -1))
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
             and (not self.previous_turn_id or e.get("turn_id") != self.previous_turn_id)),
            key=lambda e: e.get("sequence", 0),
        )
        return "\n".join(block.get("text", "") for message in messages
                         for block in message.get("content", []) if block.get("type") == "text")

    def result(self):
        completed = [e for e in self.entities.values() if e.get("kind") == "turn_completed"
                     and e.get("sequence", 0) > self.after_sequence
                     and (not self.previous_turn_id or e.get("turn_id") != self.previous_turn_id)]
        if not completed:
            return None
        done = max(completed, key=lambda e: e.get("sequence", 0))
        self.turn_id = done.get("turn_id")
        if done.get("stop_reason") != "end_turn":
            raise UpstreamError("Notion turn did not finish normally: " + str(done.get("stop_reason")))
        messages = sorted(
            (e for e in self.entities.values() if e.get("kind") == "assistant_message"
             and e.get("turn_id") == done.get("turn_id")),
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
        # Credential destination is intentionally fixed, not user-overridable.
        if config.get("origin") != "https://app.notion.com":
            raise ValueError("Only https://app.notion.com is supported")
        self.space_id = config["space_id"]
        self.account_scope = hashlib.sha256((config["user_id"] + ":" + self.space_id).encode()).hexdigest()
        self.timeout = timeout or float(os.getenv("NOTION_GATEWAY_TIMEOUT", "900"))
        headers = {k: v for k, v in config.get("headers", {}).items() if k.lower() in SESSION_HEADERS}
        headers.update({"origin": config["origin"], "referer": config["origin"] + "/chat",
                        "x-notion-space-id": self.space_id,
                        "x-notion-active-user-header": config["user_id"]})
        self.client = httpx.AsyncClient(
            base_url=config["origin"], headers=headers,
            cookies={c["name"]: c["value"] for c in config["cookies"]},
            timeout=httpx.Timeout(40, connect=15), follow_redirects=False,
        )

    async def close(self):
        await self.client.aclose()

    async def post(self, endpoint: str, body: dict):
        try:
            response = await self.client.post("/api/v3/" + endpoint, json=body)
        except httpx.HTTPError as exc:
            raise UpstreamError("Notion transport failed (" + type(exc).__name__ + ")") from None
        if response.status_code != 200:
            status = response.status_code if response.status_code in {401, 403, 429} else 502
            # Never include arbitrary upstream response bodies, credentials or trace URLs.
            raise UpstreamError(f"Notion {endpoint} returned HTTP {response.status_code}", status,
                                rejected=400 <= response.status_code < 500 and response.status_code != 408)
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
        if continuation:
            # Model selection belongs to thread creation. Adding modelSelection here
            # was rejected by the actual Notion endpoint; preserve the thread's model.
            await self.post("sendEventToAgentThread", {
                "spaceId": self.space_id, "threadId": thread_id,
                "event": {"type": "user.message", "content": [{"type": "text", "text": [[prompt]]}]},
                "surface": "full_page_chat", "clientEventId": event_id,
            })
        else:
            await self.post("createAgentThread", {
                "type": "personal_agent", "spaceId": self.space_id, "threadId": thread_id,
                "enableSuggestedEditsTools": False, "model": MODEL, "reasoningEffort": effort,
                "createdSource": "full_page_chat", "content": [{"type": "text", "text": [[prompt]]}],
                "policies": {"approval_mode": "review_for_me"},
                "agentMemorySettings": {"useMemories": False, "excludeChatFromMemories": True},
                "browserEnabled": False, "clientMessageId": event_id,
            })

    async def wait_for_turn(self, thread_id, cursor=None, after_sequence=-1, previous_turn_id=None, on_text=None):
        async with asyncio.timeout(self.timeout):
            state = Transcript(after_sequence, previous_turn_id)
            state.cursor = cursor
            seen = ""
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
                result = state.result()
                if result is not None:
                    return result, state.cursor, state.sequence, state.turn_id
                if state.session.get("pending_input"):
                    raise UpstreamError("Notion requested an internal action; local tool bridging cannot approve it")
                await asyncio.sleep(0.75 if on_text else 1.5)

    async def complete(self, prompt: str, effort: str = "medium"):
        """Standalone diagnostic call; client endpoints use the persistent session store."""
        thread_id = str(uuid.uuid4())
        await self.start_turn(prompt, effort, thread_id, str(uuid.uuid4()))
        text, *_ = await self.wait_for_turn(thread_id)
        return text, thread_id
