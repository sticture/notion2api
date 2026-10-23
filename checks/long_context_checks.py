"""Long Desktop tool histories must fit fresh-thread imports without altering actions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent_gateway.protocol import prepare_request
from app.agent_gateway.sessions import (MARKER, SessionStore, incremental_prompt, wire_size)
from app.agent_gateway.pool import WorkspacePool
from app.agent_gateway.upstream import UpstreamError
from session_checks import ScopedNotion


def long_history():
    messages = [{'role': 'user', 'content': 'Preserve this requirement exactly. ' + 'u' * 5000}]
    for i in range(32):
        messages += [
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'call-{i}',
                'name': 'Bash', 'input': {'command': 'immutable-command ' + 'x' * (28000 if i == 0 else 100)}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f'call-{i}',
                'content': f'HEAD-{i} ' + '中文\\"\n' * 1400 + f' TAIL-{i}'}]},
        ]
    return messages


def prompt(messages):
    return prepare_request({'model': 'claude-opus-5-5', 'max_tokens': 1000,
                            'messages': messages})[0]


class LongContextChecks(unittest.IsolatedAsyncioTestCase):
    def test_client_history_over_four_million_characters_still_sends_only_delta(self):
        history = [{'role': 'user', 'content': 'Previously supplied context ' + 'x' * 4100000}]
        _, snapshot, _, _ = incremental_prompt(prompt(history), {})
        continued = history + [{'role': 'assistant', 'content': 'Acknowledged'},
                               {'role': 'user', 'content': 'Only this new requirement.'}]
        sent, _, _, mode = incremental_prompt(prompt(continued), {'snapshot': snapshot})
        self.assertEqual(mode, 'append')
        self.assertLess(wire_size(sent), 2000)
        self.assertIn('Only this new requirement.', sent)
        self.assertNotIn('Previously supplied context', sent)

    def test_large_immutable_call_does_not_prevent_bounding_historical_output(self):
        messages = long_history()
        for seed in (0, 300000):
            with self.subTest(seed=seed), \
                    patch('app.agent_gateway.sessions.NEW_THREAD_MAX_CHARS', seed), \
                    patch('app.agent_gateway.sessions.MAX_SEND_CHARS', 180000):
                sent, state, _, _ = incremental_prompt(prompt(messages), {})
            self.assertLessEqual(wire_size(sent), 180000)
            payload = json.loads(sent.split(MARKER)[1])
            replay = payload['messages']
            self.assertEqual(replay[0]['content'][0]['text'], messages[0]['content'])
            calls = [b for m in replay for b in m['content'] if b['type'] == 'tool_use']
            results = [b for m in replay for b in m['content'] if b['type'] == 'tool_result']
            self.assertEqual(len(calls), 32)
            self.assertEqual(calls[0]['input'], messages[1]['content'][0]['input'])
            self.assertEqual({b['id'] for b in calls}, {b['tool_use_id'] for b in results})
            for i, block in enumerate(results):
                text = block['content'][0]['text']
                self.assertIn(f'HEAD-{i}', text)
                self.assertIn(f'TAIL-{i}', text)
            self.assertGreater(state['truncated_tool_results'], 0)
            self.assertIn('Bridge omitted', sent)

    async def test_quota_failover_import_retry_and_restart_keep_the_new_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            store = SessionStore(path)
            a, b = ScopedNotion('original'), ScopedNotion('backup')
            pool = WorkspacePool([('original', a), ('backup', b)])
            messages = long_history()
            # Establish the original binding before the history has accumulated.
            await pool.run(store, 'desktop', prompt(messages[:1]), 'medium', json.loads)
            record = store.get('desktop')
            # The original workspace has already received the prior tool rounds.
            record['snapshot'] = incremental_prompt(prompt(messages[:-2]), {})[1]
            store.save('desktop', record)
            a.exhausted = True
            with patch('app.agent_gateway.sessions.NEW_THREAD_MAX_CHARS', 300000), \
                    patch('app.agent_gateway.sessions.FAILOVER_SEED_CHARS', 300000), \
                    patch('app.agent_gateway.sessions.MAX_SEND_CHARS', 180000):
                result, info = await pool.run(store, 'desktop', prompt(messages), 'medium', json.loads)
                self.assertEqual(info['history_mode'], 'failover')
                self.assertEqual(info['profile'], 'backup')
                self.assertLessEqual(wire_size(b.writes[-1][2]), 180000)
                store.close()
                store = SessionStore(path)
                _, cached = await pool.run(store, 'desktop', prompt(messages), 'medium', json.loads)
                self.assertTrue(cached['cached'])
                self.assertEqual(len(b.writes), 1)
                followup = messages + [{'role': 'assistant', 'content': result['content']},
                                       {'role': 'user', 'content': 'Continue with the next check.'}]
                _, final = await pool.run(store, 'desktop', prompt(followup), 'medium', json.loads)
            self.assertEqual(final['thread_id'], info['thread_id'])
            self.assertEqual(final['profile'], 'backup')
            self.assertEqual(final['history_mode'], 'append')
            self.assertLess(final['upstream_input_bytes'], 2000)
            self.assertNotIn('pending', store.get('desktop'))
            store.close()

    async def test_unshrinkable_user_request_still_fails_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / 'state.db')
            up = ScopedNotion('original')
            with patch('app.agent_gateway.sessions.NEW_THREAD_MAX_CHARS', 300000), \
                    patch('app.agent_gateway.sessions.MAX_SEND_CHARS', 180000), \
                    self.assertRaises(UpstreamError) as ctx:
                await store.run(up, 'desktop', prompt([{'role': 'user', 'content': 'x' * 240000}]),
                                'medium', json.loads)
            self.assertEqual(ctx.exception.status, 413)
            self.assertEqual(ctx.exception.diagnostics['error_code'], 'context_payload_too_large')
            self.assertEqual(store.get('desktop'), {})
            self.assertEqual(up.writes, [])
            store.close()
