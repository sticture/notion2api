"""Durable client-session bindings and incremental Notion turns.

Only hashes of client history are retained. The last validated response is cached
so retries preserve tool IDs. Never re-submit an inference with an unknown outcome.
Call run() under the server's per-session lock, including response validation/commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .upstream import UpstreamError
from .protocol import AnswerError
from .media import ImagePrompt, MAX_TURN_IMAGES, images_in_messages

MARKER = "CLIENT_CONTEXT_JSON:\n"
# UTF-8 byte bound on actual serialized content, including JSON escaping. Notion
# persists multiple copies; a ~200 KB request can exceed its DynamoDB item limit.
MAX_SEND_CHARS = int(os.getenv("NOTION_GATEWAY_MAX_SEND_CHARS", "195000"))
MAX_TOOL_RESULT_CHARS = int(os.getenv("NOTION_GATEWAY_MAX_TOOL_RESULT_CHARS", "16000"))
# Notion re-reads the whole thread every turn, so cost grows with thread length.
# Optional (0 = off; Notion manages its own thread context): past this many accumulated
# chars, continue on a fresh thread seeded with recent history.
THREAD_MAX_CHARS = int(os.getenv("NOTION_GATEWAY_THREAD_MAX_CHARS", "0"))
ROLLOVER_SEED_CHARS = int(os.getenv("NOTION_GATEWAY_ROLLOVER_SEED_CHARS", "80000"))
# Optional tighter bound for stateless side requests (0 = same as MAX_SEND_CHARS).
SIDE_MAX_SEND_CHARS = int(os.getenv("NOTION_GATEWAY_SIDE_MAX_SEND_CHARS", "0"))
# Recent history replayed onto the new workspace's thread on failover (keeps usage bounded).
FAILOVER_SEED_CHARS = int(os.getenv("NOTION_GATEWAY_FAILOVER_SEED_CHARS", "195000"))
# Hard cap on the first turn of every fresh Notion thread (initial, resync, side request, failover).
# Notion bills the whole input of that turn, so big replays drain usage fast.
NEW_THREAD_MAX_CHARS = int(os.getenv("NOTION_GATEWAY_NEW_THREAD_MAX_CHARS", "195000"))
MAX_ANSWER_REPAIRS = 2


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


def is_away_summary(body):
    """Claude Code's background recap is not part of its next main history."""
    messages = [m for m in body.get("messages", []) if m.get("role") != "system"]
    if not messages or messages[-1].get("role") != "user":
        return False
    content = messages[-1].get("content", "")
    if isinstance(content, list):
        if not all(b.get("type") == "text" for b in content):
            return False
        content = "\n".join(b.get("text", "") for b in content)
    return isinstance(content, str) and content.startswith(
        "The user stepped away and is coming back. Recap in under 40 words, "
        "1-2 plain sentences, no markdown.")


def stable_directive(block):
    if block.get("type") != "text":
        return block
    text = block["text"]
    text = re.sub(r"^x-anthropic-billing-header:[^\n]*(?:\n|$)", "", text)
    text = re.sub(r"<total_tokens>\s*\d+ tokens left\s*</total_tokens>", "", text).strip()
    return {**block, "text": text} if text else None


def context_parts(prompt):
    preamble, separator, raw = prompt.partition(MARKER)
    if not separator:
        raise ValueError("Missing bridge context")
    context = json.loads(raw)
    context["system"] = [v for b in context["system"] if (v := stable_directive(b)) is not None]
    atoms, directives = [], []
    for message in context.pop("messages"):
        if message["role"] == "system":
            blocks = [v for b in message["content"] if (v := stable_directive(b)) is not None]
            if blocks:
                directives.append({"role": "system", "content": blocks})
        else:
            has_images = any(block["type"] == "image" for block in message["content"])
            atoms.extend({"role": message["role"], "content": block,
                          **({"image_message_start": i == 0} if has_images else {})}
                         for i, block in enumerate(message["content"]))
    context["message_system"] = directives
    return preamble, context, atoms


