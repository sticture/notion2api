#!/usr/bin/env python3
"""Provision a personal Notion profile and optional manual launcher commands."""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent_gateway.config import ROOT, LOCAL, profile_path
from app.agent_gateway.provision import parse_curl, private_write, ensure_key, install_commands
from app.agent_gateway.upstream import NotionAgent, UpstreamError


async def verify(path):
    agent = NotionAgent(path)
    try:
        return await agent.verify_model()
    finally:
        await agent.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-curl", type=Path, help="File containing one Chrome Copy as cURL (bash) getAvailableModels request")
    parser.add_argument("--profile", default="default", help="Named workspace/account profile (default: default)")
    parser.add_argument("--check", action="store_true", help="Verify stored login and Opus 5.5 access without creating a chat")
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--install-commands", action="store_true", help="Install manual launchers; no global client setting changes or autostart")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin", help="Launcher directory (default: ~/.local/bin)")
    args = parser.parse_args()
    try:
        folder = profile_path(args.profile)
        if args.list_profiles:
            if (LOCAL / "notion-session.json").exists():
                print("default")
            for path in sorted((LOCAL / "profiles").glob("*/notion-session.json")):
                print(path.parent.name)
            return
        if args.install_commands and os.name == "nt":
            raise ValueError("On Windows run the Python launchers directly; command installation is macOS/Linux only")
        session = folder / "notion-session.json"
        if args.from_curl:
            config = parse_curl(args.from_curl.read_text(encoding="utf-8"))
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".verify-")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(config, f)
                model = asyncio.run(verify(Path(tmp)))
            finally:
                os.unlink(tmp)
            private_write(session, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
            ensure_key(LOCAL)
            print(f"Saved profile {args.profile}; verified {model['name']} for the captured workspace.")
        elif args.check or args.install_commands:
            if not session.exists():
                raise ValueError("Profile is missing; import your own logged-in request with --from-curl first")
            model = asyncio.run(verify(session))
            ensure_key(LOCAL)
            print(f"Profile {args.profile}: {model['name']} is available.")
        else:
            parser.print_help()
            return
        if args.install_commands:
            install_commands(ROOT, args.bin_dir.expanduser().resolve())
            print(f"Installed: notion-proxy, claude-notion, codex-notion in {args.bin_dir}. Add that directory to PATH.")
        print("Start manually: notion-proxy" + (" --profile " + args.profile if args.profile != "default" else ""))
        print("No login autostart installed. Existing Claude/Codex settings were not modified.")
    except (ValueError, UpstreamError, OSError, TimeoutError) as exc:
        parser.exit(1, "Setup failed: " + (str(exc) if not isinstance(exc, OSError) else type(exc).__name__) + "\n")


if __name__ == "__main__":
    main()
