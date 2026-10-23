# Local Opus 5.5 coding gateway (experimental)

This server provides Anthropic Messages and OpenAI Responses endpoints. It uses the current Notion Agent endpoints, rather than changing the original Chat Completions server. The verified workspace mapping is `albuquerque-quinn` → **Opus 5.5**.

## Existing global model-picker installation

From any directory, run one command:

```sh
notion-proxy
```

Keep that terminal open. Then open Claude Code or Codex normally and select **Notion Opus 5.5**. Claude Code exposes it in `/model`; Codex exposes it in its model picker. The actual model ID is `notion-claude-opus-5-5`. Restart clients opened before installation to reload their configuration. No login autostart or LaunchAgent is installed.

The one running proxy is shared by all projects. Existing Claude models route to the preserved DuckCode gateway; existing Codex models route to the preserved ChatGPT backend with the client's own OAuth handling. These routes also require the local proxy to be running after integration. Stop a foreground proxy with Ctrl+C. If the command reports that it is already running, use the existing instance.

Global configuration was backed up under `.local/integration-backups/`. Only Claude's gateway/custom-model environment settings and Codex's selected provider plus the new provider block were changed. Existing default model, permissions, original provider blocks, skills, and plugin configuration were preserved.

Each Claude Code or Codex conversation now keeps one Notion thread, including local tool calls and resumed conversations. Restarting `notion-proxy` preserves the binding. Starting a new client conversation creates a separate binding. Conversations from the old stateless gateway establish their binding once on the first request after this upgrade, importing the supplied client history; subsequent requests reuse it. Existing Notion chat records are left intact.

## Server development

Requires Python 3.11 or newer and an installed Claude Code CLI.

```sh
cd /path/to/notion2api
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/local_gateway.py
```

The gateway binds to `127.0.0.1:8765`. It creates `.local/gateway-key` with mode 0600. Do not put a Notion Cookie in a Claude Code configuration: only the gateway holds the Notion session.

From the project you want to work on, run:

```sh
/path/to/notion2api/.venv/bin/python \
  /path/to/notion2api/scripts/claude_notion.py \
  --safe-mode --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --tools Read,Edit,Write,Bash,Glob,Grep
```

The older `claude_notion.py` launcher remains available for isolated diagnostics. Everyday use no longer needs it. Normal local tool permission checks remain active. Supplied code, prompts, and tool results go to Notion, and Notion creates chat records. After provisioning the local session and model catalog, `scripts/install_model_picker.py` installs the global picker integration and the manual command.

## Session file and profiles

Default settings live in `.local/`; named profiles use `.local/profiles/<name>/`. The server selects the profile through `notion-proxy --profile <name>`. Bindings are namespaced by Notion user and workspace. The original default database is migrated in place on first upgraded startup. Both CLI wrappers use process-local settings, while the global model-picker installer is an optional advanced path. See the README for prerequisites and workspace switching rules.


For a fresh installation, follow the [README](../README.md) and import your own browser-copied request with `scripts/setup_gateway.py`. The importer verifies model access before saving a profile. `.local/notion-session.json` is ignored by Git and stored with mode 0600. It contains:

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

Use only your own session. Current deployments can also require session-binding and device cookies. Preserve the appropriate Notion cookies from the authorized session; do not copy unrelated site credentials. API destinations are fixed in code to official Notion origins: `https://app.notion.com`, with `https://www.notion.so` used only after an explicit Cloudflare HTML 403 rejection. The same request/event is retried there, preserving its workspace and thread, and a successful origin is reused. Redirects are never followed. If both origins are blocked, return 503 with a 30-second upstream backoff rather than a false authentication error or workspace failover. Genuine JSON 401/403 errors retain their authentication/permission semantics. On session expiration, refresh the session file locally and restart. Never paste its contents into chat, issue reports, or commits. Deleting `.local/notion-session.json` removes this application's local copy; session revocation is managed in Notion.

## Implementation and limitations

