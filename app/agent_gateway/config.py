"""Local paths shared by the server and first-run tools."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
CATALOG = Path(__file__).with_name("model_catalog.json")


def profile_path(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ValueError("Profile must contain 1-64 letters, digits, underscores or hyphens")
    return LOCAL if name == "default" else LOCAL / "profiles" / name


PROFILE = os.getenv("NOTION_GATEWAY_PROFILE", "default")
PROFILE_LOCAL = profile_path(PROFILE)


def venv_python():
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
