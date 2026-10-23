"""An upstream edge block must not log Desktop out or move a bound session."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.agent_gateway.upstream import NotionAgent, UpstreamError
from app.agent_gateway.sessions import SessionStore
from app.agent_gateway.pool import WorkspacePool
from long_context_checks import prompt
from session_checks import ScopedNotion


HTML = '<html><title>Attention Required! | Cloudflare</title>private edge response</html>'


class EdgeChecks(unittest.IsolatedAsyncioTestCase):
    async def test_html_403_is_service_unavailable_with_backoff_and_no_secret_body(self):
        for endpoint in ('getThreadTranscript', 'createAgentThread', 'sendEventToAgentThread'):
            with self.subTest(endpoint=endpoint):
                up = object.__new__(NotionAgent)
                calls = []
                def respond(request):
                    calls.append(request)
                    return httpx.Response(403, text=HTML,
                        headers={'content-type': 'text/html', 'server': 'cloudflare'})
                up.client = httpx.AsyncClient(base_url='https://app.notion.com',
                                             transport=httpx.MockTransport(respond))
                with patch('app.agent_gateway.upstream.time.monotonic', return_value=100):
                    for _ in range(2):
                        with self.assertRaises(UpstreamError) as ctx:
                            await up.post(endpoint, {'private': 'request'})
                        error = ctx.exception
                        self.assertEqual(error.status, 503)
                        self.assertEqual(error.rejected, endpoint != 'getThreadTranscript')
                        self.assertFalse(error.exhausted)
                        self.assertEqual(error.diagnostics['error_code'], 'upstream_edge_blocked')
                        self.assertNotIn('private', str(error) + json.dumps(error.diagnostics))
                self.assertEqual(len(calls), 2)
                with patch('app.agent_gateway.upstream.time.monotonic', return_value=131):
                    with self.assertRaises(UpstreamError):
                        await up.post(endpoint, {})
                self.assertEqual(len(calls), 4)
                await up.close()

    async def test_official_origin_fallback_preserves_payload_and_pins_successful_origin(self):
        for endpoint in ('getThreadTranscript', 'createAgentThread', 'sendEventToAgentThread'):
            with self.subTest(endpoint=endpoint):
                up = object.__new__(NotionAgent)
                calls = []
                def respond(request):
                    calls.append(request)
                    if request.url.host == 'app.notion.com':
                        return httpx.Response(403, text=HTML, headers={'content-type': 'text/html'})
                    self.assertEqual(request.url.host, 'www.notion.so')
                    self.assertEqual(request.headers['origin'], 'https://www.notion.so')
                    return httpx.Response(200, json={'ok': True})
                up.client = httpx.AsyncClient(base_url='https://app.notion.com',
                                             transport=httpx.MockTransport(respond))
                body = {'threadId': 'same-thread', 'clientEventId': 'same-event', 'spaceId': 'same-space'}
                self.assertEqual(await up.post(endpoint, body), {'ok': True})
                self.assertEqual(calls[0].content, calls[1].content)
                self.assertEqual(up.api_origin, 'https://www.notion.so')
                await up.post(endpoint, body)
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[-1].url.host, 'www.notion.so')
                await up.close()

    async def test_real_notion_json_auth_errors_remain_auth_errors(self):
        for status in (401, 403):
            up = object.__new__(NotionAgent)
            up.client = httpx.AsyncClient(base_url='https://app.notion.com', transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={'name': 'UnauthorizedError'},
                                               headers={'server': 'cloudflare'})))
            with self.assertRaises(UpstreamError) as ctx:
                await up.post('getThreadTranscript', {})
            self.assertEqual(ctx.exception.status, status)
            self.assertFalse(ctx.exception.exhausted)
            await up.close()

    async def test_redirect_does_not_forward_credentials_to_another_origin(self):
        up = object.__new__(NotionAgent)
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'https://untrusted.invalid/'})
        up.client = httpx.AsyncClient(base_url='https://app.notion.com',
            cookies={'token_v2': 'test-private-cookie'}, transport=httpx.MockTransport(respond),
            follow_redirects=False)
        with self.assertRaises(UpstreamError):
            await up.post('createAgentThread', {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, 'app.notion.com')
        await up.close()

    async def test_edge_block_never_fails_over_and_retains_thread_for_retry(self):
        from app.agent_gateway.upstream import edge_error
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / 'state.db')
            a, b = ScopedNotion('original'), ScopedNotion('backup')
            pool = WorkspacePool([('original', a), ('backup', b)])
            history = [{'role': 'user', 'content': 'Remember this session.'}]
            _, first = await pool.run(store, 'desktop', prompt(history), 'medium', json.loads)
            start = a.start_turn
            a.start_turn = AsyncMock(side_effect=edge_error('sendEventToAgentThread'))
            history += [{'role': 'assistant', 'content': 'OK'}, {'role': 'user', 'content': 'Continue'}]
            with self.assertRaises(UpstreamError):
                await pool.run(store, 'desktop', prompt(history), 'medium', json.loads)
            self.assertEqual(store.get('desktop')['thread_id'], first['thread_id'])
            self.assertNotIn('pending', store.get('desktop'))
            self.assertEqual(b.writes, [])
            self.assertEqual(pool.cooling, {})
            a.start_turn = start
            _, final = await pool.run(store, 'desktop', prompt(history), 'medium', json.loads)
            self.assertEqual(final['thread_id'], first['thread_id'])
            self.assertEqual(final['profile'], 'original')
            store.close()

    async def test_edge_block_during_reconciliation_cannot_discard_an_uncertain_write(self):
        from app.agent_gateway.upstream import edge_error
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / 'state.db')
            up = ScopedNotion('original')
            history = [{'role': 'user', 'content': 'Original request'}]
            _, first = await store.run(up, 'desktop', prompt(history), 'medium', json.loads)
            history += [{'role': 'assistant', 'content': 'OK'}, {'role': 'user', 'content': 'Next request'}]
            up.start_turn = AsyncMock(side_effect=UpstreamError('Unknown write outcome', 502))
            up.turn_is_absent = AsyncMock(side_effect=edge_error('getThreadTranscript'))
            with self.assertRaises(UpstreamError) as ctx:
                await store.run(up, 'desktop', prompt(history), 'medium', json.loads)
            self.assertEqual(ctx.exception.status, 503)
            self.assertFalse(ctx.exception.rejected)
            record = store.get('desktop')
            self.assertEqual(record['thread_id'], first['thread_id'])
            self.assertIn('pending', record)
            self.assertFalse(record['pending']['dispatch_accepted'])
            up.start_turn.assert_awaited_once()
            store.close()
