# Claude-Code-Python

Claude-Code-Python(CCPY) 是一个用 Python 实现的 Claude Code 风格命令行Agent。项目重点复刻核心执行模型：LLM 多轮循环、工具调用、Bash 执行、文件读写搜索、JSONL 会话记录，以及支持并行多任务的 `Task`/`Agent` 子代理工具。

本项目不追求和 Claude Code 官方 UI 完全一致，而是提供一个易读、易改、可开源的 Python agent runtime，可连接 K2.6 或任意 OpenAI-compatible 模型接口。

本项目是一个vibecoding作业。

## 功能特性

- OpenAI-compatible 模型适配，默认模型名为 `kimi-k2.6`。
- 兼容常见模型网关差异：New API 网页地址误配提示、自动 `/v1` fallback、SSE 响应聚合、Kimi/New API `reasoning_content` 补丁。
- 模型请求具备限流保护：429 自动重试、`Retry-After`/New API `try again after` 解析、共享 RPM 节流器。
- Claude Code 风格 agent loop：`model -> tool_use -> tool_result -> model`。
- 严格保持 `tool_call_id` 与 `tool_result` 配对，工具失败也会返回结构化错误。
- `Task`/`Agent` Tool：支持同步子代理、后台子代理、任务输出、停止任务、发送消息。
- 并行工具执行：对声明为 concurrency-safe 的工具批量并发执行，默认并发上限为 10。
- Bash Tool：Windows 默认 PowerShell，Unix 默认 `/bin/bash -lc`，支持工作目录、超时、后台任务和输出捕获。
- 默认工具集：文件、搜索、编辑、Todo、Web、Bash、Task/sub agents。
- 借鉴 Claw Agent 的核心工程化模块：Provider presets、MCP stdio/SSE/HTTP 桥接、micro/snip/reactive compact、memory manifest、session notes 和 Hook 生命周期。
- JSONL 会话记录和 session notes，便于调试、审计、compact 和未来恢复会话。
- 提供 Task Tool demo、multi-agent demo 和 Terminal-Bench smoke 入口。

## 项目结构

```text
Claude-Code-Python/
├── src/claude_code_python/
│   ├── cli.py                 # ccpy CLI 入口
│   ├── runner.py              # AgentRunner 主循环与工具调度
│   ├── model.py               # OpenAI-compatible 模型客户端
│   ├── providers.py           # Provider presets 与模型客户端工厂
│   ├── tasks.py               # 后台任务与子代理任务管理
│   ├── compact.py             # 上下文压缩与工具结果裁剪
│   ├── memory.py              # 项目记忆与 memory manifest
│   ├── hooks.py               # Agent 生命周期 HookManager
│   ├── session.py             # JSONL 会话记录与 session notes
│   ├── permissions.py         # 工作区路径与命令权限限制
│   ├── mcp.py                 # HTTP manifest 兼容加载与 MCP stdio/SSE/HTTP 桥接
│   └── tools/
│       ├── base.py            # Tool 抽象与 schema
│       ├── registry.py        # ToolRegistry
│       └── default.py         # 默认工具实现
├── examples/
│   ├── demo_task_tool.py      # Task Tool 示例
│   └── multi_agent_system.py  # coordinator/worker 多代理示例
├── benchmarks/
│   └── terminalbench_smoke.py # Terminal-Bench 风格 smoke 入口
├── tests/
│   └── test_runner_tools.py   # 单元测试
├── VALIDATION_REPORT.md       # 本地验证报告
└── pyproject.toml
```

## 安装

### Windows PowerShell

```powershell
cd Claude-Code-Python
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

### macOS / Linux

```bash
cd Claude-Code-Python
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

安装后可使用：

```bash
ccpy --help
```

也可以不依赖 console script，直接运行：

```bash
python -m claude_code_python.cli --help
```

## 配置 Kimi2.6

本项目默认使用 OpenAI-compatible `/chat/completions` 接口。你需要提供支持 tool calling 的兼容服务。

### Windows PowerShell

```powershell
$env:CCPY_BASE_URL="https://your-openai-compatible-endpoint/v1"
$env:CCPY_API_KEY="your_api_key"
$env:CCPY_MODEL="kimi-k2.6"
```

### macOS / Linux

```bash
export CCPY_BASE_URL="https://your-openai-compatible-endpoint/v1"
export CCPY_API_KEY="your_api_key"
export CCPY_MODEL="kimi-k2.6"
```

可选环境变量：

