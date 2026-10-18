"""Fixed-destination pass-through for the user's existing providers."""
import json

import httpx
from fastapi.responses import Response, StreamingResponse

HOP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "keep-alive",
               "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade",
               "x-notion-gateway-key"}


async def passthrough(request, body, provider, path):
    route = request.app.state.routes.get(provider)
    if not route:
        return Response("Original provider route is not configured", status_code=503)
    # Destinations come from the preserved local configuration, never request input.
    url = route["base_url"].rstrip("/") + "/" + path.lstrip("/")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    headers.pop("cookie", None)
    if provider == "claude":
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
        headers["authorization"] = "Bearer " + route["auth_token"]
    elif request.headers.get("x-notion-gateway-key") is None:
        return Response("Codex routing key is required", status_code=401)
    headers["accept-encoding"] = "identity"
    client = request.app.state.proxy_http
    outgoing = client.build_request(request.method, url, content=body, headers=headers,
                                    params=list(request.query_params.multi_items()))
    try:
        response = await client.send(outgoing, stream=True)
    except httpx.HTTPError:
        return Response("Existing provider is unreachable", status_code=502)
    response_headers = {k: v for k, v in response.headers.items() if k.lower() not in HOP_HEADERS}

    async def stream():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(stream(), status_code=response.status_code, headers=response_headers)
