# loop-hermes — Autonomous Development Loop for Hermes Agent

**Your AI coding agent with a steering wheel.** Turn "I want X" into "X is done" — hands-free, end-to-end, no manual orchestration.

[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-0.1.0-blue)](https://github.com/PerryLink/loop-hermes)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

**loop-hermes autonomously orchestrates Hermes Agent through end-to-end development loops — set a goal and walk away.**

[English](#english) | [中文](#中文)

---

## English

### 🚀 Features

- 🔄 **Full Autonomous Loop** — 11-phase workflow (3 design + 8 execution), from natural-language goal to verified completion
- 🛡️ **Guardrail Integration** — Hermes Agent HARDLINE/WARN events automatically mapped to P0/P1/P2 issue severities
- 🔀 **Intelligent Routing** — P0 fatal → redesign / P1 decision tree (5 positive + 6 negative conditions) → design-level or fix / P2 → repair mode
- ✅ **Convergence Detection** — Three-layer termination: issue clearance + convergence counter + max-cycle cap
- 🏛️ **File-Driven State Machine** — Atomic writes (tmp→fsync→rename→fsync-dir), SHA-256 checksum, automatic backups, crash recovery
- 🚦 **Four Trust Levels** — safe (all gates) / auto (default) / unsafe (minimal) / interactive (collaborative with 30min timeout)
- 🔁 **Provider Fallback Chain** — Claude → OpenAI → DeepSeek with per-provider failure counters, circuit breaker, and auto-recovery probes
- 📊 **Monitor Sidecar** — Independent watchdog process for heartbeat, memory/CPU/disk, zombie detection, and artifact integrity
- 🔀 **Parallel Delegation** — Union-Find clustering for independent tasks, batch-parallel Hermes sub-agent dispatch with conflict detection
- 🧪 **Comprehensive QA** — Golden-path tests, edge-case tests, performance benchmarks, bandit security scanning, PyInstaller build

### ⚡ Quick Start

#### Prerequisites

- **Python** >= 3.10
- **Hermes Agent** (SDK or CLI)
- At least one LLM Provider API key (Anthropic / OpenAI / DeepSeek)

#### Install

```bash
git clone https://github.com/PerryLink/loop-hermes.git
cd loop-hermes
pip install -r requirements.txt
```

#### Set API Keys

```bash
# Set at least one
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."
```

#### Run

```bash
# Initialize a project directory with your goal
python -m loop_hermes.cli --init --goal "Create a weather CLI tool"

# Single-shot execution
python -m loop_hermes.cli --no-pause --goal "Create a weather CLI tool"

# Continuous loop mode (built-in scheduler, every 2 minutes)
python -m loop_hermes.scheduler --interval 120

# Safety mode — all gates active, pauses for confirmation
python -m loop_hermes.cli --safe --goal "Refactor user auth module"
```

### Trust Modes

| Mode | Flag | Behavior |
|------|------|----------|
| **safe** | `--safe` | All gates active, pauses for solution confirmation |
| **auto** | `--auto` (default) | Solution auto-approved, dangerous ops paused |
| **unsafe** | `--unsafe` | Only content-safety + catastrophic hard-blocks |
| **interactive** | `--interactive` | Part 1 decisions await confirmation, 30min timeout auto-degrade |

### CLI Reference

```
python -m loop_hermes.cli [OPTIONS]

Options:
  --init                    Initialize project directory
  --check                   Run 15-point sanity check
  --state-dir DIR           State directory (default: .hermes/loop-hermes/)
  --goal TEXT               User goal description
  --mode MODE               Trust mode (safe/auto/unsafe/interactive)
  --safe / --auto / --unsafe / --interactive
  --provider-fallback LIST  Custom fallback chain (comma-separated)
  --max-cycles N            Max loop cycles (default: 5)
  --convergence-rounds N    Rounds needed for convergence (default: 2)
  --hermes-model MODEL      Hermes model (default: claude-sonnet-4-20250514)
  --skip-testing            Skip test phases
  --no-pause                Don't pause for user confirmation
  --json-output             Output results as JSON
```

### ❓ FAQ

**Q: What's the difference between using loop-hermes and Hermes Agent directly?**

A: Hermes Agent is the engine — it provides tool-calling, guardrails, and sub-agent delegation. loop-hermes is the steering wheel — it orchestrates Hermes through a full autonomous loop with state management, phase routing, issue-to-guardrail mapping, and convergence detection. You set a goal once and walk away.

**Q: Can it run on a headless server?**

A: Yes. Use `--no-pause` mode with the built-in scheduler (`python -m loop_hermes.scheduler --interval 120`). It works with cron, systemd timers, or Windows Task Scheduler.

**Q: What if all LLM providers fail?**

A: Each provider has independent failure counters (5 consecutive failures trigger auto-degrade). After all providers are exhausted, the system emits a fatal error and exits cleanly with a state snapshot for recovery.

**Q: Is my code safe from unwanted changes?**

A: In `--safe` mode, every design decision and dangerous operation pauses for your approval. In `--auto` mode, only high-risk operations (file deletion outside scope, dependency changes, network access) trigger a pause. You can always `git revert` any changes. The monitor sidecar provides real-time visibility.

**Q: How many cycles should I set?**

A: Default 5 covers most simple-to-medium tasks. For complex projects, set `--max-cycles 10` with `--convergence-rounds 3`. The convergence counter automatically terminates when no new issues are detected for the required number of consecutive rounds — you don't need to guess the exact number.

### 🔗 Related Projects

- **[loop-aider](https://github.com/PerryLink/loop-aider)** — Autonomous driving layer for Aider CLI (subprocess-based)
- **[loop-claudecode](https://github.com/PerryLink/loop-claudecode)** — Autonomous loop for Claude Code CLI
- **[loop-copilot](https://github.com/PerryLink/loop-copilot)** — Autonomous loop for GitHub Copilot
- **[loop-cursor](https://github.com/PerryLink/loop-cursor)** — Autonomous loop for Cursor IDE agent
- **[loop-deepseek](https://github.com/PerryLink/loop-deepseek)** — Autonomous loop for DeepSeek Coder
- **[loop-opencode](https://github.com/PerryLink/loop-opencode)** — Autonomous loop for OpenCode CLI
- **[loop-codex](https://github.com/PerryLink/loop-codex)** — Autonomous loop for OpenAI Codex CLI
- **[loop-ollama](https://github.com/PerryLink/loop-ollama)** — Autonomous loop for Ollama local models
- **[loop-superpowers](https://github.com/PerryLink/loop-superpowers)** — Autonomous loop for Superpowers-enhanced agents
- **[loop-everything](https://github.com/PerryLink/loop-everything)** — Meta-loop orchestrating multiple AI coding tools

### 📄 License

Apache License 2.0 — Copyright 2026 Perry Link.

See [LICENSE](LICENSE) for the full text.

---

## 中文

**loop-hermes** 是一款面向 Hermes Agent 的自主开发循环引擎——将"我要 X"自动转化为"X 已完成"，全程无人干预。

**loop-hermes 全自动编排 Hermes Agent 完成端到端开发循环——设定目标，即可走开。**

### 🚀 功能特性

- 🔄 **全自主循环** — 11 阶段工作流（3 设计 + 8 执行），从自然语言目标到验证完成
- 🛡️ **护栏集成** — Hermes Agent 的 HARDLINE/WARN 事件自动映射为 P0/P1/P2 问题严重级别
- 🔀 **智能路由** — P0 致命→重新设计 / P1 决策树（5 正向+6 反向条件）→设计级或修复 / P2→修复模式
- ✅ **收敛检测** — 三层终止机制：问题清零 + 收敛计数器 + 最大循环上限
- 🏛️ **文件驱动状态机** — 原子写入（tmp→fsync→rename→fsync-dir），SHA-256 校验，自动备份，崩溃恢复
- 🚦 **四种信任模式** — safe（全门控）/ auto（默认）/ unsafe（最小门控）/ interactive（协作模式，30分钟超时）
- 🔁 **Provider 容灾链** — Claude → OpenAI → DeepSeek，每 provider 独立故障计数、断路器、自动恢复探测
- 📊 **Monitor 侧车** — 独立守护进程，提供心跳、内存/CPU/磁盘、僵尸检测和产物完整性监控
- 🔀 **并行委托** — Union-Find 聚类识别独立任务，批量并行 Hermes 子代理调度，冲突检测
- 🧪 **全面质量保障** — 黄金路径测试、边界测试、性能基准、bandit 安全扫描、PyInstaller 构建

### ⚡ 快速开始

#### 前置条件

- **Python** >= 3.10
- **Hermes Agent**（SDK 或 CLI）
- 至少一个 LLM Provider API Key（Anthropic / OpenAI / DeepSeek）

#### 安装

```bash
git clone https://github.com/PerryLink/loop-hermes.git
cd loop-hermes
pip install -r requirements.txt
```

#### 设置 API Key

```bash
# 至少设置一个
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."
```

#### 运行

```bash
# 初始化项目目录并设定目标
python -m loop_hermes.cli --init --goal "创建一个天气 CLI 工具"

# 单次执行
python -m loop_hermes.cli --no-pause --goal "创建一个天气 CLI 工具"

# 持续循环模式（内置调度器，每 2 分钟一次）
python -m loop_hermes.scheduler --interval 120

# 安全模式 — 所有门控激活，暂停等待确认
python -m loop_hermes.cli --safe --goal "重构用户认证模块"
```

### 信任模式

| 模式 | 参数 | 行为 |
|------|------|------|
| **safe** | `--safe` | 全部门控激活，暂停等待方案确认 |
| **auto** | `--auto`（默认） | 方案自动批准，危险操作暂停 |
| **unsafe** | `--unsafe` | 仅内容安全 + 灾难性硬阻断 |
| **interactive** | `--interactive` | Part 1 决策等待确认，30 分钟超时自动降级 |

### CLI 参考

```
python -m loop_hermes.cli [OPTIONS]

选项:
  --init                    初始化项目目录
  --check                   运行 15 点健全性检查
  --state-dir DIR           状态目录（默认: .hermes/loop-hermes/）
  --goal TEXT               用户目标描述
  --mode MODE               信任模式 (safe/auto/unsafe/interactive)
  --safe / --auto / --unsafe / --interactive
  --provider-fallback LIST  自定义容灾链（逗号分隔）
  --max-cycles N            最大循环次数（默认: 5）
  --convergence-rounds N    收敛所需轮次（默认: 2）
  --hermes-model MODEL      Hermes 模型（默认: claude-sonnet-4-20250514）
  --skip-testing            跳过测试阶段
  --no-pause                不暂停等待用户确认
  --json-output             以 JSON 格式输出结果
```

### ❓ 常见问题

**Q: loop-hermes 和直接使用 Hermes Agent 有什么区别？**

A: Hermes Agent 是引擎——提供工具调用、护栏和子代理委托。loop-hermes 是方向盘——通过状态管理、阶段路由、问题到护栏映射和收敛检测，编排 Hermes 完成完整的自主循环。你只需设定目标，然后放手。

**Q: 能否在无头服务器上运行？**

A: 可以。使用 `--no-pause` 模式配合内置调度器（`python -m loop_hermes.scheduler --interval 120`）。支持 cron、systemd timers 或 Windows 任务计划程序。

**Q: 如果所有 LLM Provider 都失败了怎么办？**

A: 每个 Provider 都有独立的故障计数器（连续 5 次失败触发自动降级）。所有 Provider 耗尽后，系统会发出致命错误并干净退出，同时保存状态快照以便恢复。

**Q: 我的代码能避免不必要的修改吗？**

A: 在 `--safe` 模式下，每个设计决策和危险操作都会暂停等待你的批准。在 `--auto` 模式下，只有高风险操作（范围外文件删除、依赖变更、网络访问）会触发暂停。你可以随时使用 `git revert` 撤销任何更改。Monitor 侧车提供实时可见性。

**Q: 应该设置多少个循环？**

A: 默认 5 轮覆盖大多数简单到中等复杂度任务。对于复杂项目，设置 `--max-cycles 10` 和 `--convergence-rounds 3`。收敛计数器在连续达到所需轮次未检测到新问题时自动终止——你不需要猜测确切数字。

### 🔗 相关项目

- **[loop-aider](https://github.com/PerryLink/loop-aider)** — Aider CLI 自动驾驶层（基于子进程）
- **[loop-claudecode](https://github.com/PerryLink/loop-claudecode)** — Claude Code CLI 自主循环
- **[loop-copilot](https://github.com/PerryLink/loop-copilot)** — GitHub Copilot 自主循环
- **[loop-cursor](https://github.com/PerryLink/loop-cursor)** — Cursor IDE Agent 自主循环
- **[loop-deepseek](https://github.com/PerryLink/loop-deepseek)** — DeepSeek Coder 自主循环
- **[loop-opencode](https://github.com/PerryLink/loop-opencode)** — OpenCode CLI 自主循环
- **[loop-codex](https://github.com/PerryLink/loop-codex)** — OpenAI Codex CLI 自主循环
- **[loop-ollama](https://github.com/PerryLink/loop-ollama)** — Ollama 本地模型自主循环
- **[loop-superpowers](https://github.com/PerryLink/loop-superpowers)** — Superpowers 增强代理自主循环
- **[loop-everything](https://github.com/PerryLink/loop-everything)** — 编排多个 AI 编码工具的元循环

### 📄 许可证

Apache License 2.0 — Copyright 2026 Perry Link.

详见 [LICENSE](LICENSE) 获取完整文本。
