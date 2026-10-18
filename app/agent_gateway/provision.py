"""Import a browser-copied Notion request as data, never as shell code."""
import json
import os
import secrets
import shlex
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .upstream import SESSION_HEADERS


def parse_curl(raw):
    words = shlex.split(raw.replace("\\\r\n", "").replace("\\\n", ""))
    if not words or Path(words.pop(0)).name != "curl":
        raise ValueError("Expected Chrome Copy as cURL (bash) text; the command is parsed, not executed")
    headers, cookies, urls, data = {}, "", [], None
    flags = {"--compressed", "--globoff", "--http2", "--silent"}
    pairs = {"-H", "--header", "-b", "--cookie", "--data-raw", "--data-binary", "--data", "-d",
             "-X", "--request", "--url", "-A", "--user-agent"}
    i = 0
    while i < len(words):
        word = words[i]
        if word in flags:
            i += 1
            continue
        if word in pairs:
            if i + 1 >= len(words):
                raise ValueError("Incomplete cURL option")
            value = words[i + 1]
            i += 2
            if word in {"-H", "--header"}:
                name, sep, content = value.partition(":")
                if not sep:
                    raise ValueError("Invalid request header")
                headers[name.lower().strip()] = content.strip()
            elif word in {"-b", "--cookie"}:
                cookies = value
            elif word in {"-d", "--data", "--data-raw", "--data-binary"}:
                data = value
            elif word == "--url":
                urls.append(value)
            elif word in {"-A", "--user-agent"}:
                headers["user-agent"] = value
            elif value.upper() != "POST":
                raise ValueError("Capture a POST getAvailableModels request")
        elif word.startswith("https://"):
            urls.append(word)
            i += 1
        else:
            raise ValueError("Unsupported cURL syntax; use Chrome Copy as cURL (bash), not PowerShell or cmd")
    if len(urls) != 1:
        raise ValueError("Import exactly one Notion request")
    url = urlsplit(urls[0])
    if (url.scheme != "https" or url.netloc != "app.notion.com"
            or url.path != "/api/v3/getAvailableModels"):
        raise ValueError("Capture https://app.notion.com/api/v3/getAvailableModels in the intended workspace")
    try:
        body = json.loads(data or "{}")
        space = str(uuid.UUID(body.get("spaceId") or headers.get("x-notion-space-id", "")))
        user = str(uuid.UUID(headers.get("x-notion-active-user-header", "")))
        if headers.get("x-notion-space-id") and str(uuid.UUID(headers["x-notion-space-id"])) != space:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Request must identify the current Notion user and one matching workspace") from None
    jar = {}
    for part in (cookies or headers.get("cookie", "")).split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            jar[name] = value
    if not jar.get("token_v2"):
        raise ValueError("The captured request is missing token_v2; copy the logged-in request including cookies")
    return {"origin": "https://app.notion.com", "space_id": space, "user_id": user,
            "cookies": [{"name": k, "value": v} for k, v in jar.items()],
            "headers": {k: v for k, v in headers.items() if k in SESSION_HEADERS}}


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gateway-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_key(local):
    path = local / "gateway-key"
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if len(path.read_text().strip()) < 24:
            raise ValueError("Existing gateway-key is invalid")
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))


def install_commands(root, bin_dir):
    targets = {"notion-proxy": "notion_proxy.py", "claude-notion": "claude_notion.py", "codex-notion": "codex_notion.py"}
    for name, script in targets.items():
        dst = bin_dir / name
        if dst.exists() or dst.is_symlink():
            if not dst.is_symlink() or dst.resolve() != root / "scripts" / script:
                raise ValueError(f"Command {name} already belongs to another installation")
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, script in targets.items():
        target = root / "scripts" / script
        target.chmod(0o755)
        dst = bin_dir / name
        if not dst.is_symlink():
            dst.symlink_to(target)
