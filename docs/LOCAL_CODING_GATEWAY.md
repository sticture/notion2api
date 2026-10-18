# Local Opus 5.5 coding gateway (experimental)

This server provides Anthropic Messages and OpenAI Responses endpoints. It uses the current Notion Agent endpoints, rather than changing the original Chat Completions server. The verified workspace mapping is `albuquerque-quinn` → **Opus 5.5**.

## Everyday use (already installed on this Mac)

From any directory, run one command:

```sh
notion-proxy
```

Keep that terminal open. Then open Claude Code or Codex normally and select **Notion Opus 5.5**. Claude Code exposes it in `/model`; Codex exposes it in its model picker. The actual model ID is `notion-claude-opus-5-5`. Restart clients opened before installation to reload their configuration. No login autostart or LaunchAgent is installed.

The one running proxy is shared by all projects. Existing Claude models route to the preserved DuckCode gateway; existing Codex models route to the preserved ChatGPT backend with the client's own OAuth handling. These routes also require the local proxy to be running after integration. Stop a foreground proxy with Ctrl+C. If the command reports that it is already running, use the existing instance.

Global configuration was backed up under `.local/integration-backups/`. Only Claude's gateway/custom-model environment settings and Codex's selected provider plus the new provider block were changed. Existing default model, permissions, original provider blocks, skills, and plugin configuration were preserved.

Each Claude Code or Codex conversation now keeps one Notion thread, including local tool calls and resumed conversations. Restarting `notion-proxy` preserves the binding. Starting a new client conversation creates a separate binding. Conversations from the old stateless gateway establish their binding once on the first request after this upgrade, importing the supplied client history; subsequent requests reuse it. Existing Notion chat records are left intact.

## Development / fresh installation

Requires Python 3.11 or newer and an installed Claude Code CLI.

```sh
cd /Users/ticture/Documents/project/notion2api
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/local_gateway.py
```

The gateway binds to `127.0.0.1:8765`. It creates `.local/gateway-key` with mode 0600. Do not put a Notion Cookie in a Claude Code configuration: only the gateway holds the Notion session.

From the project you want to work on, run:

```sh
/Users/ticture/Documents/project/notion2api/.venv/bin/python \
  /Users/ticture/Documents/project/notion2api/scripts/claude_notion.py \
  --safe-mode --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --tools Read,Edit,Write,Bash,Glob,Grep
```

The older `claude_notion.py` launcher remains available for isolated diagnostics. Everyday use no longer needs it. Normal local tool permission checks remain active. Supplied code, prompts, and tool results go to Notion, and Notion creates chat records. After provisioning the local session and model catalog, `scripts/install_model_picker.py` installs the global picker integration and the manual command.

## Session file

This installation was provisioned from the user's authorized, logged-in Notion browser session. `.local/notion-session.json` is ignored by Git and stored with mode 0600. It contains:

```json
{
  "origin": "https://app.notion.com",
  "space_id": "YOUR_WORKSPACE_ID",
  "user_id": "YOUR_USER_ID",
  "cookies": [{"name": "token_v2", "value": "YOUR_OWN_SESSION_COOKIE"}],
  "headers": {
    "notion-client-version": "CAPTURE_CURRENT_VERSION",
    "notion-audit-log-platform": "web",
    "x-notion-cell-hint": "CAPTURE_YOUR_WORKSPACE_ROUTE",
    "x-notion-cell-hint-source": "space-id-mapping"
  }
}
```

Use only your own session. Current deployments can also require session-binding and device cookies. Preserve the appropriate Notion cookies from the authorized session; do not copy unrelated site credentials. The domain is fixed to `https://app.notion.com` in code. On expiration, refresh the session file locally and restart. Never paste its contents into chat, issue reports, or commits. Deleting `.local/notion-session.json` removes this application's local copy; session revocation is managed in Notion.

## Implementation and limitations

