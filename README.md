# loop-hermes

*A [**Loop Engineering**](https://github.com/PerryLink/loop-everything) autonomous coding loop engine — turn goals into production code.*

> Production-grade autonomous coding — wrap Hermes SDK, set a goal, walk away.

[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![CI](https://github.com/PerryLink/loop-hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/PerryLink/loop-hermes/actions)

**LLMO Entity Definition**: This project is an **autonomous coding loop engine** that **wraps the Hermes SDK with 24 source modules, 6 Gate implementations, Provider Fallback dual-layer management, ParallelDelegateManager Union-Find clustering, and SHA-256 3-layer checksum**, optimized for **production-grade autonomous development** using **Python 3.10+ and the Hermes SDK**.


---

## ✨ Core Features

- **24 Source Modules** -- Full-stack autonomous loop: `cli`, `state_machine`, `phase_dispatch`, `routing`, `scheduler`, `hermes_client`, `guardrail_mapper`, `sanity_check`, `checksum`, `config`, `platform_config`, `schemas`, `build`, `monitor`, `parallel_manager`, `provider_fallback`, `gate_guard`, plus 6 gate modules (G1-G6)
- **6 Gate Implementations** -- G1 Content Safety (CRITICAL/HIGH/MEDIUM pattern matching), G2 Plan Confirmation (safe/auto/unsafe/collaborative tiered approval), G3 Dependency Install (typosquatting detection, system-wide interception), G4 Dangerous Operations (L0-L4 5-layer command matcher), G5 File Changes (per-mode change threshold enforcement), G6 Completion Gate (7-condition hard quality gate)
- **Provider Fallback Dual-Layer Management** -- Anthropic -> OpenAI -> DeepSeek priority chain with per-provider failure counters (5 consecutive failures trigger auto-degrade), circuit breaker pattern, and 5-minute recovery probes. A separate top-level `provider_fallback_manager.py` provides full LLM adapters, while the internal `provider_fallback` module manages the availability state machine.
- **ParallelDelegateManager Union-Find Clustering** -- Groups independent tasks via Union-Find connectivity analysis, dispatches them to Hermes sub-agents in parallel batches with configurable max concurrency, monitors timeouts, merges results, and supports fail-fast mode.
- **SHA-256 3-Layer Checksum** -- Layer 1: file content SHA-256 hashing; Layer 2: monotonic version counter per artifact; Layer 3: full integrity audit via Sanity Check #15 on startup. Change detection and tamper-proofing across all phases.
- **11-Phase Autonomous Loop** -- 3 design phases (understand, plan, solution) + 8 execution phases (gate-check, implement, verify, test, fix, audit, hard-gate, convergence). Natural-language goal to verified completion, hands-free.
- **Guardrail-to-Severity Mapping** -- Hermes SDK HARDLINE events map to P0 (fatal, trigger redesign), WARN events map to P1/P2 (decision-tree routing: 5 positive + 6 negative conditions).
- **Convergence Detection** -- Three-layer termination: issue clearance + convergence counter + max-cycle cap. Automatically stops when no new issues are detected for the required number of consecutive rounds.
- **File-Driven State Machine** -- Atomic writes (tmp -> fsync -> rename -> fsync-dir), crash recovery from state snapshots, automatic backups.
- **Monitor Sidecar** -- Independent watchdog process for heartbeat, memory/CPU/disk, zombie detection, and artifact integrity monitoring.
- **Four Trust Modes** -- `safe` (all gates, pause for confirmation), `auto` (default, dangerous ops paused), `unsafe` (catastrophic-only), `interactive` (collaborative with 30min timeout).

---

## 🚀 Quick Start

### Prerequisites

- **Python** >= 3.10
- **Hermes SDK** installed and configured
- At least one LLM Provider API key (Anthropic / OpenAI / DeepSeek)

### Install

```bash
pip install loop-hermes
```

### Set API Keys

```bash
# Set at least one
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."
```

### Run

```bash
# Single-shot autonomous execution
loop-hermes run --goal "Create a weather CLI tool in Python"

# Safety mode — all gates active, pauses for confirmation
loop-hermes run --safe --goal "Refactor user auth module"

# Continuous loop mode (built-in scheduler, every 2 minutes)
loop-hermes scheduler --interval 120

# Initialize a new project directory
loop-hermes init
```

### CLI Reference

```
loop-hermes [COMMAND] [OPTIONS]

Commands:
  run           Start autonomous loop execution
  init          Initialize project directory
  check         Run 15-point sanity check
  scheduler     Continuous loop mode with configurable interval

Options:
  --goal TEXT               User goal description
  --mode MODE               Trust mode (safe/auto/unsafe/interactive)
  --safe / --auto / --unsafe / --interactive
  --provider-fallback LIST  Custom fallback chain (comma-separated)
  --max-cycles N            Max loop cycles (default: 5)
  --convergence-rounds N    Rounds needed for convergence (default: 2)
  --skip-testing            Skip test phases
  --no-pause                Don't pause for user confirmation
  --json-output             Output results as JSON
```

---

## 🙋 FAQ

**Q: What is the difference between using loop-hermes and the Hermes SDK directly?**

A: Hermes SDK is the engine — it provides tool-calling, guardrails, and sub-agent delegation. loop-hermes is the steering wheel — it orchestrates Hermes through a full autonomous loop with state management, phase routing, guardrail-to-severity mapping, 6 safety gates, and three-layer convergence detection. You set a goal once and walk away.

**Q: Can it run on a headless server?**

A: Yes. Use `--no-pause` mode with the built-in scheduler (`loop-hermes scheduler --interval 120`). It works with cron, systemd timers, or Windows Task Scheduler.

**Q: What happens if all LLM providers fail?**

A: Each provider has an independent failure counter (5 consecutive failures trigger auto-degrade to the next provider). The circuit breaker pattern prevents thrashing. Recovery probes run every 5 minutes to restore higher-priority providers. If all providers are exhausted, the system emits a fatal error and exits cleanly with a state snapshot for recovery.

**Q: Is my code safe from unwanted changes?**

A: In `--safe` mode, every design decision and dangerous operation pauses for your approval. G4 (Dangerous Operations Gate) provides L0-L4 5-layer command classification — from safe read-only (L0) to always-blocked catastrophic commands (L4). G5 (File Changes Gate) enforces per-mode change thresholds. You can always `git revert` any changes. The monitor sidecar provides real-time visibility.

**Q: How many cycles should I set?**

A: The default of 5 cycles covers most simple-to-medium tasks. For complex projects, set `--max-cycles 10` with `--convergence-rounds 3`. The convergence counter automatically terminates when no new issues are detected for the required number of consecutive rounds — you don't need to guess the exact number.

---

## 🌐 Related Projects

- **[loop-aider](https://github.com/PerryLink/loop-aider)** -- Autonomous driving layer for Aider CLI (subprocess-based)
- **[loop-antigravity](https://github.com/PerryLink/loop-antigravity)** -- Autonomous loop for Google Antigravity / Gemini
- **[loop-claudecode](https://github.com/PerryLink/loop-claudecode)** -- Autonomous loop for Claude Code CLI
- **[loop-codex](https://github.com/PerryLink/loop-codex)** -- Autonomous loop for OpenAI Codex CLI
- **[loop-copilot](https://github.com/PerryLink/loop-copilot)** -- Autonomous loop for GitHub Copilot
- **[loop-cursor](https://github.com/PerryLink/loop-cursor)** -- Autonomous loop for Cursor IDE agent
- **[loop-deepseek](https://github.com/PerryLink/loop-deepseek)** -- Autonomous loop for DeepSeek Coder
- **[loop-hermes](https://github.com/PerryLink/loop-hermes)** -- Autonomous coding loop for Hermes SDK
- **[loop-ollama](https://github.com/PerryLink/loop-ollama)** -- Autonomous loop for Ollama local models
- **[loop-opencode](https://github.com/PerryLink/loop-opencode)** -- Autonomous loop for OpenCode CLI
- **[loop-openclaw](https://github.com/PerryLink/loop-openclaw)** -- Multi-agent config generator for OpenClaw Gateway
- **[loop-superpowers](https://github.com/PerryLink/loop-superpowers)** -- Autonomous loop for Superpowers-enhanced agents

---

## 📄 License

Apache License 2.0 © 2026 Perry Link. See [LICENSE](LICENSE) for the full text.

---

## 中文说明

**loop-hermes** 是一款面向 Hermes SDK 的生产级自主编码循环引擎。通过封装 Hermes SDK，提供 24 个源码模块、6 个 Gate 实现、Provider Fallback 双层管理、ParallelDelegateManager Union-Find 聚类以及 SHA-256 三层校验，将"我要 X"自动转化为"X 已完成"。

### 核心特性

- **24 个源码模块** -- 全栈自主循环：CLI 入口、状态机、阶段派发、智能路由、调度器、Hermes 客户端、护栏映射、健全检查、校验和、配置管理、平台配置、Schema 定义、构建打包、监控侧车、并行委派（Union-Find）、Provider 容错及 6 个闸门模块
- **6 个 Gate 实现** -- G1 内容安全门（CRITICAL/HIGH/MEDIUM 三级模式匹配）、G2 计划确认门（safe/auto/unsafe/collaborative 分级确认）、G3 依赖安装门（typosquatting 检测、系统级安装拦截）、G4 危险操作门（L0-L4 五层命令匹配器）、G5 文件变更门（按模式设定变更阈值）、G6 完成门（7 项硬性质量检查）
- **Provider Fallback 双层管理** -- Anthropic → OpenAI → DeepSeek 优先级链，每 provider 独立故障计数器（5 次连续失败自动降级），断路器模式，5 分钟恢复探测。顶层 `provider_fallback_manager.py` 提供完整 LLM 适配器，内部 `provider_fallback` 模块管理可用性状态机
- **ParallelDelegateManager Union-Find 聚类** -- 通过 Union-Find 连通性分析对独立任务分组，批量并行派发至 Hermes sub-agent，支持可配置最大并发数、超时监控、结果合并及 fail-fast 模式
- **SHA-256 三层校验** -- 第 1 层：文件内容 SHA-256 哈希；第 2 层：单调递增版本计数器；第 3 层：启动时 Sanity Check #15 全量完整性审计。跨阶段变更检测与防篡改
- **11 阶段自主循环** -- 3 设计阶段（理解、规划、方案）+ 8 执行阶段（闸门检查、实施、验证、测试、修复、审计、硬门、收敛）。自然语言目标到验证完成，全程无人干预
- **护栏-严重级别映射** -- Hermes SDK HARDLINE 事件映射为 P0（致命，触发重新设计），WARN 事件映射为 P1/P2（决策树路由：5 正向 + 6 反向条件）
- **收敛检测** -- 三层终止机制：问题清零 + 收敛计数器 + 最大循环上限。连续多轮无新问题时自动停止
- **文件驱动状态机** -- 原子写入（tmp → fsync → rename → fsync-dir），崩溃恢复，自动备份
- **Monitor 侧车进程** -- 独立看门狗：心跳、内存/CPU/磁盘、僵尸检测、产物完整性监控
- **四种信任模式** -- `safe`（全闸门，暂停确认）、`auto`（默认，危险操作暂停）、`unsafe`（仅灾难性拦截）、`interactive`（协作模式，30 分钟超时）

### 快速开始

```bash
pip install loop-hermes

# 设置 API 密钥
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DEEPSEEK_API_KEY="sk-..."

# 单次自主执行
loop-hermes run --goal "创建一个天气 CLI 工具"

# 安全模式
loop-hermes run --safe --goal "重构用户认证模块"

# 持续循环模式
loop-hermes scheduler --interval 120
```

### 常见问题

**Q: loop-hermes 和直接使用 Hermes SDK 有什么区别？**

A: Hermes SDK 是引擎，提供工具调用、护栏和子代理委派。loop-hermes 是方向盘，通过状态管理、阶段路由、护栏-严重级别映射、6 个安全闸门和三层收敛检测来编排 Hermes 完成完整的自主循环。设定一次目标即可离开。

**Q: 可以在无头服务器上运行吗？**

A: 可以。使用 `--no-pause` 模式配合内置调度器（`loop-hermes scheduler --interval 120`）。兼容 cron、systemd timer 和 Windows 任务计划程序。

**Q: 所有 LLM Provider 都失败了怎么办？**

A: 每个 Provider 有独立的故障计数器（连续 5 次失败自动降级到下一个 Provider）。断路器模式防止反复重试。每 5 分钟运行恢复探测以恢复高优先级 Provider。若所有 Provider 耗尽，系统发出致命错误并干净退出，保留状态快照以便恢复。

**Q: 我的代码安全吗？**

A: 在 `--safe` 模式下，每个设计决策和危险操作都会暂停等待你的批准。G4（危险操作门）提供 L0-L4 五层命令分类——从安全的只读命令（L0）到无条件拦截的灾难性命令（L4）。G5（文件变更门）按模式强制执行变更阈值。你可以随时 `git revert` 任何变更。Monitor 侧车提供实时可见性。

### 关联项目

- **[loop-aider](https://github.com/PerryLink/loop-aider)** -- Aider CLI 自主驾驶层（子进程模式）
- **[loop-antigravity](https://github.com/PerryLink/loop-antigravity)** -- Google Antigravity / Gemini 自主循环
- **[loop-claudecode](https://github.com/PerryLink/loop-claudecode)** -- Claude Code CLI 自主循环
- **[loop-codex](https://github.com/PerryLink/loop-codex)** -- OpenAI Codex CLI 自主循环
- **[loop-copilot](https://github.com/PerryLink/loop-copilot)** -- GitHub Copilot 自主循环
- **[loop-cursor](https://github.com/PerryLink/loop-cursor)** -- Cursor IDE Agent 自主循环
- **[loop-deepseek](https://github.com/PerryLink/loop-deepseek)** -- DeepSeek Coder 自主循环
- **[loop-hermes](https://github.com/PerryLink/loop-hermes)** -- Hermes SDK 自主编码循环
- **[loop-ollama](https://github.com/PerryLink/loop-ollama)** -- Ollama 本地模型自主循环
- **[loop-opencode](https://github.com/PerryLink/loop-opencode)** -- OpenCode CLI 自主循环
- **[loop-openclaw](https://github.com/PerryLink/loop-openclaw)** -- OpenClaw Gateway 多代理配置生成器
- **[loop-superpowers](https://github.com/PerryLink/loop-superpowers)** -- Superpowers 增强代理自主循环

### 许可证

Apache License 2.0 © 2026 Perry Link。详见 [LICENSE](LICENSE)。
