"""Proxy activation after startup must recover without replaying requests."""
import json
import os
import unittest
import urllib.request
from contextlib import ExitStack
from unittest.mock import patch

import httpx

from app.agent_gateway import network


class RecordingTransport(httpx.AsyncBaseTransport):
    instances = []
    failure = None

    def __init__(self, *args, proxy=None, **kwargs):
        self.proxy = str(proxy.url) if isinstance(proxy, httpx.Proxy) else proxy
        self.requests = []
        self.closed = False
        self.instances.append(self)

    async def handle_async_request(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return httpx.Response(200, json={"proxy": self.proxy}, request=request)

    async def aclose(self):
        self.closed = True


class NetworkChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.system = {}
        RecordingTransport.instances = []
        RecordingTransport.failure = None
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(network.sys, "platform", "darwin"))
        self.stack.enter_context(patch.object(urllib.request, "getproxies_macosx_sysconf",
            side_effect=lambda: dict(self.system), create=True))
        discovered = lambda: urllib.request.getproxies_environment() or dict(self.system)
        self.stack.enter_context(patch.object(urllib.request, "getproxies", side_effect=discovered))
        self.stack.enter_context(patch("httpx._utils.getproxies", side_effect=discovered))
        self.stack.enter_context(patch("httpx._client.AsyncHTTPTransport", RecordingTransport))
        self.stack.enter_context(patch.object(network.httpx, "AsyncHTTPTransport", RecordingTransport))

    def client(self, **kwargs):
        client = httpx.AsyncClient(transport=network.NotionTransport(), **kwargs)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_running_client_follows_system_proxy_start_change_and_stop(self):
        client = self.client()
        url = "https://app.notion.com/api/v3/getThreadTranscript"
        self.assertIsNone((await client.post(url, json={})).json()["proxy"])
        original = RecordingTransport.instances[0]
        for port in (7897, 7898):
            proxy = f"http://127.0.0.1:{port}"
            self.system["https"] = proxy
            self.assertEqual((await client.post(url, json={})).json()["proxy"], proxy)
            self.assertFalse(original.closed)
        self.system.clear()
        self.assertIsNone((await client.post(url, json={})).json()["proxy"])
        self.assertEqual(len(original.requests), 2)
        await client.aclose()
        self.assertTrue(all(t.closed for t in RecordingTransport.instances))

    async def test_no_proxy_only_retains_system_proxy_and_httpx_bypass_semantics(self):
        self.system.update(http="http://127.0.0.1:7897", https="http://127.0.0.1:7897")
        os.environ["NO_PROXY"] = "localhost,127.0.0.1,.example.com,https://bypass.test"
        client = self.client()
        for url in ("https://app.notion.com/api/v3/getAvailableModels", "http://public.test"):
            self.assertEqual((await client.get(url)).json()["proxy"], self.system["https"])
        for url in ("http://localhost:8765", "https://127.0.0.1", "https://sub.example.com",
                    "https://bypass.test"):
            with self.subTest(url=url):
                self.assertIsNone((await client.get(url)).json()["proxy"])

    async def test_explicit_proxy_environment_overrides_system_and_preserves_bypass(self):
        self.system["https"] = "http://127.0.0.1:7897"
        for variable in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            with self.subTest(variable=variable), patch.dict(os.environ,
                    {variable: "http://explicit.test:8888", "NO_PROXY": "app.notion.com"}, clear=True):
                client = self.client()
                self.assertEqual((await client.get("https://www.notion.so")).json()["proxy"],
                                 "http://explicit.test:8888")
                self.assertIsNone((await client.get("https://app.notion.com")).json()["proxy"])

    async def test_no_proxy_wildcard_disables_system_proxy(self):
        self.system["https"] = "http://127.0.0.1:7897"
        for bypass in ("*", "localhost, * "):
            with self.subTest(bypass=bypass):
                os.environ["NO_PROXY"] = bypass
                self.assertIsNone((await self.client().get("https://app.notion.com")).json()["proxy"])

    async def test_non_macos_keeps_standard_environment_routing(self):
        self.system["https"] = "http://system.test:7897"
        os.environ["NO_PROXY"] = "localhost"
        with patch.object(network.sys, "platform", "linux"):
            self.assertIsNone((await self.client().get("https://app.notion.com")).json()["proxy"])

    async def test_http_only_environment_does_not_inject_system_https_proxy(self):
        self.system["https"] = "http://127.0.0.1:7897"
        os.environ["HTTP_PROXY"] = "http://explicit.test:8888"
        self.assertIsNone((await self.client().get("https://app.notion.com")).json()["proxy"])

    async def test_transport_does_not_replay_failed_generation(self):
        RecordingTransport.failure = httpx.ReadTimeout("response lost")
        client = self.client()
        with self.assertRaises(httpx.ReadTimeout):
            await client.post("https://app.notion.com/api/v3/createAgentThread", json={"event": "same"})
        self.assertEqual(sum(len(t.requests) for t in RecordingTransport.instances), 1)

    async def test_request_body_credentials_and_deadline_survive_routing_change(self):
        client = self.client(cookies={"token_v2": "test-private"}, timeout=httpx.Timeout(40, connect=15))
        for proxy in (None, "http://127.0.0.1:7897"):
            self.system.update({"https": proxy} if proxy else {})
            await client.post("https://app.notion.com/api/v3/sendEventToAgentThread",
                              headers={"x-notion-space-id": "same-space"}, json={"clientEventId": "same-event"})
        sent = [r for t in RecordingTransport.instances for r in t.requests]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].content, sent[1].content)
        for request in sent:
            self.assertEqual(json.loads(request.content)["clientEventId"], "same-event")
            self.assertEqual(request.headers["cookie"], "token_v2=test-private")
            self.assertEqual(request.headers["x-notion-space-id"], "same-space")
            self.assertEqual(request.extensions["timeout"]["connect"], 15)
        storage = self.client()
        await storage.put("https://storage.test/upload", content=b"image")
        upload = next(t.requests[-1] for t in reversed(RecordingTransport.instances) if t.requests)
        self.assertNotIn("cookie", upload.headers)
        self.assertNotIn("x-notion-space-id", upload.headers)