def legacy_image_atom(atom, images):
    """Reconstruct the exact pre-image-support representation for hash matching.

    This is only a compatibility comparison. Actual image bytes are still sent
    separately as historical context when a legacy placeholder matches.
    """
    def convert(block):
        if block.get("type") == "image" and block.get("image_id") in images:
            image = images[block["image_id"]]
            encoded_size = 4 * ((len(image.data) + 2) // 3)
            return {"type": "text", "text":
                    f"[image ({image.media_type}, ~{encoded_size * 3 // 4 // 1024}KB) "
                    "omitted: this bridge is text-only]"}
        if block.get("type") == "tool_result":
            return {**block, "content": [convert(b) for b in block["content"]]}
        return block
    return {"role": atom["role"], "content": convert(atom["content"])}


def historical_image_refs(atom, index):
    block = atom["content"]
    blocks = block["content"] if block.get("type") == "tool_result" else [block]
    return [{"history_item": index, "role": atom["role"], "image": b,
             **({"tool_use_id": block["tool_use_id"]} if block.get("type") == "tool_result" else {})}
            for b in blocks if b.get("type") == "image"]


def delivered_atoms(result):
    """Canonical client-visible output, including IDs assigned by this bridge."""
    if "output" not in result:
        return [{"role": "assistant", "content": block} for block in result.get("content", [])]
    atoms = []
    for item in result["output"]:
        kind = item["type"]
        if kind == "message":
            atoms.extend({"role": "assistant", "content": {"type": "text", "text": b["text"]}}
                         for b in item["content"])
        elif kind in {"function_call", "custom_tool_call"}:
            name = (item.get("namespace", "") + "." if item.get("namespace") else "") + item["name"]
            args = json.loads(item["arguments"]) if kind == "function_call" else {"input": item["input"]}
            atoms.append({"role": "assistant", "content": {
                "type": "tool_use", "id": item["call_id"], "name": name, "input": args}})
    return atoms


def group(atoms):
    messages = []
    for atom in atoms:
        if messages and messages[-1]["role"] == atom["role"]:
            messages[-1]["content"].append(atom["content"])
        else:
            messages.append({"role": atom["role"], "content": [atom["content"]]})
    return messages


def wire_size(text):
    # The context is itself JSON embedded inside a Notion JSON string: quotes,
    # backslashes and newlines are escaped again on the wire.
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def size(value):
    return wire_size(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def compact_tool_result(atom):
    block = atom["content"]
    if block.get("type") != "tool_result":
        return atom
    text = "\n".join(b["text"] for b in block["content"] if b["type"] == "text")
    limit = MAX_TOOL_RESULT_CHARS
    if len(text) <= limit:
        return atom
    marker = f"\n[Bridge truncated tool result: {len(text)} original characters; request narrower file ranges or filtered command output for omitted data.]\n"
    half = max(0, (limit - len(marker)) // 2)
    shortened = text[:half] + marker + (text[-half:] if half else "")
    return with_text(atom, shortened)


def atom_text(atom):
    block = atom["content"]
    if block.get("type") == "text":
        return block["text"]
    if block.get("type") == "tool_result":
        return "\n".join(b.get("text", "") for b in block["content"])
    return None


def with_text(atom, text):
    block = atom["content"]
    if block.get("type") == "text":
        return {**atom, "content": {**block, "text": text}}
    # Keep images even when a long tool result needs text compaction.
    content, inserted = [], False
    for item in block["content"]:
        if item["type"] != "text":
            content.append(item)
        elif not inserted:
            content.append({"type": "text", "text": text})
            inserted = True
    return {**atom, "content": {**block, "content": content}}


def shorten_middle(text, limit):
    if len(text) <= limit:
        return text
    marker = (f"\n[Bridge omitted {len(text) - limit} characters from the middle of this item to bound "
              "context; ask for or re-read details if needed.]\n")
    half = max(0, (limit - len(marker)) // 2)
    return text[:half] + marker + text[-half:] if half else marker


def fit_atoms(atoms, limit, preserve_user=False):
    """Shrink the largest text items (keeping head and tail) until the history fits limit."""
    atoms, shortened = list(atoms), 0
    minimum = 512 if preserve_user else 2000
    for _ in range(2 * len(atoms)):
        sizes = [size(a) for a in atoms]
        excess = sum(sizes) - limit
        if excess <= 0 or not atoms:
            break
        # Tool arguments stay exact. A large immutable invocation must not stop
        # us from shortening the surrounding historical tool output instead.
        candidates = [i for i, atom in enumerate(atoms)
                      if (text := atom_text(atom)) is not None and len(text) >= 2 * minimum
                      and (not preserve_user or atom["role"] == "assistant"
                           or atom["content"]["type"] == "tool_result")]
        if not candidates:
            break
        i = max(candidates, key=sizes.__getitem__)
        text = atom_text(atoms[i])
        ratio = len(text) / max(sizes[i], 1)
        target = max(minimum, int(len(text) - excess * ratio) - 500)
        atoms[i] = with_text(atoms[i], shorten_middle(text, target))
        shortened += 1
    return atoms, shortened


def drop_oldest(atoms, limit):
    """Drop the oldest history to fit limit, cutting only at user input so tool pairs stay intact."""
    sizes = [size(a) for a in atoms]
    if sum(sizes) <= limit:
        return atoms, 0
    best, latest, total, unmatched = None, None, 0, set()
    for i in range(len(atoms) - 1, 0, -1):
        total += sizes[i]
        if total > limit and latest is not None:
            break
        block = atoms[i]["content"]
        if block.get("type") == "tool_result":
            unmatched.add(block.get("tool_use_id"))
        elif block.get("type") == "tool_use":
            unmatched.discard(block.get("id"))
        elif atoms[i]["role"] == "user" and atoms[i].get("image_message_start", True) and not unmatched:
            latest = latest or i
            if total <= limit:
                best = i
    # If even the newest turn exceeds the limit, keep just that turn.
    best = best or latest
    return (atoms[best:], best) if best else (atoms, 0)


def incremental_prompt(prompt, record, budget=None):
    preamble, settings, atoms = context_parts(prompt)
    hashes = [digest(atom) for atom in atoms]
    setting_hashes = {k: digest(v) for k, v in settings.items()}
    snapshot = {"history": hashes, "settings": setting_hashes,
                "system_blocks": {k: [digest(b) for b in settings[k]] for k in ("system", "message_system")}}
    fingerprint = digest([settings, atoms])
    previous = record.get("snapshot", {})
    history = previous.get("history", [])
    images = getattr(prompt, "images", {})
    common, image_upgrades = 0, list(previous.get("pending_image_upgrades", []))
    for i, (old, new) in enumerate(zip(history, hashes)):
        if old != new:
            # Upgrading the normalizer must not turn every old screenshot into
            # an edited user request and replay the rest of a long conversation.
            if not images or old != digest(legacy_image_atom(atoms[i], images)):
                break
            image_upgrades.extend(historical_image_refs(atoms[i], i))
        common = i + 1
    append = bool(previous) and common == len(history)
    start = len(history) if append else 0
    if previous and not append:
        # Desktop edits interruption/retry markers near the end of its history.
        # Retain the known prefix in Notion instead of replaying the whole chat
        # for a tiny tail revision. Start at a user turn with no open tool calls,
        # so a tool result is never detached from the call that produced it.
        open_tools = set()
        for i, atom in enumerate(atoms[:common + 1]):
            block = atom["content"]
            if (atom["role"] == "user" and atom.get("image_message_start", True)
                    and block.get("type") != "tool_result" and not open_tools):
                start = i
            if block.get("type") == "tool_use":
                open_tools.add(block.get("id"))
            elif block.get("type") == "tool_result":
                open_tools.discard(block.get("tool_use_id"))
    # Acknowledged output is already present in Notion. Only relay assigned tool IDs,
    # not a second copy of its prose, code patches, or tool arguments.
    acknowledged = previous.get("assistant", [])
    if append and acknowledged and hashes[start:start + len(acknowledged)] == acknowledged:
        start += len(acknowledged)
    else:
        acknowledged = []
    mode = "initial" if not record else "append" if append else "resync"
    changes = {}
    for k, v in settings.items():
        if not previous or previous.get("settings", {}).get(k) != setting_hashes[k]:
            prior_blocks = previous.get("system_blocks", {}).get(k)
            if previous and prior_blocks is not None and isinstance(v, list):
                current_blocks = [digest(b) for b in v]
                if current_blocks[:len(prior_blocks)] == prior_blocks:
                    changes[k + "_append"] = v[len(prior_blocks):]
                    continue
            changes[k] = v
    pending_atoms = atoms[start:]
    kept = [compact_tool_result(atom) for atom in pending_atoms]
    truncated = sum(a != b for a, b in zip(pending_atoms, kept))
    omitted = 0
    if not append:
        # Only a fresh thread needs the seed cap. Resync still has earlier context
        # in Notion and must not discard it just to meet the smaller seed budget.
        overhead = wire_size(preamble) + size({"mode": mode, **changes}) + 2000
        cap = min(budget or MAX_SEND_CHARS, MAX_SEND_CHARS)
        if not record:
            cap = min(cap, NEW_THREAD_MAX_CHARS or MAX_SEND_CHARS)
        kept, omitted = drop_oldest(kept, cap - overhead)
        if not record and NEW_THREAD_MAX_CHARS:
            # Shrinking single items only serves the usage cap; a turn still over
            # MAX_SEND_CHARS is rejected explicitly below instead of being hidden.
            usage_cap = min(budget or NEW_THREAD_MAX_CHARS, NEW_THREAD_MAX_CHARS)
            kept, shortened = fit_atoms(kept, max(0, usage_cap - overhead))
            truncated += shortened
        if not record:
            # A generous seed/usage budget cannot bypass the wire cap. Preserve
            # user instructions and tool arguments while bounding replayed output.
            kept, shortened = fit_atoms(kept, max(0, cap - overhead), preserve_user=True)
            truncated += shortened
    payload = {"mode": mode, **changes, "messages": group(kept)}
    selected_images = images_in_messages(payload["messages"], images) if images else {}
    if len(selected_images) > MAX_TURN_IMAGES:
        raise UpstreamError("A turn can contain at most 19 distinct images; split the input", 413, rejected=True)
    # Upgrade old placeholders without replaying their old tool calls/results.
    # Current-turn images take priority. Any overflow is deferred durably, not
    # silently dropped or allowed to break an otherwise small new request.
    upgrades = {r["image"]["image_id"]: r for r in image_upgrades
                if r["image"]["image_id"] not in selected_images}
    selected_upgrades = []
    for image_id, ref in reversed(list(upgrades.items())):
        if image_id in images and len(selected_images) < MAX_TURN_IMAGES:
            selected_images[image_id] = images[image_id]
            selected_upgrades.append(ref)
    remaining = [ref for image_id, ref in upgrades.items() if image_id not in selected_images]
    if remaining:
        snapshot["pending_image_upgrades"] = remaining
    if selected_upgrades:
        payload["historical_image_context"] = list(reversed(selected_upgrades))
        payload["historical_image_note"] = (
            "These are actual images from the already-received client history, previously represented "
            "by text placeholders. Their history_item and optional tool_use_id locate the original "
            "messages. Use them as context for the latest request; do not repeat historical tool calls.")
    if remaining:
        payload["deferred_historical_images"] = len(remaining)
    if not append and start:
        payload["unchanged_prefix_items"] = start
        payload["revised_history_note"] = (
            "The unchanged client-history prefix is already in this thread. Replace the prior client "
            "history suffix from this content-block index with these messages. This includes client "
            "interruptions or edits; use the revised suffix as authority, and do not repeat old actions.")
    if omitted:
        payload["omitted_earlier_items"] = omitted
        payload["omitted_note"] = ("The oldest client history was omitted by the bridge to bound context; "
                                   "rely on the remaining history and re-read files if details are needed.")
    unset = sorted(set(previous.get("settings", {})) - set(settings)) if previous else []
    if unset:
        payload["unset"] = unset
    if acknowledged and previous.get("tool_ids"):
        payload["previous_tool_ids"] = previous["tool_ids"]
    if record.get("discarded_turn"):
        payload["previous_response_not_delivered"] = True
    snapshot["truncated_tool_results"] = truncated
    snapshot["omitted_earlier_items"] = omitted
    instructions = (
        "Continue the existing client conversation; do not repeat prior actions. Keep unchanged settings. "
        "Fields ending _append add to the named setting; unset removes settings. previous_tool_ids maps "
        "your previous tool calls in order to client-assigned IDs. Respond only to new messages, in the "
        "same JSON content envelope. Resync supplies the client's current history window; retain relevant "
        "earlier context in this thread unless the client explicitly corrects it. Omitted history is not "
        "a request to forget it. Historical tool calls are not new actions. If previous_response_not_delivered "
        "is true, the client did not receive your last response or execute its proposed local tools; "
        "answer the current request using the supplied client history as the authority for executed actions.\n"
    )
    # The complete protocol description is needed only on initial binding/resync.
    rendered = (preamble if not previous else "") + instructions + MARKER + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"))
    if selected_images:
        rendered = ImagePrompt(rendered, selected_images)
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

    async def dispatch(self, upstream, record, text, effort, continuation):
        pending = record["pending"]
        try:
            await upstream.start_turn(text, effort, record["thread_id"], pending["event_id"], continuation)
        except UpstreamError as exc:
            if (not continuation or exc.rejected or exc.exhausted or exc.status < 500
                    or not hasattr(upstream, "turn_is_absent")):
                raise
            absent = await upstream.turn_is_absent(record["thread_id"], record.get("cursor"),
                                                   record.get("sequence", -1), record.get("turn_id"),
                                                   pending.get("submitted_at"))
            if not absent:
                # The write may have been accepted; the caller will poll it on retry.
                raise
            # One bounded retry, using the same event ID and exact payload, only
            # after the unchanged remote checkpoint proves it was not accepted.
            pending["submission_retries"] = pending.get("submission_retries", 0) + 1
            await upstream.start_turn(text, effort, record["thread_id"], pending["event_id"], continuation)

    async def run(self, upstream, key, prompt, effort, render, progress=None, stateless=False,
                  preserve_history=False, decorate=None):
        record = self.get(key)
        sent, snapshot, fingerprint, mode = incremental_prompt(prompt, record)
        fingerprint = digest([fingerprint, effort])
        cached = record.get("auxiliary_cache", {}) if preserve_history else record
        if cached.get("fingerprint") == fingerprint and cached.get("result"):
            return cached["result"], {**cached["audit"], "cached": True}
        pending = record.get("pending")
        superseded = None
        retry_event_id = None
        retry_repair = {}
        recovered_unsent = False
        scope = getattr(upstream, "account_scope", None)
        bound = record.get("scope") or self.namespace or scope
        same = bool(pending) and pending["fingerprint"] == fingerprint
        if record and scope and bound != scope and not (same and "answer" in pending):
            # Workspace failover: this binding's thread lives in another workspace. Replay the
            # authoritative client history onto a fresh thread here. An unfinished turn there
            # was never delivered to the client, so no tool call can run twice.
            superseded = record
            record = {}
            sent, snapshot, _, _ = incremental_prompt(prompt, record, (SIDE_MAX_SEND_CHARS or None) if stateless
                                                      else (FAILOVER_SEED_CHARS or None))
            mode = "failover"
            pending = None
        elif stateless and not same:
            # Side requests (classifiers, schema calls) rewrite their prompt every time, so
            # they can never append. Notion re-reads the whole thread per turn: send each one
            # on a fresh thread instead of resyncing onto an ever-growing one.
            record = {}
            sent, snapshot, _, mode = incremental_prompt(prompt, record, SIDE_MAX_SEND_CHARS or None)
            pending = None
        elif (pending and "answer" not in pending and not pending.get("dispatch_accepted")
              and hasattr(upstream, "turn_is_absent")):
            # A failed/ interrupted POST may never have reached Notion. Legacy
            # pending records also lack an acknowledgement. Do not poll forever
            # when repeated remote reads prove the completed checkpoint is idle.
            if await upstream.turn_is_absent(record["thread_id"], record.get("cursor"),
                                             record.get("sequence", -1), record.get("turn_id"),
                                             pending.get("submitted_at")):
                retry_event_id = pending["event_id"] if same else None
                if same and pending.get("repair_prompt"):
                    sent = pending["repair_prompt"]
                    retry_repair = {k: pending[k] for k in ("repair_prompt", "repair_attempts")}
                recovered_unsent = True
                record.pop("pending")
                pending = None
                # Keep the original history, scope and cursor. The replacement
                # pending record is committed below before dispatch; identical
                # retries retain their event ID as an additional deduplication aid.
        if pending and pending["fingerprint"] != fingerprint:
            # Drain the prior turn before appending; a changed retry must not
            # silently create a new thread or deliver stale executable output.
            if "answer" not in pending:
                try:
                    text, cursor, sequence, turn_id = await upstream.wait_for_turn(
                        record["thread_id"], record.get("cursor"), record.get("sequence", -1),
                        record.get("turn_id"))
                except UpstreamError as exc:
                    completed = getattr(exc, "completed_turn", None)
                    if exc.exhausted or not completed:
                        raise
                    text = ""
                    cursor, sequence, turn_id = (completed[k] for k in ("cursor", "sequence", "turn_id"))
                pending.update(answer=text, cursor=cursor, sequence=sequence, turn_id=turn_id)
                record["thread_chars"] = record.get("thread_chars", 0) + pending["input_chars"] + len(text)
            record.update(cursor=pending["cursor"], sequence=pending["sequence"], turn_id=pending["turn_id"],
                          discarded_turn=True)
            if not pending.get("preserve_history"):
                record["snapshot"] = pending["snapshot"]
                for field in ("fingerprint", "result", "audit"):
                    record.pop(field, None)
            else:
                record["snapshot"] = {**record.get("snapshot", {}),
                                      **{k: pending["snapshot"][k] for k in ("settings", "system_blocks")}}
            record.pop("pending")
            # Persist the completed cursor even if the next send is rejected or
            # interrupted. A later retry continues this same thread.
            self.save(key, record)
            sent, snapshot, _, mode = incremental_prompt(prompt, record)
            pending = None
        if not pending and THREAD_MAX_CHARS and record and mode == "append" and \
                record.get("thread_chars", 0) + len(sent) > THREAD_MAX_CHARS:
            # Restore the old binding if the fresh thread is rejected.
            superseded = record
            record = {}
            sent, snapshot, _, _ = incremental_prompt(prompt, record, ROLLOVER_SEED_CHARS)
            mode = "rollover"
        if not pending:
            continuation = bool(record)
            record = record or {"thread_id": str(uuid.uuid4()), "effort": effort, **({"scope": scope} if scope else {})}
            if getattr(sent, "images", None):
                sent = await upstream.prepare_images(sent, record["thread_id"], continuation)
            if wire_size(sent) > MAX_SEND_CHARS:
                # Nothing has been saved or dispatched yet, so the existing binding is untouched.
                error = UpstreamError(
                    f"Turn payload {wire_size(sent)} UTF-8 bytes exceeds {MAX_SEND_CHARS}; "
                    "compact the conversation", 413, rejected=True)
                error.diagnostics = {"error_code": "context_payload_too_large", "stage": "prepare",
                                     "history_mode": mode, "input_bytes": wire_size(sent),
                                     "limit_bytes": MAX_SEND_CHARS}
                raise error
            pending = {"fingerprint": fingerprint, "snapshot": snapshot, "mode": mode,
                       "input_chars": len(sent), "input_bytes": wire_size(sent),
                       "event_id": retry_event_id or str(uuid.uuid4()),
                       "submitted_at": time.time(), "dispatch_accepted": False,
                       "recovered_unsent": recovered_unsent,
                       "preserve_history": preserve_history, **retry_repair}
            record["pending"] = pending
            # Commit BEFORE dispatch, so a disconnect/restart cannot create a second thread or turn.
            self.save(key, record)
            try:
                await self.dispatch(upstream, record, sent, effort, continuation)
                pending["dispatch_accepted"] = True
                self.save(key, record)
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
        while True:
            if "answer" not in pending:
                try:
                    text, cursor, sequence, turn_id = await upstream.wait_for_turn(
                        record["thread_id"], record.get("cursor"), record.get("sequence", -1), record.get("turn_id"))
                except UpstreamError as exc:
                    completed = getattr(exc, "completed_turn", None)
                    if exc.exhausted or not completed:
                        raise
                    # A terminal generation failure is safe to correct on this
                    # same thread. No partial answer or executable tool escapes.
                    text = ""
                    cursor, sequence, turn_id = (completed[k] for k in ("cursor", "sequence", "turn_id"))
                pending.update(answer=text, cursor=cursor, sequence=sequence, turn_id=turn_id)
                record["thread_chars"] = record.get("thread_chars", 0) + pending["input_chars"] + len(text)
                self.save(key, record)
            # Nothing (including leading prose) is delivered before validation.
            # Correct invalid output on the same thread, with a durable bound so
            # client retries/restarts cannot endlessly regenerate or execute it.
            try:
                result = render(pending["answer"])
                break
            except AnswerError as exc:
                attempts = pending.get("repair_attempts", 0)
                if attempts >= MAX_ANSWER_REPAIRS:
                    error = UpstreamError("Notion returned invalid output after two correction attempts; "
                                          "no tools from this response were executed. Revise the request to continue.", 422)
                    error.diagnostics = {**exc.diagnostics, "repair_attempts": attempts,
                                         "stage": "answer_validation"}
                    raise error from None
                _, settings, _ = context_parts(prompt)
                repair = {
                    "validation_error": str(exc), **exc.repair,
                    "tool_choice": settings.get("tool_choice", {"type": "auto"}),
                    "allowed_tool_names": [t["name"] for t in settings.get("tools", [])],
                }
                correction = (
                    "PROTOCOL CORRECTION: Your last response failed the local client's validation. "
                    "NONE of its tools or text were delivered or executed. Do not perform actions in Notion. "
                    "Return a corrected response for the same latest user request, not a new task. "
                    "Return exactly one JSON object with content containing text and/or tool_use blocks. "
                    "Tool calls must use name and input, no id. Use only the current client tools, obey "
                    "tool_choice, and match every required field and nested item in the supplied schema. "
                    "Do not invent tool results or treat prior unexecuted tool calls as completed. "
                    "Do not explain the protocol correction to the user.\nVALIDATION_CONTEXT_JSON:\n"
                    + json.dumps(repair, ensure_ascii=False, separators=(",", ":")))
                if wire_size(correction) > MAX_SEND_CHARS:
                    raise UpstreamError("Tool schema is too large for a bounded protocol correction", 422) from None
                record.update(cursor=pending["cursor"], sequence=pending["sequence"],
                              turn_id=pending["turn_id"], discarded_turn=True)
                for field in ("answer", "cursor", "sequence", "turn_id"):
                    pending.pop(field, None)
                pending.update(repair_attempts=attempts + 1, repair_prompt=correction,
                               event_id=str(uuid.uuid4()), submitted_at=time.time(), dispatch_accepted=False,
                               input_chars=len(correction), input_bytes=wire_size(correction))
                self.save(key, record)
                # On an ambiguous write, leave the correction pending. A later
                # identical retry reconciles and reuses this exact correction/id.
                await self.dispatch(upstream, record, correction, effort, True)
                pending["dispatch_accepted"] = True
                self.save(key, record)
        if decorate is not None:
            # Persist exactly the displayed answer so its next acknowledgement
            # stays incremental, including the gateway's optional usage footer.
            result = await decorate(result, upstream)
        output_atoms = delivered_atoms(result)
        pending["snapshot"]["assistant"] = [digest(a) for a in output_atoms]
        pending["snapshot"]["tool_ids"] = [{"id": a["content"]["id"], "name": a["content"]["name"]}
                                            for a in output_atoms if a["content"]["type"] == "tool_use"]
        info = {"client_input_chars": len(prompt),
                "truncated_tool_results": pending["snapshot"].get("truncated_tool_results", 0),
                "omitted_earlier_items": pending["snapshot"].get("omitted_earlier_items", 0),
                "auxiliary": preserve_history, "recovered_pending": bool(record.get("discarded_turn")),
                "recovered_unsent": pending.get("recovered_unsent", False),
                "repair_attempts": pending.get("repair_attempts", 0),
                "submission_retries": pending.get("submission_retries", 0),
                "thread_id": record["thread_id"], "session_hash": key, "history_mode": pending["mode"],
                "upstream_input_chars": pending["input_chars"],
                "upstream_input_bytes": pending.get("input_bytes"), "cached": False,
                "thread_effort": record["effort"], "thread_chars": record.get("thread_chars", 0)}
        cache = {"fingerprint": fingerprint, "result": result, "audit": info}
        if preserve_history:
            # Recaps share the upstream thread, but must not become part of the
            # main client's history or replace its last acknowledged tool IDs.
            record["auxiliary_cache"] = cache
            previous = record.get("snapshot", {})
            record["snapshot"] = {**previous, **{k: pending["snapshot"][k]
                                                 for k in ("settings", "system_blocks")}}
        else:
            record.update(snapshot=pending["snapshot"], **cache)
            record.pop("auxiliary_cache", None)
        record.update(cursor=pending["cursor"], sequence=pending["sequence"], turn_id=pending["turn_id"])
        record.pop("pending")
        record.pop("discarded_turn", None)
        self.save(key, record)
        return result, info
