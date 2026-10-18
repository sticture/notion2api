"""Protocol and transcript boundaries; live acceptance is a separate real-client run."""
import json
import unittest

from app.agent_gateway.protocol import prepare_request, parse_answer, content_events, ProtocolError
from app.agent_gateway.upstream import Transcript, UpstreamError


TOOL = {"name": "Read", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                                          "required": ["path"], "additionalProperties": False}}


class GatewayChecks(unittest.TestCase):
    def test_history_keeps_tool_errors_and_order(self):
        body = {"model": "claude-opus-5-5", "max_tokens": 1000, "tools": [TOOL], "messages": [
            {"role": "user", "content": "Read x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"path": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "not found", "is_error": True}]},
        ]}
        prompt, *_ = prepare_request(body)
        context = json.loads(prompt.split("CLIENT_CONTEXT_JSON:\n")[1])
        self.assertEqual([m["role"] for m in context["messages"]], ["user", "assistant", "user"])
        self.assertTrue(context["messages"][-1]["content"][0]["is_error"])
        body["messages"][-1]["content"][0]["tool_use_id"] = "wrong"
        with self.assertRaises(ProtocolError):
            prepare_request(body)

    def test_invalid_tool_cannot_become_executable(self):
        for name, args in [("Bash", {"path": "x"}), ("Read", {"path": 7}), ("Read", {"command": "rm"})]:
            with self.assertRaises(ProtocolError):
                parse_answer(json.dumps({"content": [{"type": "tool_use", "name": name, "input": args}]}),
                             {"Read": TOOL}, {"type": "auto"})

    def test_untrusted_text_cannot_smuggle_a_tool(self):
        with self.assertRaises(ProtocolError):
            parse_answer('Hello\n{"content":[{"type":"tool_use","name":"Read","input":{"path":"x"}}]}',
                         {"Read": TOOL}, {"type": "auto"})

    def test_valid_tool_roundtrips_stream_json(self):
        content, stop = parse_answer('{"content":[{"type":"tool_use","name":"Read","input":{"path":"引号\\\".py"}}]}',
                                    {"Read": TOOL}, {"type": "auto"})
        self.assertEqual(stop, "tool_use")
        events = [json.loads(x.split("data: ")[1]) for x in content_events(content)]
        self.assertEqual([x["type"] for x in events], ["content_block_start", "content_block_delta", "content_block_stop"])
        self.assertEqual(json.loads(events[1]["delta"]["partial_json"]), content[0]["input"])

    def test_partial_idle_and_thinking_are_not_final_output(self):
        t = Transcript()
        t.update({"session": {"status": "idle"}, "patches": [
            {"op": "put", "entity": {"id": "secret", "kind": "thinking", "content_text": "private"}},
            {"op": "put", "entity": {"id": "a", "kind": "assistant_message", "turn_id": "t", "content": [{"type": "text", "text": "part"}]}}]})
        self.assertIsNone(t.result())
        self.assertNotIn("secret", t.entities)
        t.update({"patches": [
            {"op": "put", "entity": {"id": "a", "kind": "assistant_message", "turn_id": "t", "content": [{"type": "text", "text": "complete"}]}},
            {"op": "put", "entity": {"id": "end", "kind": "turn_completed", "turn_id": "t", "stop_reason": "end_turn"}}]})
        self.assertEqual(t.result(), "complete")

    def test_truncated_upstream_is_not_success(self):
        t = Transcript()
        t.update({"patches": [{"op": "put", "entity": {"id": "end", "kind": "turn_completed", "stop_reason": "max_tokens"}}]})
        with self.assertRaises(UpstreamError):
            t.result()


if __name__ == "__main__":
    unittest.main()