- `CCPY_WORKSPACE`：工作区根目录，默认当前目录。
- `CCPY_PROVIDER`：Provider preset 名称，默认 `openai-compatible`。可选 `openai`、`kimi`、`moonshot`、`minimax`、`deepseek`、`qwen`。
- `CCPY_MAX_TOOL_CONCURRENCY`：并发安全工具的最大并发数，默认 `10`。
- `CCPY_MAX_TURNS`：单次任务最大模型/工具循环轮数，默认 `20`。
- `CCPY_REQUEST_TIMEOUT_S`：模型请求超时时间，默认 `500` 秒。
- `CCPY_MODEL_MAX_CONCURRENCY`：模型请求最大并发，默认 `1`。低 RPM/低并发网关建议保持 `1`。
- `CCPY_MODEL_MIN_INTERVAL_S`：模型请求最小间隔。自测网关限制为 20 RPM，设为 `3.5`。
- `CCPY_MODEL_MAX_RETRIES`：模型限流/临时错误最大重试次数，默认 `6`。
- `CCPY_MODEL_RETRY_BASE_DELAY_S`：指数退避基础等待时间，默认 `1` 秒。
- `CCPY_MODEL_RETRY_MAX_DELAY_S`：单次重试最大等待时间，默认 `30` 秒。
- `CCPY_PROJECT_MEMORY`：是否自动注入项目记忆，默认 `1`。设为 `0` 可关闭。
- `CCPY_PROJECT_MEMORY_MAX_CHARS`：项目记忆注入的最大字符数，默认 `20000`。
- `CCPY_MEMORY_INDEX`：是否生成 `.ccpy/memories` manifest 注入提示词，默认 `1`。
- `CCPY_SESSION_NOTES`：是否为当前 session 维护 `.notes.md` 摘要文件，默认 `1`。
- `CCPY_COMPACT_MAX_TOKENS`：触发 compact 的本地 token 估算阈值，默认 `20000`。
- `CCPY_COMPACT_RECENT_MESSAGES`：snip compact 保留的最近消息数，默认 `24`。
- `CCPY_COMPACT_TOOL_RESULT_MAX_CHARS`：旧工具结果 micro compact 的字符上限，默认 `8000`。
- `CCPY_COMPACT_REACTIVE`：模型返回 prompt-too-long/413 时是否进行 reactive compact 并重试，默认 `1`。
- `CCPY_HOOKS`：是否启用内部 HookManager，默认 `1`。
- `CCPY_MCP_SERVERS`：MCP server JSON 配置，可为列表或 `{ "servers": [...] }`。
- `CCPY_SESSION_DIR`：会话 JSONL 保存目录，默认 `.ccpy/sessions`。
- `CCPY_TASK_OUTPUT_DIR`：后台任务输出目录，默认 `.ccpy/task-outputs`。
- `CCPY_SHELL`：自定义 shell 可执行文件。

不要把 API Key 提交到 Git 仓库。建议使用环境变量或本地 `.env`，并确保 `.env` 已被 `.gitignore` 忽略。

## 上下文与记忆管理

CCPY 参考 Claude Code 和 Claw Agent 的 context/memory 设计，当前实现了渐进增强版本：

- JSONL 会话记录：每次运行会写入 `.ccpy/sessions/sess_xxx.jsonl`。
- Session notes：每次运行会维护 `.ccpy/sessions/sess_xxx.notes.md`，记录最新用户目标、assistant 摘要和已观察工具。
- Micro compact：裁剪较旧的大型 tool result，最近 5 个工具结果保持完整。
- Snip compact：当本地 token 估算超过阈值时，保留 system、session notes 摘要和最近消息尾部。
- Reactive compact：当模型网关返回 prompt-too-long/413 类错误时，会进一步压缩上下文并重试。
- 项目级记忆注入：启动时自动读取工作区中的记忆文件、`MEMORY.md` 索引和 memory manifest，并追加到 system prompt。
- Sub-agent 上下文隔离：sub agent 使用独立 runner depth，但共享模型客户端、任务管理器和工具统计。

自动读取的项目记忆文件：

```text
CLAUDE.md
AGENTS.md
.ccpy/memory.md
.ccpy/memories/MEMORY.md
.ccpy/memories/*.md
```

推荐用法：

```markdown
# AGENTS.md

- 默认使用中文回复。
- 修改代码后运行 pytest。
- 不要提交 .env、.ccpy/、API Key。
```

如果需要关闭项目记忆注入：

```powershell
$env:CCPY_PROJECT_MEMORY="0"
```

和 Claude Code 相比，CCPY 仍未实现完整的长期记忆系统，例如：

