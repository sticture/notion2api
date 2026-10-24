"""Follow proxy changes without interrupting concurrent Notion requests."""
from __future__ import annotations

import sys
import urllib.request

import httpx


PROXY_SCHEMES = ("http", "https", "all")


def proxy_configuration():
    environment = urllib.request.getproxies_environment()
    settings = urllib.request.getproxies()
    extra_mounts = {}
    # A NO_PROXY-only environment hides macOS system proxies in getproxies().
    # Add the missing scheme mounts; HTTPX still supplies the more-specific
    # NO_PROXY bypass mounts and handles explicit proxy environment variables.
    if (sys.platform == "darwin" and not any(environment.get(s) for s in PROXY_SCHEMES)
            and "*" not in [host.strip() for host in environment.get("no", "").split(",")]):
        system = getattr(urllib.request, "getproxies_macosx_sysconf", lambda: {})()
        settings = {**system, **environment}
        if environment.get("no"):
            extra_mounts = {scheme + "://": system[scheme] for scheme in PROXY_SCHEMES
                            if system.get(scheme)}
    signature = tuple((key, settings.get(key)) for key in (*PROXY_SCHEMES, "no"))
    return signature, extra_mounts


class NotionTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.clients = {}

    async def handle_async_request(self, request):
        signature, extra_mounts = proxy_configuration()
        client = self.clients.get(signature)
        if client is None:
            client = httpx.AsyncClient(follow_redirects=False, mounts={
                pattern: httpx.AsyncHTTPTransport(proxy=proxy)
                for pattern, proxy in extra_mounts.items()
            })
            self.clients[signature] = client
        # Never retry here: the session store owns ambiguous-write recovery.
        # Keep earlier pools until shutdown so a proxy change cannot close
        # another session's in-flight response. API and storage callers each
        # own a transport instance and supply their own authenticated request.
        return await client.send(request, stream=True)

    async def aclose(self):
        for client in self.clients.values():
            await client.aclose()
        self.clients.clear()
