import json
import tempfile
import unittest
from pathlib import Path

from app.agent_gateway.protocol import prepare_request
from app.agent_gateway.sessions import SessionStore, client_session, incremental_prompt
from app.agent_gateway.upstream import Transcript, UpstreamError


def prompt(messages, **extra):
    return prepare_request({"model": "claude-opus-5-5", "max_tokens": 1000,
                            "messages": messages, **extra})[0]


class FakeNotion:
    def __init__(self):
        self.writes = []
        self.polls = []
        self.timeout = False
        self.reject = False

    async def start_turn(self, text, effort, thread, event, continuation):
        if self.reject:
            raise UpstreamError("rejected", rejected=True)
        self.writes.append((thread, continuation, text))

    async def wait_for_turn(self, thread, cursor, sequence, turn_id):
        self.polls.append((thread, cursor, sequence, turn_id))
        if self.timeout:
            raise TimeoutError()
        n = len(self.writes)
        return '{"content":[{"type":"text","text":"OK"}]}', f"cursor{n}", n * 10, f"turn{n}"


class SessionChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sessions.db"
        self.store = SessionStore(self.path)
        self.up = FakeNotion()
        self.first = [{"role": "user", "content": "Remember violet."}]

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def run_turn(self, key="one", messages=None):
        return await self.store.run(self.up, key, prompt(messages or self.first), "medium", json.loads)

    async def test_resume_after_restart_and_new_session_isolation(self):
        _, first = await self.run_turn()
        self.store.close()
        self.store = SessionStore(self.path)
        messages = self.first + [{"role": "assistant", "content": "OK"}, {"role": "user", "content": "What color?"}]
        _, second = await self.run_turn(messages=messages)
        _, other = await self.run_turn(key="two")
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertNotEqual(first["thread_id"], other["thread_id"])
        self.assertEqual([w[1] for w in self.up.writes], [False, True, False])
        self.assertNotIn("Remember violet", self.up.writes[1][2])
        self.assertIn("What color?", self.up.writes[1][2])
        self.assertEqual(self.up.polls[1][1:], ("cursor1", 10, "turn1"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_identical_retry_returns_cached_response_without_new_turn(self):
        a, _ = await self.run_turn()
        b, info = await self.run_turn()
        self.assertEqual(a, b)
        self.assertTrue(info["cached"])
        self.assertEqual(len(self.up.writes), 1)

    async def test_timeout_then_restart_polls_original_turn_without_resubmission(self):
        self.up.timeout = True
        with self.assertRaises(TimeoutError):
            await self.run_turn()
        self.store.close()
        self.store = SessionStore(self.path)
        self.up.timeout = False
        await self.run_turn()
        self.assertEqual(len(self.up.writes), 1)
        self.assertEqual(len(self.up.polls), 2)

    async def test_pending_different_request_recovers_on_isolated_thread(self):
        self.up.timeout = True
        with self.assertRaises(TimeoutError):
            await self.run_turn()
        old_thread = self.up.writes[0][0]
        self.store.close()
        self.store = SessionStore(self.path)
        self.up.timeout = False
        messages = self.first + [{"role": "user", "content": "different"}]
        _, info = await self.run_turn(messages=messages)
        self.assertNotEqual(info["thread_id"], old_thread)
        self.assertEqual(info["history_mode"], "initial")
        self.assertFalse(self.up.writes[-1][1])
        self.assertIn("Remember violet", self.up.writes[-1][2])
        self.assertIn("different", self.up.writes[-1][2])
        self.assertEqual(self.up.polls[-1][1:], (None, -1, None))
        await self.run_turn(messages=messages)
        self.assertEqual(len(self.up.writes), 2)

    async def test_changed_settings_can_recover_failed_validation(self):
        def invalid(text):
            raise ValueError("invalid answer")
        with self.assertRaises(ValueError):
            await self.store.run(self.up, "one", prompt(self.first), "medium", invalid)
        old = self.store.get("one")
        self.assertIn("answer", old["pending"])
        _, info = await self.store.run(self.up, "one", prompt(self.first, system="Updated context"),
                                       "medium", json.loads)
        self.assertNotEqual(info["thread_id"], old["thread_id"])
        self.assertEqual(len(self.up.writes), 2)

    async def test_rejected_recovery_preserves_original_pending_request(self):
        self.up.timeout = True
        with self.assertRaises(TimeoutError):
            await self.run_turn()
        original = self.store.get("one")
        self.up.reject = True
        with self.assertRaises(UpstreamError):
            await self.run_turn(messages=[{"role": "user", "content": "different"}])
        self.assertEqual(self.store.get("one"), original)
        self.up.reject = self.up.timeout = False
        await self.run_turn()
        self.assertEqual(len(self.up.writes), 1)

    async def test_interrupted_recovery_resumes_new_thread_without_duplicate(self):
        self.up.timeout = True
        with self.assertRaises(TimeoutError):
            await self.run_turn()
        messages = [{"role": "user", "content": "different"}]
        with self.assertRaises(TimeoutError):
            await self.run_turn(messages=messages)
        new_thread = self.up.writes[-1][0]
        self.up.timeout = False
        _, info = await self.run_turn(messages=messages)
        self.assertEqual(info["thread_id"], new_thread)
        self.assertEqual(len(self.up.writes), 2)

    async def test_definitive_rejection_does_not_leave_pending_binding(self):
        self.up.reject = True
        with self.assertRaises(UpstreamError):
            await self.run_turn()
        self.assertEqual(self.store.get("one"), {})

    async def test_compaction_resyncs_existing_thread(self):
        _, first = await self.run_turn()
        _, second = await self.run_turn(messages=[{"role": "user", "content": "Compacted summary. Next task."}])
        self.assertEqual(first["thread_id"], second["thread_id"])
        self.assertEqual(second["history_mode"], "resync")
        self.assertIn('"mode":"resync"', self.up.writes[1][2])

    def test_headers_metadata_fallback_and_agent_isolation(self):
        a, source = client_session({"X-Claude-Code-Session-Id": "a"}, {}, "messages")
        b, _ = client_session({}, {"metadata": {"user_id": json.dumps({"session_id": "a"})}}, "messages")
        self.assertEqual(a, b)
        self.assertEqual(source, "claude-header")
        child, _ = client_session({"x-claude-code-session-id": "a", "x-claude-code-agent-id": "child"}, {}, "messages")
        self.assertNotEqual(a, child)
        c, _ = client_session({}, {"client_metadata": {"thread_id": "c", "session_id": "ephemeral"}}, "responses")
        d, _ = client_session({}, {"client_metadata": {"thread_id": "c", "session_id": "changed"}}, "responses")
        self.assertEqual(c, d)
        self.assertNotEqual(client_session({}, {}, "messages")[0], client_session({}, {}, "messages")[0])

    def test_delta_handles_merged_messages_tool_ids_and_changed_system(self):
        tool = {"name": "Read", "input_schema": {"type": "object"}}
        first = [{"role": "user", "content": "Read file"}, {"role": "system", "content": "budget 1"}]
        _, snapshot, _, _ = incremental_prompt(prompt(first, tools=[tool]), {})
        next_messages = [first[0], {"role": "assistant", "content": [
            {"type": "text", "text": "Reading"}, {"type": "tool_use", "id": "local-id", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "local-id", "content": "contents"}]},
            {"role": "system", "content": "budget 2"}]
        delta, _, _, mode = incremental_prompt(prompt(next_messages, tools=[tool]), {"snapshot": snapshot})
        context = json.loads(delta.split("CLIENT_CONTEXT_JSON:\n", 1)[1])
        self.assertEqual(mode, "append")
        self.assertNotIn("tools", context)
        self.assertEqual(context["messages"][0]["role"], "assistant")
        self.assertEqual(context["messages"][1]["content"][0]["tool_use_id"], "local-id")
        self.assertIn("budget 2", delta)

    def test_stale_completion_cannot_complete_new_turn(self):
        t = Transcript(after_sequence=10, previous_turn_id="old")
        t.update({"patches": [
            {"op": "put", "entity": {"kind": "turn_completed", "id": "old-end", "sequence": 10,
                                      "turn_id": "old", "stop_reason": "end_turn"}}]})
        self.assertIsNone(t.result())
        t.update({"patches": [
            {"op": "put", "entity": {"kind": "assistant_message", "id": "new-text", "sequence": 11,
                                      "turn_id": "new", "content": [{"type": "text", "text": "NEW"}]}},
            {"op": "put", "entity": {"kind": "turn_completed", "id": "new-end", "sequence": 12,
                                      "turn_id": "new", "stop_reason": "end_turn"}}]})
        self.assertEqual(t.result(), "NEW")