- 完整 resume 会话恢复。
- 基于 parent UUID 的严格 transcript 回放。
- 后台 session memory 子代理自动抽取。
- 模型生成式 auto-compact 摘要。
- 大型 tool result 落盘后按需引用。

这些是后续可继续补齐的方向。

## 快速使用

```powershell
ccpy run --workspace D:\Agent\Claude-Code-Python "查看这个项目的 Python 包结构，并总结核心模块"
```

运行工具链验证：

```powershell
ccpy run --workspace D:\Agent\Claude-Code-Python "使用 Glob 查看 Python 文件，然后用 Bash 执行 python -m pytest，最后总结结果"
```

如果不需要指定工作区，也可以使用快捷写法：

```powershell
ccpy "回复 OK"
```

## MCP 工具接入

CCPY 现在保留两种 MCP 接入方式：

- `--mcp-manifest`：兼容早期 HTTP manifest，适合简单自定义工具服务。
- `--mcp-config` / `CCPY_MCP_SERVERS`：加载 MCP stdio、SSE 或 streamable HTTP server，并将工具注册为 `mcp__<server>__<tool>`。

安装官方 MCP 依赖：

```powershell
python -m pip install -e ".[mcp]"
```

示例 `mcp.json`：

```json
{
  "servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "your_mcp_server"]
    }
  ]
}
```

运行：

```powershell
ccpy run --workspace D:\Agent\Claude-Code-Python --mcp-config .\mcp.json "列出可用 MCP 工具并调用一个只读工具"
```

## 如何解读工具调用统计

每次运行结束后，CLI 会输出工具统计，用来证明模型是否真的调用了工具、是否真的使用了 sub agent：

```text
Top-level tool calls: Agent=3
Autonomous Agent/Task used: True
Global executed tools: Agent=3, Glob=2, Read=3, Bash=1
Sub-agent executed tools: Glob=2, Read=3, Bash=1
```

含义：

- `Top-level tool calls`：只统计父 agent 这一层的 assistant tool calls。出现 `Agent=3` 表示父 agent 启动了 3 个 sub agents。
- `Autonomous Agent/Task used`：父 agent 是否调用了 `Agent` 或 `Task`。这用于判断是否使用了 sub agent。
- `Global executed tools`：整个运行期间实际执行过的所有工具，包括父 agent 和 sub agents。
- `Sub-agent executed tools`：只统计 sub agents 内部实际执行过的工具。这里出现 `Read`、`Glob`、`Bash` 才能证明 sub agent 不只是返回文本，而是确实调用了工具辅助完成任务。

如果输出类似：

```text
Top-level tool calls: Bash=7, Write=8
Autonomous Agent/Task used: False
Global executed tools: Bash=7, Write=8
```

说明模型确实使用了普通工具完成任务，但没有使用 sub agent。

## Task Tool 使用 Demo

```powershell
ccpy demo task --workspace D:\Agent\Claude-Code-Python
```

这个 demo 是“强制 Task Tool 功能演示”。它会明确提示父 agent 在同一轮中发起多个 `Agent` 工具调用，例如：

- researcher 子代理：查看项目结构。
- tester 子代理：运行安全的 Bash 检查。
- reviewer 子代理：总结风险与后续测试建议。

父 agent 会收集子代理结果并输出最终报告。只要模型在同一轮返回多个 `Agent` tool calls，runner 就可以并行调度这些任务。

## 自主 Sub-Agent 验证 Demo

```powershell
ccpy demo autonomous --workspace D:\Agent\Claude-Code-Python
```

这个 demo 不会在用户 prompt 中明确要求“使用 Agent/Task”。它只给主 agent 一个通用复杂任务：

```text
准备公开 GitHub 发布，检查项目结构、运行合适的本地检查、识别风险并输出发布准备报告。
```

运行结束后，CLI 会打印本次真实工具调用统计：

```text
Top-level tool calls: Agent=2
Autonomous Agent/Task used: True
Global executed tools: Agent=2, Bash=1, Glob=1, Read=2
Sub-agent executed tools: Bash=1, Glob=1, Read=2
```

如果 `Autonomous Agent/Task used=True`，说明模型在通用任务下自主选择了 `Agent`/`Task` 子代理；如果为 `False`，说明模型只用了普通工具，未自主分派子代理。这比 `ccpy demo task` 更适合验证“主 agent 是否会主动产生子 agent 任务请求”。

真实验证中观察到：在当前 Kimi-K2.6/New API 通道下，`demo autonomous` 可能输出 `Global executed tools: Bash=5, Read=10`，但 `Autonomous Agent/Task used=False`。这说明模型会自主调用普通工具，但未必会在没有明确要求时自主分派 sub agent。这是模型策略结果，不代表框架不支持 sub agent。

