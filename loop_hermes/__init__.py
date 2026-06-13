# -*- coding: utf-8 -*-
"""loop-hermes —— Hermes Agent 自动驾驶开发闭环。

设定一个目标，Hermes Agent 自动驾驶完成"设计→实施→测试→验证"的全闭环。
基于目标条件收敛驱动，配合监控侧车，把"我想要 X"变成"X 已完成"。

Public API:
    cli.main()                  — CLI 入口（--init, --check, --mode, --provider, --requirement）。
    config.LoopHermesConfig     — 全局配置数据类（模式、provider 优先级、API keys）。
    schemas.HermesSchemas       — JSON Schema 定义（state.json / gate_state / repair_context）。
    state_machine.StateMachine  — 状态机核心（原子写入、Default-FAIL 合约、checksum 协议）。
    hermes_client.HermesClient  — Hermes 客户端抽象层（SDK/CLI 双路径）。
    platform_config.PlatformConfig — 平台配置（Windows/Linux/macOS 差异）。
    sanity_check.SanityCheck    — Sanity Check 15 项启动检查。
    phase_dispatch.PhaseDispatcher — 11 阶段分派引擎（prompt 模板 + 路由决策）。
    scheduler.Scheduler         — 调度器——串联所有模块自主闭环。
    routing.Router              — P0/P1/P2 三层路由决策系统。
    provider_fallback.ProviderFallbackManager — Provider 回退管理器（指数退避）。
    gate_guard.GateGuard        — 6 安全 Gate 入口控制（G1-G6, G4 5 层分类器）。
    guardrail_mapper.GuardrailMapper — 安全护栏映射器（122+ 命令模式）。
    monitor.Monitor             — 监控侧车（审计日志 + SHA-256 链式哈希）。
    parallel_manager.ParallelManager — 并行执行管理器。
    checksum.ChecksumManager    — 文件校验和协议管理器。
    build.Builder               — PyInstaller 构建打包器。
"""

__version__ = "0.1.0"
__author__ = "loop-hermes contributors"
__license__ = "Apache-2.0"