- `app/agent_gateway/upstream.py` authenticates to Notion, creates a personal-agent thread, and reads ordered transcript patches with a forward cursor. It waits for `turn_completed` with `end_turn`; an idle session alone is not success.
- `app/agent_gateway/sessions.py` binds Claude's `x-claude-code-session-id` (with `metadata.user_id` fallback) and Codex's `client_metadata.thread_id` to persistent Notion threads. Claude agent IDs and request classes, when supplied, isolate auxiliary conversations. Other API clients can send `x-notion-session-id`; clients without an identity remain stateless, rather than guessing from prompt similarity.
- The first request creates the thread; later requests use `sendEventToAgentThread`. Hashes of normalized message blocks identify the unchanged prefix, so only new messages, acknowledged assistant outputs with their local tool IDs, and changed settings are sent. Edited/compacted histories trigger an explicit authoritative-history resync in the same thread. Notion still retains its older transcript; this is a context instruction, not physical deletion of old messages.
- Desktop can revise interruption/retry markers at the tail of an otherwise unchanged history. A resync preserves that known prefix and sends only the revised suffix, with an explicit replacement marker; it starts at a user turn and retains complete tool-call/result pairs. A change at the beginning of history still uses a full resync. This prevents a small Desktop retry from repeatedly replaying a large old transcript.
- Bindings, transcript cursors, history hashes, and the last validated answer are stored in private `.local/gateway-sessions.sqlite3` (0600). Pending raw answers may be retained until validation succeeds. This file contains response/tool data and must remain private. Keep it across restarts; deleting it loses bindings. The reasoning effort selected when the Notion thread was created remains in effect for that thread.
- Retrying an identical request reuses its validated answer and tool IDs. If a write's outcome is unknown or polling is interrupted, retry resumes polling that same turn instead of submitting it again. A changed request first drains the prior turn, persists its completed cursor, and then appends to the same Notion thread. Its stale output/tool calls are not delivered. A known terminal failure can also be advanced past; an unfinished or unknown outcome remains pending and returns an error rather than creating a replacement thread. Definitively rejected continuation attempts retain the completed cursor and original thread for retry. Completion polling uses the sequence checkpoint and the next user message, not turn ID alone: Notion can reuse a turn ID for a later message. Old assistant messages and completions before the new input are excluded.
- An unacknowledged continuation (including legacy pending records) is checked before polling: three transcript reads must explicitly show an idle session, no queued input, no new patches and exactly the last completed sequence. Only then can the client retry submit on the same thread; an identical request retains its event ID. Successful dispatch is recorded durably so ordinary generation timeouts are never replayed. New activity, missing state or failed reads preserve the pending turn. Initial thread creation has no completed checkpoint and remains poll-only.
- Claude Code's background away-summary requests share the main Notion thread and lock. Their responses are cached separately, and their transient messages do not replace the main history hashes or acknowledged tool IDs. Upstream settings changes are tracked so the next main request restores its own settings. This avoids an unnecessary full-history resync when the next user message omits the background recap.
- Thread-length rollover is disabled by default (`NOTION_GATEWAY_THREAD_MAX_CHARS=0`); accumulated history can exceed 300K without a gateway-created replacement. Fresh-thread and failover seed budgets remain subject to the single-send wire limit, even when configured to 300000. Resync uses the single-send budget rather than the smaller seed cap and instructs Notion to retain relevant earlier context. Notion controls its own context retention and billing.
- New threads disable browser access, suggested edits, and use of memories; they request exclusion from memories. These flags were verified in live calls. They do not turn Notion Agent into a raw Anthropic API, or guarantee every internal Notion tool is disabled. Instructions tell it to use the supplied local-tool protocol instead.
- Tools are emulated through constrained JSON text, **not native Notion client tool calling**. Every output tool must exist in the client request and pass its JSON schema. The proxy never executes shell commands or modifies project files; Claude Code does that using its own tools and permissions.
- The entire JSON answer, including leading prose, is buffered until every tool argument and output constraint validates. Invalid model output is corrected on the same thread using the current exact schema, at most twice per client request. The repair count, event ID and correction are persisted before dispatch, so disconnects and restarts cannot reset the limit or repeat accepted calls. Only a validated answer is cached for delivery. Exhausting corrections returns a non-retryable 422 rather than an endless cached 502. SSE starts immediately and sends `ping` events while waiting, then emits the validated Anthropic-compatible blocks.
- Thinking blocks are not relayed and no thinking signatures are invented. Text, `tool_use`, and `tool_result` are supported. Messages image blocks, including images nested in tool results, are uploaded through Notion's native file API and attached to the same thread. Supported sources are base64 PNG/JPEG/GIF/WebP, at most 20 MiB per image and 19 distinct images per dispatched turn. URL/file-ID image sources return an explicit input error; document attachments and Responses images remain unsupported. Server tools are unsupported; JSON-schema output and stop sequences are emulated by the bridge.
- Image bytes live only in request memory, outside text-history budgets and the session database. Stable image hashes preserve incremental history; unchanged historical images are not re-uploaded on ordinary follow-ups. New-thread/failover replays attach the retained images again. Uploads finish before saving a pending generation, so an upload failure cannot strand the conversation. Storage requests use a separate client without Notion cookies or client credentials. Token estimates do not include image processing costs.
- Sessions created before native image support are migrated by matching the exact legacy placeholder hashes. Their unchanged text/tool history is not replayed. Previously omitted images are attached separately with their original history index and tool-result ID, then canonical hashes are saved for future appends. New-turn images have priority; historical upgrades beyond the 19-image limit are saved as hash-only references and completed on later turns. A failed upload preserves the original pending record. Changes to normalized content formats must include an existing-session migration regression; fresh-session image tests alone do not cover compatibility.
- The supported `scripts/notion_proxy.py` launcher runs all `checks/*_checks.py` before stopping the existing proxy. A failed pre-start check leaves the running service untouched. Launching `scripts/local_gateway.py` directly bypasses this guard and is intended only for development.
- Token usage is an explicit character-based estimate (`x-notion-usage-estimated: true`), not Notion billing data; cache usage is not claimed. `max_tokens` is passed as a textual output budget rather than a native Notion decoding limit. The token counting endpoint is intentionally absent, so Claude Code uses its own fallback.
- Notion allowances are read separately from `getCreditRateLimitStatus`. Authenticated `GET /v1/notion/usage` (also `/claude/v1/notion/usage`) returns the actual rolling duration, used/remaining percentages, supplied billing-period dates and observation time. Send the same session header as Messages to select that session's workspace, including after failover; without an identity it reports the primary workspace. This endpoint does not generate a model turn or switch workspaces. Reads are coalesced and cached for 30 seconds; errors return `unavailable`, or explicitly `stale` with the previous observation time, never a fabricated zero balance.
- Claude Code Desktop requests with `mcp__ccd_*` tools display these allowances under each final prose answer. Other Messages clients can opt in with `x-notion-usage-display: footer`; use `off` or set `NOTION_GATEWAY_DESKTOP_USAGE_FOOTER=false` to disable future footers. Tool-call responses, JSON schemas, stop sequences and background away summaries are unchanged. The displayed answer is included in the durable response snapshot, so acknowledgements/retries/restarts retain the same Thread. Desktop's native plan-usage page is disabled for Gateway routing and has fixed Claude weekly labels, so it cannot represent a Notion monthly period faithfully.
- The inspected Notion APIs do not supply an official context-window limit, active token count or remaining context. The footer explicitly reports the request's estimated text tokens (excluding image tokens) and marks Notion's capacity/remaining amount as unavailable. These estimates must not be interpreted as Notion's retained Thread context or billed tokens. Window labels come from the actual response: the observed workspace reports `6h` plus a monthly billing period, rather than assuming five hours and a fixed thirty days.
- Full client histories are bounded to 8,000,000 characters and HTTP bodies to 128 MB. The 195,000-byte upstream limit (UTF-8 after JSON string escaping; legacy environment name `NOTION_GATEWAY_MAX_SEND_CHARS`) applies only after delta extraction; unchanged history does not count against it. Fresh-thread and workspace-failover seeds default to the same 195,000-byte budget. Individual large tool results are explicitly shortened to approximately 16,000 characters, while user instructions/tool arguments are not silently trimmed. Initial/resync histories omit the oldest complete turns with an explicit context marker to fit the budget. Fresh-thread imports additionally shorten replayed output to the actual wire cap, even if a larger seed budget is configured or the largest item is an immutable tool invocation. User instructions and tool arguments are preserved in this second pass. Input that still cannot fit is rejected before dispatch. Notion DynamoDB size errors are classified as 413, not retryable 502; safe diagnostics log endpoint, status, byte size and error ID without prompts or credentials. Requests are serialized per client session (other sessions and side requests run concurrently). Each turn has a 300-second upstream deadline (`NOTION_GATEWAY_TIMEOUT`) and fails after 90 seconds without transcript progress (`NOTION_GATEWAY_IDLE_TIMEOUT`, 0 disables). Large repository tasks may need compaction. This bridge does not provide full Claude API compatibility.
- Browser-origin requests, non-loopback hosts and unauthenticated API requests are rejected. The gateway key and Notion credentials are separate. Transcript/model-directory reads retry transient transport failures and HTTP 408/500/502/503/504 up to three attempts with bounded backoff; authentication and quota failures are not retried this way. A failed continuation write may be retried once with the exact event ID/payload only after the stable idle checkpoint proves it was not accepted. Other ambiguous writes remain pending. If the client disconnects, polling stops; the Notion task may still finish remotely.
- Audit logs contain request/session hashes, tool names, sizes, timing, thread IDs, repair/retry counts, safe error codes, and append/resync/cache status, not full prompts, tool arguments or cookies. They remain local in `.local/`.
- Codex `/v1/responses` is implemented, including text, function/custom tool calls, tool outputs, and SSE completion events. The Notion model exposes native local coding tools only. External connector/plugin tool definitions are not sent to Notion; this avoids Notion's observed HTTP 413 for the roughly 600 KB full plugin tool catalog. Original GPT models keep all existing tools via pass-through.
- Original-provider pass-through forwards complete SSE events as they arrive and closes the connection at the protocol's terminal event. A mid-stream transport failure discards the unfinished event and emits a client-protocol error instead of an ASGI traceback or a success marker. Non-streaming responses are read before sending headers, so interrupted bodies return 502. The gateway does not replay generation requests; client disconnects still release the upstream connection. Safe warnings record the provider, endpoint and exception class only.
- Codex's model catalog is refreshed from the original provider and merged with the Notion entry; a local complete catalog snapshot is used if catalog refresh is unavailable. ChatGPT credentials are forwarded only to the original ChatGPT endpoint, never to Notion. The local routing key is stripped from outgoing provider requests.
- The original `/v1/chat/completions` server remains unchanged.