- `app/agent_gateway/upstream.py` authenticates to Notion, creates a personal-agent thread, and reads ordered transcript patches with a forward cursor. It waits for `turn_completed` with `end_turn`; an idle session alone is not success.
- `app/agent_gateway/sessions.py` binds Claude's `x-claude-code-session-id` (with `metadata.user_id` fallback) and Codex's `client_metadata.thread_id` to persistent Notion threads. Claude agent IDs and request classes, when supplied, isolate auxiliary conversations. Other API clients can send `x-notion-session-id`; clients without an identity remain stateless, rather than guessing from prompt similarity.
- The first request creates the thread; later requests use `sendEventToAgentThread`. Hashes of normalized message blocks identify the unchanged prefix, so only new messages, acknowledged assistant outputs with their local tool IDs, and changed settings are sent. Edited/compacted histories trigger an explicit authoritative-history resync in the same thread. Notion still retains its older transcript; this is a context instruction, not physical deletion of old messages.
- Bindings, transcript cursors, history hashes, and the last validated answer are stored in private `.local/gateway-sessions.sqlite3` (0600). Pending raw answers may be retained until validation succeeds. This file contains response/tool data and must remain private. Keep it across restarts; deleting it loses bindings. The reasoning effort selected when the Notion thread was created remains in effect for that thread.
- Retrying an identical request reuses its validated answer and tool IDs. If a write's outcome is unknown or polling is interrupted, retry resumes polling that same turn instead of submitting it again. If the client sends a different request while a prior turn is unresolved (including changed context after idle/disconnect), the gateway binds to a fresh Notion thread and imports the full current client history. It neither appends to the uncertain thread nor returns its stale output/tool calls. The old remote turn is not cancelled and its transcript remains in Notion; only history supplied by the client is restored. A definitively rejected recovery restores the original binding. Completion polling excludes the previous turn so stale transcript events cannot masquerade as a new answer.
- New threads disable browser access, suggested edits, and use of memories; they request exclusion from memories. These flags were verified in live calls. They do not turn Notion Agent into a raw Anthropic API, or guarantee every internal Notion tool is disabled. Instructions tell it to use the supplied local-tool protocol instead.
- Tools are emulated through constrained JSON text, **not native Notion client tool calling**. Every output tool must exist in the client request and pass its JSON schema. The proxy never executes shell commands or modifies project files; Claude Code does that using its own tools and permissions.
- The entire JSON answer is buffered and validated before any executable tool block is emitted. SSE starts immediately and sends `ping` events while waiting. The final block sequence is Anthropic-compatible, but text is not streamed token by token.
- Thinking blocks are not relayed and no thinking signatures are invented. Text, `tool_use`, and `tool_result` are supported. Images, server tools, native structured output and custom stop sequences are rejected.
- Token usage is an explicit character-based estimate (`x-notion-usage-estimated: true`), not Notion billing data; cache usage is not claimed. `max_tokens` is passed as a textual output budget rather than a native Notion decoding limit. The token counting endpoint is intentionally absent, so Claude Code uses its own fallback.
- Inputs are bounded to 180,000 characters, one active inference at a time, and a 180-second upstream deadline. Large repository tasks may need compaction. This is a text-only prototype, not full Claude API compatibility.
- Browser-origin requests, non-loopback hosts and unauthenticated API requests are rejected. The gateway key and Notion credentials are separate. Inference writes are not automatically retried. If the client disconnects, polling stops; the Notion task may still finish remotely.
- Audit logs contain request/session hashes, tool names, sizes, timing, thread IDs, and append/resync/cache status, not full prompts, tool arguments or cookies. They remain local in `.local/`.
- Codex `/v1/responses` is implemented, including text, function/custom tool calls, tool outputs, and SSE completion events. The Notion model exposes native local coding tools only. External connector/plugin tool definitions are not sent to Notion; this avoids Notion's observed HTTP 413 for the roughly 600 KB full plugin tool catalog. Original GPT models keep all existing tools via pass-through.
- Codex's model catalog is refreshed from the original provider and merged with the Notion entry; a local complete catalog snapshot is used if catalog refresh is unavailable. ChatGPT credentials are forwarded only to the original ChatGPT endpoint, never to Notion. The local routing key is stripped from outgoing provider requests.
- The original `/v1/chat/completions` server remains unchanged.

## Verified integration results (2026-10-18)

- Claude Code with normal global configuration and the new model returned `INSTALLED_CLAUDE_OK`.
- Codex with normal global configuration and the new model returned `INSTALLED_CODEX_OK`.
- Codex independently read a disposable Python project, edited `duration.py` with `apply_patch`, and executed `python3 -m unittest -v`; all three test methods passed. Tests and README remained unchanged.
- Existing ChatGPT routing through the new router returned `ORIGINAL_CODEX_OK`.
- The original DuckCode destination produced a TLS `UNEXPECTED_EOF_WHILE_READING` on direct inspection. Its configuration is preserved; live availability of that upstream was not established. Pass-through preservation is covered by stream/credential isolation tests.
- Twenty-one protocol/routing/session boundary checks passed. Token/cost fields shown by clients remain estimates, not Notion billing data.
- Persistent-session acceptance: one real Claude Code conversation made eight model requests across five CLI invocations, including a gateway process restart, `Read`, `Read`, `Edit`, and `Bash`. All eight reused one Notion thread; the client recalled the first-turn marker, modified a disposable Python file, and passed its unchanged test.
- A real Codex conversation resumed with the same Notion thread and recalled its first-turn marker. Two transient upstream transport failures required client retries; the remote transcript had exactly two user messages and two completed turns, confirming retries did not duplicate the continuation.

## Verify

```sh
.venv/bin/python -m unittest discover -s checks -p '*_checks.py' -v
.venv/bin/python scripts/accept_gateway.py
```

The acceptance script starts the real Claude Code CLI against the running gateway, supplies only a disposable Python sample, and requires actual local file edits plus test execution. It first proves the sample fails, protects the supplied tests with a hash, and runs additional independent checks afterward. It writes the actual client event stream, source diff and result under `.local/acceptance-*/`. A prior UI JSON echo does not count as acceptance.

References: [Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming), [Claude Code gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol).
