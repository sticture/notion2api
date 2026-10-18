#!/usr/bin/env python3
"""Launch Claude Code with process-local gateway settings; leave global settings unchanged."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
key_path = root / ".local/gateway-key"
if not key_path.exists():
    raise SystemExit("Start scripts/local_gateway.py first")
env = os.environ.copy()
for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "CLAUDE_CODE_USE_MANTLE"):
    env.pop(name, None)
try:
    port = int(env.get("NOTION_PROXY_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError()
except ValueError:
    raise SystemExit("NOTION_PROXY_PORT must be between 1 and 65535")
env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_AUTH_TOKEN": key_path.read_text().strip(),
            "ANTHROPIC_MODEL": "claude-opus-5-5", "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-5-5", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-5-5",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
os.execvpe("claude", ["claude", "--model", "claude-opus-5-5", *sys.argv[1:]], env)