## Verified Desktop recovery (2026-10-21)

- 104 local checks cover reused turn IDs, stale completions, invalid/nested tool arguments, bounded correction across retry/restart, lost acknowledgements, safe read retries and SSE validation boundaries.
- The existing Claude Desktop Code conversation completed eight model turns on its original Notion thread/workspace, including the Desktop plan tool, two file reads, `Bash` running only `pwd`, and the Desktop wrap-up tool. One actual invalid plan response was corrected once before delivery.
- A generation was intentionally stopped in Desktop, the gateway was restarted with its accepted turn still pending, and the next request drained it and completed a new `Read` plus final response. Desktop retained the earlier business requirements. The final session had no pending turn; no other request failures occurred during this acceptance. Local evidence is `.local/desktop-full-fix-acceptance.json` (not committed).

## Verified integration results (2026-10-18)

- Claude Code with normal global configuration and the new model returned `INSTALLED_CLAUDE_OK`.
- Codex with normal global configuration and the new model returned `INSTALLED_CODEX_OK`.
- Codex independently read a disposable Python project, edited `duration.py` with `apply_patch`, and executed `python3 -m unittest -v`; all three test methods passed. Tests and README remained unchanged.
- Existing ChatGPT routing through the new router returned `ORIGINAL_CODEX_OK`.
- The original DuckCode destination produced a TLS `UNEXPECTED_EOF_WHILE_READING` on direct inspection. Its configuration is preserved; live availability of that upstream was not established. Pass-through preservation is covered by stream/credential isolation tests.
- Protocol/routing/session/setup checks are in `checks/`; run the command below for the current result. Token/cost fields shown by clients remain estimates, not Notion billing data.
- Persistent-session acceptance: one real Claude Code conversation made eight model requests across five CLI invocations, including a gateway process restart, `Read`, `Read`, `Edit`, and `Bash`. All eight reused one Notion thread; the client recalled the first-turn marker, modified a disposable Python file, and passed its unchanged test.
- A real Codex conversation resumed with the same Notion thread and recalled its first-turn marker. Two transient upstream transport failures required client retries; the remote transcript had exactly two user messages and two completed turns, confirming retries did not duplicate the continuation.