## Multi-Agent System Demo

```powershell
ccpy demo multi-agent --workspace D:\Agent\Claude-Code-Python
```

该 demo 展示 coordinator/worker 模式：父 agent 作为协调者，分派 coder、tester、reviewer 等 worker agent，并通过 `TaskOutput` 汇总后台任务结果。

当前 `multi-agent` demo 为了更容易验证真实 tool calling，使用同步 `Agent` 子代理而不是后台任务。它会明确要求模型在同一轮中调用三次 `Agent`：

- `coder`：检查源码并提出一个小实现改进。
- `tester`：检查测试并提出一个测试改进。
- `reviewer`：检查 README 和验证报告中的发布风险。

如果模型不能调用工具，它应输出 `TOOL_CALLING_NOT_AVAILABLE`。成功时 CLI 应显示类似：

```text
Top-level tool calls: Agent=3
Global executed tools: Agent=3, Glob=2, Read=3, Bash=1
Sub-agent executed tools: Glob=2, Read=3, Bash=1
```

其中 `Top-level tool calls` 证明父 agent 调用了 sub agents；`Sub-agent executed tools` 证明 sub agents 内部确实继续调用了工具辅助完成任务。

## Multi-Agent System 展示方式

本项目可以通过三种方式展示“特别的 multi-agent system”，分别覆盖强制子代理、角色化协调和自主分派实验。

### 1. Task Fan-Out 并行子代理

命令：

```powershell
ccpy demo task --workspace D:\Agent\Claude-Code-Python
```

展示点：

- 父 agent 在同一轮发起多个 `Agent` tool calls。
- 子代理按角色分工，例如 researcher、tester、reviewer。
- 父 agent 收集子代理结果并汇总。

成功证据：

```text
Top-level tool calls: Agent=3
Global executed tools: Agent=3, ...
Sub-agent executed tools: ...
```

这证明 Task Tool 能支持并行多任务。

### 2. Coordinator / Worker 工程团队

命令：

```powershell
ccpy demo multi-agent --workspace D:\Agent\Claude-Code-Python
```

展示点：

- 父 agent 扮演 coordinator。
- 子 agent 扮演 worker：
  - `coder`：检查源码并提出实现改进。
  - `tester`：检查测试并提出测试改进。
  - `reviewer`：检查 README / 验证报告并提出发布风险。
- 父 agent 汇总 coder、tester、reviewer 的结果，形成工程报告。

成功证据：

```text
Top-level tool calls: Agent=3
Autonomous Agent/Task used: True
Global executed tools: Agent=3, Glob=2, Read=3, Bash=1
Sub-agent executed tools: Glob=2, Read=3, Bash=1
```

这证明父 agent 不只是调用了子代理，而且子代理内部也实际调用了工具。

### 3. Autonomous Delegation 自主分派实验

命令：

```powershell
ccpy demo autonomous --workspace D:\Agent\Claude-Code-Python
```

展示点：

- prompt 不明确要求使用 `Agent` / `Task`。
- 主 agent 需要自行判断是否值得使用 sub agent。
- 该 demo 用来观察模型是否具备自主任务分派倾向。

判断方式：

```text
Autonomous Agent/Task used: True
```

表示模型在通用任务下自主选择了 sub agent。

如果输出类似：

```text
Top-level tool calls: Bash=5, Read=10
Autonomous Agent/Task used: False
Global executed tools: Bash=5, Read=10
```

说明模型会自主调用普通工具，但在该任务中没有自主分派 sub agent。这是模型策略结果，不代表框架不支持 sub agent。

### 可扩展方向：Release Team

后续可以扩展一个更完整的 `ccpy demo release-team`：

- `planner`：拆解任务和验收标准。
- `coder`：实现代码修改。
- `tester`：运行 pytest 并提出修复建议。
- `reviewer`：检查发布风险。
- `coordinator`：汇总并给出发布决策。

这个模式更接近真实软件工程团队，可用于展示更复杂的 multi-agent workflow。

## 默认工具列表


| 工具                | 说明                                       |
| ----------------- | ---------------------------------------- |
| `Read`            | 读取工作区内文本文件，支持 offset/limit               |
| `Write`           | 写入工作区内文件                                 |
| `Edit`            | 替换文件中的指定文本                               |
| `Glob`            | 按 glob pattern 查找文件                      |
| `Grep`            | 用正则搜索文件内容                                |
| `Bash`            | 执行 shell 命令，支持 timeout 和后台任务             |
| `TodoWrite`       | 创建或更新结构化 todo 列表                         |
| `WebFetch`        | 拉取 URL 文本内容                              |
| `WebSearch`       | 使用 Tavily 或 DuckDuckGo instant answer 搜索 |
| `Agent` / `Task`  | 启动子代理，支持同步和后台运行                          |
| `TaskOutput`      | 读取后台任务状态和输出                              |
| `TaskStop`        | 停止后台任务                                   |
| `SendMessage`     | 给后台 agent 队列发送消息                         |
| `AskUserQuestion` | 生成需要用户回答的问题                              |
| `ExitPlanMode`    | 输出计划内容                                   |


