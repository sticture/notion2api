"""Durable client-session bindings and incremental Notion turns.

Only hashes of client history are retained. The last validated response is cached
so retries preserve tool IDs. Never re-submit an inference with an unknown outcome.
Call run() under the server's inference lock, including response validation/commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

from .upstream import UpstreamError

MARKER = "CLIENT_CONTEXT_JSON:\n"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def client_session(headers, body, protocol):
    headers = {k.lower(): v for k, v in headers.items()}
    identity = headers.get("x-notion-session-id")
    source = "explicit"
    agent = request_class = ""
    if not identity and protocol == "messages":
        identity = headers.get("x-claude-code-session-id")
        source = "claude-header"
        agent = headers.get("x-claude-code-agent-id", "")
        request_class = headers.get("x-claude-code-request-class", "")
        if not identity:
            metadata = body.get("metadata")
            uid = metadata.get("user_id", "") if isinstance(metadata, dict) else ""
            try:
                meta = json.loads(uid)
                identity = meta.get("session_id") if isinstance(meta, dict) else None
            except (ValueError, TypeError):
                match = re.search(r"_session_([0-9a-fA-F-]{36})$", uid) if isinstance(uid, str) else None
                identity = match.group(1) if match else None
            source = "claude-metadata"
    if not identity and protocol == "responses":
        meta = body.get("client_metadata")
        meta = meta if isinstance(meta, dict) else {}
        identity = (meta.get("thread_id") or headers.get("x-codex-thread-id")
                    or meta.get("session_id") or headers.get("session_id")
                    or body.get("prompt_cache_key"))
        source = "codex-thread"
    if not isinstance(identity, str) or not identity:
        # Never guess identity from identical prompts; unrelated clients must not mix.
        identity, source = str(uuid.uuid4()), "unidentified"
    return digest([protocol, identity, agent, request_class]), source


def context_parts(prompt):
    preamble, separator, raw = prompt.partition(MARKER)
    if not separator:
        raise ValueError("Missing bridge context")
    context = json.loads(raw)
    atoms, directives = [], []
    for message in context.pop("messages"):
        if message["role"] == "system":
            directives.append(message)
        else:
            atoms.extend({"role": message["role"], "content": block} for block in message["content"])
    context["message_system"] = directives
    return preamble, context, atoms


def incremental_prompt(prompt, record):
    preamble, settings, atoms = context_parts(prompt)
    hashes = [digest(atom) for atom in atoms]
    setting_hashes = {k: digest(v) for k, v in settings.items()}
    snapshot = {"history": hashes, "settings": setting_hashes}
    fingerprint = digest([settings, atoms])
    previous = record.get("snapshot", {})
    history = previous.get("history", [])
    append = bool(previous) and hashes[:len(history)] == history
    start = len(history) if append else 0
    messages = []
    for atom in atoms[start:]:
        if messages and messages[-1]["role"] == atom["role"]:
            messages[-1]["content"].append(atom["content"])
        else:
            messages.append({"role": atom["role"], "content": [atom["content"]]})
    mode = "initial" if not record else "append" if append else "resync"
    changes = {k: v for k, v in settings.items()
               if not append or previous.get("settings", {}).get(k) != setting_hashes[k]}
    payload = {"mode": mode, **changes, "messages": messages}
    instructions = (
        "This is a persistent local-client conversation. In append mode, add only the supplied messages "
        "to the existing client history and keep unchanged context fields. Assistant messages acknowledge "
        "previously delivered outputs and supply bridge-assigned tool IDs; do not execute them again. "
        "In initial or resync mode, the supplied client history is authoritative and replaces any old "
        "client history, including after edits or compaction. message_system contains current client "
        "system messages. Respond to the latest client request or tool result, using exactly the JSON "
        "content envelope described above.\n"
    )
    rendered = preamble + instructions + MARKER + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return rendered, snapshot, fingerprint, mode


class SessionStore:
    def __init__(self, path: Path, namespace="", adopt_legacy=False):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.namespace = namespace
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        if namespace and adopt_legacy:
            # One-time migration of the original single-account installation.
            # Future account/workspace switches use a different namespace.
            self.db.execute("UPDATE sessions SET key = ? || ':' || key WHERE instr(key, ':') = 0", (namespace,))
        self.db.commit()

    def storage_key(self, key):
        return f"{self.namespace}:{key}" if self.namespace else key

    def close(self):
        self.db.close()

    def get(self, key):
        row = self.db.execute("SELECT data FROM sessions WHERE key = ?", (self.storage_key(key),)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, key, record):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (self.storage_key(key), json.dumps(record)))

    async def run(self, upstream, key, prompt, effort, render):
        record = self.get(key)
        sent, snapshot, fingerprint, mode = incremental_prompt(prompt, record)
        fingerprint = digest([fingerprint, effort])
        if record.get("fingerprint") == fingerprint and record.get("result"):
            return record["result"], {**record["audit"], "cached": True}
        pending = record.get("pending")
        superseded = None
        if pending and pending["fingerprint"] != fingerprint:
            # A client may change context after a disconnect, timeout or failed
            # validation. It cannot reproduce the exact old fingerprint reliably.
            # Never append to that uncertain thread or deliver its stale tool calls.
            # Rebuild from the authoritative client history on a fresh thread.
            superseded = record
            record = {}
            sent, snapshot, _, mode = incremental_prompt(prompt, record)
            pending = None
        if not pending:
            continuation = bool(record)
            record = record or {"thread_id": str(uuid.uuid4()), "effort": effort}
            pending = {"fingerprint": fingerprint, "snapshot": snapshot, "mode": mode,
                       "input_chars": len(sent), "event_id": str(uuid.uuid4())}
            record["pending"] = pending
            # Commit BEFORE dispatch, so a disconnect/restart cannot create a second thread or turn.
            self.save(key, record)
            try:
                await upstream.start_turn(sent, effort, record["thread_id"], pending["event_id"], continuation)
            except UpstreamError as exc:
                if exc.rejected:
                    record.pop("pending")
                    if superseded is not None:
                        self.save(key, superseded)
                    elif continuation:
                        self.save(key, record)
                    else:
                        with self.db:
                            self.db.execute("DELETE FROM sessions WHERE key = ?", (self.storage_key(key),))
                raise
        if "answer" not in pending:
            text, cursor, sequence, turn_id = await upstream.wait_for_turn(
                record["thread_id"], record.get("cursor"), record.get("sequence", -1), record.get("turn_id"))
            pending.update(answer=text, cursor=cursor, sequence=sequence, turn_id=turn_id)
            self.save(key, record)
        # Keep raw answers private until schema validation succeeds. Retry the same
        # validation rather than generating a duplicate action if it failed previously.
        result = render(pending["answer"])
        info = {"thread_id": record["thread_id"], "session_hash": key, "history_mode": pending["mode"],
                "upstream_input_chars": pending["input_chars"], "cached": False,
                "thread_effort": record["effort"]}
        record.update(snapshot=pending["snapshot"], fingerprint=fingerprint, result=result, audit=info,
                      cursor=pending["cursor"], sequence=pending["sequence"], turn_id=pending["turn_id"])
        record.pop("pending")
        self.save(key, record)
        return result, info
