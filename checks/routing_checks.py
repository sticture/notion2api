import asyncio
import json
import types
import unittest

import httpx
from starlette.requests import Request

from app.agent_gateway.routing import passthrough


class RoutingChecks(unittest.TestCase):
    def check_route(self, provider):
        async def run():
            observed = []

            class Chunks(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'event: original\n'
                    yield b'data: unchanged\n\n'

            async def handler(req):
                observed.append(req)
                return httpx.Response(200, stream=Chunks(), headers={"content-type": "text/event-stream"})

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            app = types.SimpleNamespace(state=types.SimpleNamespace(proxy_http=client, routes={
                "claude": {"base_url": "https://claude.example/api", "auth_token": "old-claude-key"},
                "codex": {"base_url": "https://codex.example/backend"}}))
            req = Request({"type": "http", "method": "POST", "path": "/test", "query_string": b"beta=true",
                           "headers": [(b"authorization", b"Bearer client-oauth"), (b"x-api-key", b"local-key"),
                                       (b"x-notion-gateway-key", b"local-routing-key"), (b"cookie", b"do-not-forward"),
                                       (b"host", b"localhost")], "app": app})
            response = await passthrough(req, b'{"model":"original"}', provider, "responses" if provider == "codex" else "v1/messages")
            result = b"".join([chunk async for chunk in response.body_iterator])
            self.assertEqual(result, b'event: original\ndata: unchanged\n\n')
            sent = observed[0]
            self.assertEqual(sent.content, b'{"model":"original"}')
            self.assertNotIn("x-notion-gateway-key", sent.headers)
            self.assertNotIn("cookie", sent.headers)
            self.assertEqual(sent.headers["authorization"], "Bearer old-claude-key" if provider == "claude" else "Bearer client-oauth")
            if provider == "claude":
                self.assertNotIn("x-api-key", sent.headers)
            await client.aclose()
        asyncio.run(run())

    def test_claude_uses_only_original_provider_credentials(self):
        self.check_route("claude")

    def test_codex_preserves_oauth_and_stream(self):
        self.check_route("codex")


if __name__ == "__main__":
    unittest.main()