## Task / Agent Tool 输入示例

模型会通过 OpenAI-compatible tool calling 生成类似输入：

```json
{
  "description": "分析测试覆盖",
  "prompt": "检查 tests 目录，总结当前测试覆盖了哪些核心能力。",
  "subagent_type": "tester",
  "run_in_background": false,
  "allowed_tools": ["Read", "Glob", "Grep", "Bash"]
}
```

后台任务示例：

```json
{
  "description": "后台审查项目风险",
  "prompt": "作为 reviewer 子代理，审查项目结构和 README，给出风险清单。",
  "subagent_type": "reviewer",
  "run_in_background": true
}
```

后台任务启动后，可用 `TaskOutput` 获取结果，或用 `TaskStop` 取消任务。

## 真实模型验证

如果你有真实 Kimi-k2.6 API Key，可按以下顺序验证：

```powershell
cd D:\Agent\Claude-Code-Python
python -m pip install -e ".[dev]"

$env:CCPY_BASE_URL="https://your-openai-compatible-endpoint/v1"
$env:CCPY_API_KEY="your_api_key"
$env:CCPY_MODEL="kimi-k2.6"
$env:CCPY_MODEL_MAX_CONCURRENCY="1"
$env:CCPY_MODEL_MIN_INTERVAL_S="3.5"

ccpy "回复 OK，并说明当前模型是否支持 tool calling"
ccpy run --workspace D:\Agent\Claude-Code-Python "使用 Glob 查看 Python 文件，然后用 Bash 执行 python -m pytest，最后总结结果"
ccpy demo task --workspace D:\Agent\Claude-Code-Python
ccpy demo autonomous --workspace D:\Agent\Claude-Code-Python
ccpy demo multi-agent --workspace D:\Agent\Claude-Code-Python
```

预期现象：

- 模型能正常返回文本。
- 模型能根据工具 schema 生成 `tool_calls`。
- `Bash` 能返回 `exit_code`、`stdout` 和 `stderr`。
- `Agent`/`Task` 能启动子代理并返回结果。
- `.ccpy/sessions/` 中产生 JSONL 会话记录。
- `.ccpy/task-outputs/` 中产生后台任务输出文件。

如果模型只回复普通文本而不调用工具，需要确认你的 K2.6 接口是否支持 OpenAI-compatible tool calling。

## 本地测试

不需要真实 API Key 的测试：

```powershell
python -m pytest
python -m compileall src tests examples benchmarks
python -m claude_code_python.cli --help
```

当前自动化测试覆盖工具调用、Task/sub-agent、New API 兼容、限流、超时重试、tool_call_id 规范化和 sub-agent 工具统计。最近一次结果：

```text
22 passed
```

当前验证报告见：

```text
VALIDATION_REPORT.md
```

## Terminal-Bench Smoke

项目提供一个轻量 smoke 入口：

```powershell
python benchmarks\terminalbench_smoke.py
```

它用于验证 CLI、真实模型和 Bash 工具能否完成一个简单 terminal agent 任务。它不是完整 Terminal-Bench harness，但可作为后续接入完整评测集的起点。

## 安全说明

- 所有文件路径都会解析到配置的 workspace 内，默认禁止访问 workspace 外路径。
- `Bash` 工具包含基础危险命令拦截，例如 `rm -rf /`、`shutdown`、`diskpart` 等。
- 这不是沙箱环境。运行真实模型时，请在隔离目录、容器或临时仓库中测试。
- 不要让模型处理未授权的敏感文件、凭据或生产环境命令。
- 不要提交 `.env`、API Key、会话输出或任务输出中的敏感内容。

## 与 Claude Code 的关系

Claude-Code-Python 不是 Anthropic 官方 Claude Code，也不是逐字节复制实现。它是一个 Python 版本的工程化复刻，保留核心思想：

- 工具 schema 驱动模型调用。
- 多轮工具执行循环。
- 严格 tool result 配对。
- 保守并发调度。
- Task/sub-agent 多代理协作。
- Bash 与文件系统工具组合完成真实编程任务。

