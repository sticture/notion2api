#!/usr/bin/env python3
"""Install normal model-picker routing, with backups and no login autostart."""
import datetime
import json
import os
import shutil
from pathlib import Path

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
PICKER = "notion-claude-opus-5-5"


def private_write(path, text):
    path.write_text(text)
    path.chmod(0o600)


def main():
    claude_path = Path.home() / ".claude/settings.json"
    codex_path = Path.home() / ".codex/config.toml"
    claude = json.loads(claude_path.read_text())
    codex = tomlkit.parse(codex_path.read_text())
    backup = LOCAL / "integration-backups" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, mode=0o700)
    private_write(backup / "claude-settings.json", claude_path.read_text())
    private_write(backup / "codex-config.toml", codex_path.read_text())
    routes_path = LOCAL / "routes.json"
    routes = json.loads(routes_path.read_text()) if routes_path.exists() else {}
    env = claude.setdefault("env", {})
    if env.get("ANTHROPIC_BASE_URL") != "http://127.0.0.1:8765/claude":
        if not env.get("ANTHROPIC_AUTH_TOKEN") or not env.get("ANTHROPIC_BASE_URL", "").startswith("https://"):
            raise SystemExit("An existing HTTPS Claude gateway and auth token are required for preserved routing")
        routes["claude"] = {"base_url": env["ANTHROPIC_BASE_URL"], "auth_token": env["ANTHROPIC_AUTH_TOKEN"]}
    current_provider = codex.get("model_provider", "openai")
    if current_provider != "notion_router":
        previous = codex["model_providers"][current_provider]
        if not previous.get("requires_openai_auth"):
            raise SystemExit("This installer expects the existing ChatGPT-authenticated Codex provider")
        routes["codex"] = {"base_url": previous["base_url"]}
    routes["last_backup_dir"] = str(backup)
    private_write(routes_path, json.dumps(routes))
    key = (LOCAL / "gateway-key").read_text().strip()
    env.update({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8765/claude", "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_CUSTOM_MODEL_OPTION": PICKER, "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Notion Opus 5.5",
                "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Local coding through your Notion AI account"})
    codex["model_provider"] = "notion_router"
    codex.setdefault("model_providers", {})["notion_router"] = {
        "name": "ChatGPT + Notion", "base_url": "http://127.0.0.1:8765/codex", "wire_api": "responses",
        "requires_openai_auth": True, "supports_websockets": False,
        "model_catalog_url": "http://127.0.0.1:8765/codex/models",
        "http_headers": {"x-notion-gateway-key": key},
    }
    # Do not replace an existing explicit catalog silently.
    if codex.get("model_catalog_json") and codex["model_catalog_json"] != str(LOCAL / "codex-models.json"):
        raise SystemExit("Existing explicit Codex catalog needs a deliberate merge before installation")
    command = Path.home() / ".local/bin/notion-proxy"
    command.parent.mkdir(parents=True, exist_ok=True)
    if command.exists() or command.is_symlink():
        if not command.is_symlink() or command.resolve() != ROOT / "scripts/notion_proxy.py":
            raise SystemExit("notion-proxy command already exists and is not this project")
    else:
        command.symlink_to(ROOT / "scripts/notion_proxy.py")
    (ROOT / "scripts/notion_proxy.py").chmod(0o755)
    private_write(claude_path, json.dumps(claude, ensure_ascii=False, indent=2) + "\n")
    private_write(codex_path, tomlkit.dumps(codex))
    private_write(LOCAL / "integration-installed.json", json.dumps({"backup": str(backup), "command": str(command)}))
    print("Installed Notion Opus 5.5 in Claude Code and Codex configuration.")
    print("Start manually with: notion-proxy")
    print("Backup:", backup)
    print("No login autostart was installed.")


if __name__ == "__main__":
    main()
