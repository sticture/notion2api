# Notion Proxy · Claude Code / Codex 本地编程

通过你自己的 Notion AI 登录会话，把 **Notion Opus 5.5** 接入原版 Claude Code 和 Codex CLI。客户端负责读文件、改代码、执行终端命令；代理负责协议转换和与 Notion 的通信。

基于 [maverickxone/notion2api](https://github.com/maverickxone/notion2api) 扩展。本仓库还保留原有 Chat Completions 服务、39 模型目录和 `patch_agent.py`；下面的步骤针对新增的本地编程代理，旧服务启动方式见 [原服务说明](docs/LEGACY_CHAT_COMPLETIONS.md)。两套服务的登录配置和启动入口不同。

**私有仓库说明：** 需要仓库所有者授予 GitHub 访问权限才能 clone。每个人导入自己的 Notion 登录信息；不要复制别人的 `.local` 目录。获得 GitHub 权限与加入 Notion workspace 是两回事。

> **费用提醒：** 付费 Notion workspace 通常按成员席位计费，邀请别人为 Member 可能产生额外费用。使用这份程序不要求把对方邀请到你的 workspace。每个人使用自己已有权限且已开通目标模型的 workspace。[Notion 官方计费说明](https://www.notion.com/help/billing)

## 支持范围

- Anthropic Messages：`POST /v1/messages`，支持 CC Desktop / Claude Code 的 base64 图片附件和工具返回图片（PNG/JPEG/GIF/WebP，单张最多 20 MiB，每次转发最多 19 张不同图片）。
- OpenAI Responses：`POST /v1/responses`，支持本地 function/custom 工具，包括 `apply_patch`。
- 同一个客户端对话正常续轮复用同一个 Notion thread，工具结果也在其中续聊；绑定保存在本地，重启代理后可恢复。
- 多账号、多 workspace 使用独立 profile；数据库按账号和 workspace 隔离。
- 只监听本机 `127.0.0.1`，需要本地随机密钥；不安装登录自启。
- **macOS 已完成真实 Claude Code / Codex 改文件、运行测试和续聊验证。** Linux 使用相同脚本但尚未做真实客户端验收；Windows 提供 Python 启动方式，尚未做完整验收。

这不是 Notion 官方 API，也不是原生 Claude API。工具调用通过 JSON 桥接实现；暂不支持图片 URL、PDF 等文档附件、Responses 图片、Notion 内部工具审批或 Codex 外部插件工具。模型能否使用、账号额度及限流以当前 Notion workspace 为准。

## 1. Clone 并安装依赖

需要 **Python 3.11+、Git**，以及至少一个已经安装好的客户端。可先执行 `claude --version` 或 `codex --version` 确认它在 PATH 中。已验证版本：Claude Code 2.1.302、Codex CLI 0.163.0。

macOS / Linux：

```sh
git clone https://github.com/sticture/notion2api.git
cd notion2api
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
umask 077
mkdir -p .local
```

如果 `python3 --version` 低于 3.11，请改用已安装的新版本，例如 `python3.12 -m venv .venv`。私有仓库出现 404/权限错误时，请先确认 GitHub 登录和仓库访问权限。

## 2. 导入自己的 Notion 登录信息

只在首次使用或登录失效时操作一次；日常启动不需要重新导入。

1. 在 Chrome 登录 `https://app.notion.com`，左上角切到你想使用的 **workspace**。确认该空间的 AI 模型菜单里可以选择 **Opus 5.5**。
2. 打开 Chrome 开发者工具 → **Network / 网络**，筛选 `getAvailableModels`。
3. 保持 Network 开启，刷新 Notion 页面并打开 AI 模型菜单，找到发往 `https://app.notion.com/api/v3/getAvailableModels` 的请求。
4. 右键该请求 → **Copy → Copy as cURL (bash)**。不要选择 PowerShell 或 cmd 格式。
5. 用文本编辑器将完整内容保存为仓库内的 `.local/notion-request.curl`，使用 UTF-8 编码。**不要把复制出来的 curl 粘到终端执行，也不要发到聊天、Issue 或 GitHub。** 它包含你的登录 Cookie。
6. 执行：

```sh
.venv/bin/python scripts/setup_gateway.py \
  --from-curl .local/notion-request.curl \
  --install-commands
```

脚本只解析复制文本，不执行其中的 shell 命令。它会提取该请求的账号、workspace、Cookie 和必要客户端请求头，调用 Notion 检查 Opus 5.5 权限，验证成功后才保存配置。这个检查不创建 AI 聊天。

成功后会显示 `verified Opus 5.5`，并安装三个手动命令到 `~/.local/bin`：

| 命令 | 用途 |
|---|---|
| `notion-proxy` | 启动本机代理，前台运行 |
| `claude-notion` | 启动原版 Claude Code，本次进程连接 Notion |
| `codex-notion` | 启动原版 Codex，本次进程连接 Notion |

如果终端提示 command not found，把这一行加到你的 shell 配置（例如 `~/.zshrc`），然后重开终端：

```sh
export PATH="$HOME/.local/bin:$PATH"
```

初始化不会覆盖原有 Claude/Codex 全局设置，也不会更改原来的登录方式。导入成功后，可以删除临时的 `.local/notion-request.curl`；保留生成的 profile 配置和 `gateway-key`。

**原仓库的 `python login.py` 生成 `accounts.json`，不能直接替代这里的编程代理初始化。** 新代理使用当前 Notion Agent 接口和完整网页会话，不使用 Notion integration token。

## 3. 日常使用：一个代理，任意项目共用

终端 A，在任何目录启动：

```sh
notion-proxy
```

看到 `127.0.0.1:8765` 后保持这个终端运行。关闭代理用 **Ctrl+C**；没有设置开机或登录自启。
每次执行 `notion-proxy` 都会先关闭本项目之前启动的代理（包括卡住的），再启动新的。

终端 B，进入你要开发的项目，然后任选一个客户端：

```sh
cd /path/to/your/project
claude-notion
```

或者：

```sh
cd /path/to/your/project
codex-notion
```

这两个命令都是原版 CLI 的启动包装，不是另一个编程客户端。正常的文件/终端权限仍由 CLI 管理；不用在每个项目里复制配置、填密钥或启动第二个代理。CLI 的 `--resume` / `resume` 等参数可照常传入。

模型 ID 为 `notion-claude-opus-5-5`（Messages 兼容别名为 `claude-opus-5-5`）。若已经安装了“普通客户端模型列表”接入，可继续直接运行 `claude` / `codex`，在模型列表里选择 **Notion Opus 5.5**。

### 直接在普通客户端模型列表中切换

仓库保留 `scripts/install_model_picker.py`，用于把 Notion 加入现有全局模型路由，并备份原配置。它目前要求：

- Claude Code 已配置 HTTPS 第三方 gateway 和 `ANTHROPIC_AUTH_TOKEN`；
- Codex 已显式配置使用 ChatGPT 登录的自定义 provider；
- 两个客户端配置文件均已存在。

满足这些条件、确实需要更改全局路由时运行：

```sh
.venv/bin/python scripts/install_model_picker.py
```

它不是适用于所有账号的通用安装器。原生 Claude 订阅登录、仅安装一个客户端或全新配置，优先使用上面的 `claude-notion` / `codex-notion` 命令。全局路由安装后，原有模型也会经本机代理转发，因此使用那些模型时代理同样需要运行。备份保存在 `.local/integration-backups/`。

## 4. 一个账号有多个 workspace 怎么办？

每个 workspace 建一个 profile。选择哪个空间，取决于**复制请求时浏览器当前所在的 workspace**；不要手工只替换 `space_id`，因为该空间的路由请求头也可能不同。

例如为团队空间配置 `team`：

1. 浏览器切换到团队 workspace。
2. 按第 2 节重新复制该空间的 `getAvailableModels` 请求，保存为 `.local/team-request.curl`。
3. 导入并验证：

```sh
.venv/bin/python scripts/setup_gateway.py \
  --from-curl .local/team-request.curl \
  --profile team
```

查看已保存的 profile：

```sh
.venv/bin/python scripts/setup_gateway.py --list-profiles
```

使用团队空间：直接运行下面的命令（会自动关闭之前的代理）：

```sh
notion-proxy --profile team
```

切回默认空间：

```sh
notion-proxy
```

- 每个 profile 单独保存登录信息和会话状态。默认自动加载已保存的备用 profile，额度用完时自动切换；模型权限不符或重复 workspace 会跳过。使用 `--failover ''` 可关闭自动切换。
- 同一个账号的不同 workspace 要分别检查模型权限。无权使用时会报错，不会自动回退到其他模型。
- 手动改变主 profile 后，建议新建客户端对话；自动切换备用 workspace 时可继续原对话，代理会把最近的客户端历史带到新空间。
- 使用相同 profile 续聊、重启或刷新登录，原会话绑定会保留。若 profile 的账号/workspace 发生变化，会使用独立的绑定命名空间。

### 新 thread 首轮输入上限

每次新建 Notion thread（首次绑定、独立辅助请求、切换 workspace）时，首轮输入预算默认 195000 个 JSON 转义后的 UTF-8 字节（`NOTION_GATEWAY_NEW_THREAD_MAX_CHARS`，备用 workspace 的 `NOTION_GATEWAY_FAILOVER_SEED_CHARS` 默认相同），同时受 `NOTION_GATEWAY_MAX_SEND_CHARS` 的单次发送保护限制。预算包含系统提示和工具定义。超出时先丢最早的完整历史轮次；单条文本超过首轮用量预算时保留头尾。长轮次仍超限时继续压缩历史工具输出并明确标注，保留用户指令和工具参数；不可压缩的内容仍超限则返回 413。之后的轮次照常只发增量。

同一客户端会话默认一直复用原 Notion thread，`NOTION_GATEWAY_THREAD_MAX_CHARS=0` 表示不因累计长度换线程，累计历史可以超过 300K；这不代表 Notion 模型保证保留全部历史。将新线程预算设为 `300000` 也不会绕过默认 `195000` 字节的单次发送保护。Notion 新建线程时会多次保存输入，直接提高发送保护可能触发其 409600 字节的内部写入限制。

历史压缩或编辑在原 thread 上重同步，不受较小的首轮预算限制。Claude Code 的离开后自动回顾也复用原 thread，但单独缓存响应，不覆盖主对话的历史快照，避免下一句用户消息触发不必要的全量重同步。

### 额度用完自动切换 workspace

默认 `notion-proxy` 会扫描已保存的 profile：主 profile 优先，其他 profile 按名称排序，启动时验证模型权限并去重。新增 profile 后需重启代理。`/health` 的 `workspaces` 列表可查看已加载和跳过的空间；`available` 表示未在冷却期，并非实时余额查询。也可显式限定备用列表：

```sh
notion-proxy --profile default --failover team,team2
```

（也可以设置环境变量 `NOTION_GATEWAY_FAILOVER_PROFILES=team,team2`；命令行参数优先。`--failover ''` 仅使用主 profile。）

- 当前 workspace 返回 429 或额度用完时，代理会自动换到下一个 profile，同一个客户端对话继续进行，不需要新建。
- 切换的那一轮只把最近一段客户端历史（默认 60000 个 JSON 转义后的 UTF-8 字节，`NOTION_GATEWAY_FAILOVER_SEED_CHARS`，同时受首轮和单次发送预算限制）带到新 workspace 的新 thread，之后照常只发增量。
- 请求启动慢、无进展或超时不会触发 workspace 切换，也不会将 workspace 标记为额度耗尽；只有收到明确的额度耗尽或限流信号才切换。原有总超时和空闲超时仍然生效，同一请求重试时继续轮询原会话。
- 只在本轮答案还没交付给客户端时切换，工具调用不会重复执行。
- 对话切过去后会留在新 workspace；原 workspace 恢复后也不会来回跳。冷却时间优先采用 Notion 返回的 `Retry-After`，否则默认 1800 秒（`NOTION_GATEWAY_EXHAUSTED_COOLDOWN`）。
- 所有 workspace 都不可用时返回 429。启动时没有 Opus 5.5 权限的备用 profile 会被跳过，可在 `/health` 的 `workspaces` 字段查看每个 profile 的状态。

需要同时开多个 profile 时可给代理不同的 `--port`；对应客户端用 `NOTION_PROXY_PORT` 指向它：

```sh
notion-proxy --profile team --port 8766
# 在另一个终端运行：
NOTION_PROXY_PORT=8766 claude-notion
```

## 检查、更新和登录失效

只检查登录和模型权限：

```sh
cd /path/to/notion2api
.venv/bin/python scripts/setup_gateway.py --check
# 命名 profile：
.venv/bin/python scripts/setup_gateway.py --profile team --check
```

更新代码：

```sh
cd /path/to/notion2api
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
```

然后重新执行 `notion-proxy`（会自动关闭旧进程）（命名 profile 加上对应参数）。更新无需重新导入有效的 Cookie，不要删除 `.local`。

Cookie 过期时，在浏览器重新登录并按第 2 节导入，指定原来的 profile，最后重启代理。密钥文件存在时初始化会保留它，不会让客户端配置突然失效。

## Windows（尚未完整验收）

使用 Python 3.11+ 和 UTF-8 的 cURL **bash** 文本。不要加 `--install-commands`；Windows 直接运行 Python 脚本：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts\setup_gateway.py --from-curl .local\notion-request.curl
.venv\Scripts\python.exe scripts\notion_proxy.py
```

在项目目录的另一个终端，通过仓库绝对路径运行 `scripts\claude_notion.py` 或 `scripts\codex_notion.py`。例如：

```powershell
C:\path\to\notion2api\.venv\Scripts\python.exe C:\path\to\notion2api\scripts\claude_notion.py
```

macOS/Linux 不使用全局命令时，也可通过仓库 `.venv/bin/python` 和对应脚本的绝对路径启动。

## 常见问题

| 现象 | 处理 |
|---|---|
| GitHub clone 提示 404 | 私有仓库需要授权访问和正确的 GitHub 登录 |
| `Profile is not configured` | 先运行 `setup_gateway.py --from-curl ...` |
| `missing token_v2` 或账号/workspace 缺失 | 复制已登录的实际请求，包含 Cookie 和必要请求头；不要手工删字段 |
| `Opus 5.5 is not enabled` | 当前 workspace 没有可用权限，先在网页确认；代理不会绕过权限或自动买席位 |
| HTTP 401 / 403（Notion JSON） | 检查登录是否失效、是否导入了正确账号和 workspace |
| Desktop 提示认证失败，但上游是 Cloudflare 403 HTML | 网关只在明确边缘拦截时自动重试固定的 Notion 官方 `www.notion.so` API；成功后复用该入口，不改变账号、Workspace 或 Thread。两个官方入口均被拦时返回 503 并退避，不要求重新登录，也不会因此切换 Workspace |
| `Could not stop previous Notion proxy` | 旧网关进程关不掉（例如属于其他用户），手动 `kill` 后重试 |
| `createAgentThread` 400 / 超长上下文 413 | 19.5 万字节预算只检查提取增量后、JSON 转义后的 UTF-8 内容；中文和反斜杠也按实际大小计入。完整客户端历史可达 800 万字符（HTTP 请求另限 128 MB）。首次导入/重同步会按完整用户轮次移除最旧历史并告知模型；新 Thread 导入仍超限时压缩历史输出，不改工具参数。不可压缩的单轮输入仍需分批 |
| 回复看起来停顿 | SSE 会保持心跳，但 JSON 工具结果要完整生成并校验后才能发送 |
| 偶尔又新建了一个 Notion 聊天 | 主对话续轮、失败恢复和历史重同步均复用；新客户端会话、独立辅助请求、明确额度耗尽后的 workspace 切换，以及显式启用的长度轮换仍可能新建 |
| 只在浏览器看到 JSON，项目没变化 | 不算编程成功；检查客户端是否真的执行 Read/Edit/Bash 或 exec_command/apply_patch，并查看测试结果 |

## 数据存放和限制

`.local/` 已被 Git 忽略，包含登录 Cookie、本机 API key、会话数据库、响应缓存和诊断日志。它们不是可分享的安装文件。默认 profile 在 `.local/`；命名 profile 在 `.local/profiles/<name>/`。不要把这些内容提交到 GitHub，即使仓库是私有的。

代码、提示词及工具结果会通过你的账号发送到 Notion 并生成聊天记录。Notion Cookie 只用于固定的 Notion 官方 `https://app.notion.com` 和 `https://www.notion.so` API，不跟随重定向，不交给 Claude Code 或 Codex。代理不会直接执行本地工具；输入和输出 token 数只是估算，并非官方计费数据。

普通续聊只发送新增消息与有变化的设置，不重复发送完整历史、上一轮助手正文、代码补丁或未变化的工具定义。工具结果通过简短 ID 对照关联原调用。已知的客户端计费追踪头和剩余 token 计数不会触发系统提示重发。单条超长工具结果默认保留头尾共约 16,000 字符，并标注截断、提示按范围重读；用户指令和代码编辑参数不自动截断。首次导入或历史重同步超过实际发送预算时，会按完整用户轮次移除最旧历史，并在传给模型的上下文中标注；新 Thread 的历史输出还会按实际发送预算压缩，不会因最大的条目是工具参数而中止压缩。不可压缩的输入仍超限则返回 413。为兼容旧配置，`NOTION_GATEWAY_MAX_SEND_CHARS` 名称保留，但其值现在表示 JSON 转义后的 UTF-8 字节预算（默认 195000），不是字符数。普通续轮不会为满足预算删除新增指令。

这些优化减少代理重复上传的数据，不能据此推算 Notion 的额度消耗或计费：Notion 仍管理其远端会话历史，具体缓存和计量方式不由代理控制。审计日志中的 `client_input_chars` / `upstream_input_chars` 用于区分完整客户端输入和本轮实际发送量；客户端 token usage 不是官方账单。

普通续聊采用增量历史。重试同一请求会复用已验证结果及工具 ID。网络结果不确定且客户端改变上下文时，先在原 thread 等待旧轮次结束，再使用当前客户端历史继续；旧轮次未交付的工具结果不会自动执行。旧轮次仍未结束时返回超时或错误，保留绑定供后续重试，不会因此偷偷新建 thread。原聊天记录不会被自动删除。详细实现和限制见 [网关文档](docs/LOCAL_CODING_GATEWAY.md)。

## 开发验证

```sh
.venv/bin/python -m unittest discover -s checks -p '*_checks.py' -v
.venv/bin/python scripts/accept_gateway.py
```

第二条命令需要代理和真实 Claude Code，会创建一个临时 Python 项目，让客户端实际读文件、修改实现并运行测试，同时核对测试文件未被改动。测试材料保存在 `.local/`，不进入仓库。

配置参考：[Codex 官方配置](https://learn.chatgpt.com/docs/config-file/config-reference)、[Claude Code 网关协议](https://code.claude.com/docs/en/llm-gateway-protocol)。