## Verify

```sh
.venv/bin/python -m unittest discover -s checks -p '*_checks.py' -v
.venv/bin/python scripts/accept_gateway.py
```

The acceptance script starts the real Claude Code CLI against the running gateway, supplies only a disposable Python sample, and requires actual local file edits plus test execution. It first proves the sample fails, protects the supplied tests with a hash, and runs additional independent checks afterward. It writes the actual client event stream, source diff and result under `.local/acceptance-*/`. A prior UI JSON echo does not count as acceptance.

References: [Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming), [Claude Code gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol).

## Incremental context accounting

Acknowledged assistant output is hashed after response validation so the next client history can skip it. Tool calls retain only an ID/name mapping for the next result; generated code and arguments are not replayed. Stable system directives and tool definitions stay in the Notion thread, changed fields are updated explicitly, and known billing headers/token countdowns are excluded. Normal append requests use a short continuation instruction instead of the full bridge preamble. Side requests with output schemas or stop sequences retain a separate stable binding for retry caching; this does not disable client approval/classification checks. `client_input_chars`, `upstream_input_chars`, and `truncated_tool_results` are local audit measurements, not Notion billing metrics.

### Image acceptance (2026-10-21)

- 122 local checks passed, including 18 image validation, compaction, upload isolation, failure/retry and native attachment checks.
- A live Messages SSE request containing two images (420630-byte request) correctly identified both images and their order. A visual follow-up reused the same Notion thread.
- Actual Claude Code `Read` returned an image content block and correctly identified the synthetic red rectangle and blue circle.
- Claude Desktop Code independently passed both `Read` image output and direct chat attachment: the latter correctly identified a different image containing a green triangle and yellow square. The local proxy was restarted, with its persistent session database retained.
