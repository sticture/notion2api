#!/usr/bin/env python3
"""Start stock Codex with this process's Notion provider, preserving global defaults."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def launch_args(port):
    config = {
        "model_provider": "notion_local",
        "model_catalog_json": str(ROOT / "app/agent_gateway/model_catalog.json"),
        "model_providers.notion_local.name": "Notion",
        "model_providers.notion_local.base_url": f"http://127.0.0.1:{port}/v1",
        "model_providers.notion_local.wire_api": "responses",
        "model_providers.notion_local.env_key": "NOTION_LOCAL_API_KEY",
        "model_providers.notion_local.requires_openai_auth": False,
        "model_providers.notion_local.supports_websockets": False,
        "model_reasoning_effort": "medium",
        "model_context_window": 32000,
        "model_auto_compact_token_limit": 24000,
    }
    args = ["codex"]
    for key, value in config.items():
        args += ["-c", key + "=" + json.dumps(value)]
    return args + ["--model", "notion-claude-opus-5-5"]


def main():
    key = ROOT / ".local/gateway-key"
    if not key.exists():
        raise SystemExit("Run scripts/setup_gateway.py first")
    try:
        port = int(os.environ.get("NOTION_PROXY_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise SystemExit("NOTION_PROXY_PORT must be between 1 and 65535")
    env = os.environ.copy()
    env["NOTION_LOCAL_API_KEY"] = key.read_text().strip()
    os.execvpe("codex", launch_args(port) + sys.argv[1:], env)


if __name__ == "__main__":
    main()
