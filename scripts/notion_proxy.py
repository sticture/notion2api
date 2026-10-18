#!/usr/bin/env python3
"""One manual command to start the gateway; never installs login autostart."""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent_gateway.config import profile_path, venv_python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    try:
        folder = profile_path(args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if not (folder / 'notion-session.json').exists():
        parser.error('Profile is not configured. Run scripts/setup_gateway.py --help first.')
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/health', timeout=2) as response:
            health = json.load(response)
        if health.get('tool_mode') != 'text-json-bridge':
            parser.error('This port is occupied by another service')
        if health.get('profile', 'default') != args.profile:
            parser.error('Another profile is running on this port. Stop it with Ctrl+C before switching profiles.')
        print(f'Notion proxy is already running at http://127.0.0.1:{args.port} (profile: {args.profile}).')
        return
    except (OSError, ValueError):
        pass
    python = venv_python()
    if not python.exists():
        parser.error('Missing .venv; create it and install requirements.txt first')
    os.environ['NOTION_GATEWAY_PROFILE'] = args.profile
    print(f'Starting Notion proxy at http://127.0.0.1:{args.port} (profile: {args.profile}). Ctrl+C stops it.', flush=True)
    os.chdir(ROOT)
    os.execv(str(python), [str(python), str(ROOT / 'scripts/local_gateway.py'), '--port', str(args.port)])


if __name__ == '__main__':
    main()
