import json
import shlex
import tempfile
import unittest
from pathlib import Path

from app.agent_gateway.config import profile_path
from app.agent_gateway.provision import parse_curl, private_write, ensure_key, install_commands
from app.agent_gateway.sessions import SessionStore


SPACE = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"


def copied_request(url="https://app.notion.com/api/v3/getAvailableModels", space=SPACE):
    return shlex.join(["curl", url, "-H", "x-notion-active-user-header: " + USER,
                       "-H", "x-notion-space-id: " + SPACE, "-H", "notion-client-version: test-version",
                       "-H", "authorization: must-not-be-stored", "-b", "token_v2=synthetic; device_id=test",
                       "--data-raw", json.dumps({"spaceId": space}), "--compressed"])


class SetupChecks(unittest.TestCase):
    def test_import_filters_headers_and_preserves_matching_user_workspace(self):
        data = parse_curl(copied_request())
        self.assertEqual(data["space_id"], SPACE)
        self.assertEqual(data["user_id"], USER)
        self.assertEqual(data["headers"], {"notion-client-version": "test-version"})
        self.assertEqual(data["cookies"][0], {"name": "token_v2", "value": "synthetic"})

    def test_rejects_wrong_destination_workspace_and_shell_commands(self):
        for raw in [copied_request("https://evil.example/api/v3/getAvailableModels"),
                    copied_request("https://app.notion.com.evil.example/api/v3/getAvailableModels"),
                    copied_request(space=USER), copied_request() + "; touch /tmp/notion-should-not-execute",
                    copied_request().replace("token_v2=synthetic", "other=synthetic")]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_curl(raw)

    def test_opaque_cookie_text_is_never_executed(self):
        data = parse_curl(copied_request().replace("token_v2=synthetic", "token_v2=$(not-a-command)"))
        self.assertEqual(data["cookies"][0]["value"], "$(not-a-command)")

    def test_profile_names_cannot_escape_local_directory(self):
        for name in ["../other", "/tmp/other", "", "a/b", "a\\b"]:
            with self.assertRaises(ValueError):
                profile_path(name)

    def test_secret_files_are_private_and_key_is_not_rotated_on_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            ensure_key(folder)
            key = (folder / "gateway-key").read_text()
            ensure_key(folder)
            self.assertEqual(key, (folder / "gateway-key").read_text())
            self.assertEqual((folder / "gateway-key").stat().st_mode & 0o777, 0o600)
            private_write(folder / "profile.json", "private")
            self.assertEqual((folder / "profile.json").stat().st_mode & 0o777, 0o600)

    def test_accounts_workspaces_isolated_and_legacy_binding_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.db"
            old = SessionStore(path)
            old.save("same-client", {"thread_id": "legacy-thread"})
            old.close()
            a = SessionStore(path, namespace="account-a-workspace-a", adopt_legacy=True)
            self.assertEqual(a.get("same-client")["thread_id"], "legacy-thread")
            a.close()
            b = SessionStore(path, namespace="account-a-workspace-b", adopt_legacy=True)
            self.assertEqual(b.get("same-client"), {})
            b.save("same-client", {"thread_id": "different-thread"})
            b.close()
            a = SessionStore(path, namespace="account-a-workspace-a", adopt_legacy=True)
            self.assertEqual(a.get("same-client")["thread_id"], "legacy-thread")
            a.close()

    def test_command_collision_does_not_overwrite_existing_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "codex-notion").write_text("existing")
            with self.assertRaises(ValueError):
                install_commands(Path("/nonexistent"), folder)
            self.assertEqual((folder / "codex-notion").read_text(), "existing")
            self.assertFalse((folder / "notion-proxy").exists())
