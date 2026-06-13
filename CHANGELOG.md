# Changelog

All notable changes to loop-hermes will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - 2026-06-13

### 新增 (Added)

- **核心状态机**: 文件驱动状态机，支持 state.json 全生命周期管理
  - 原子写入协议（tmp → fsync → rename → fsync dir）
  - .lock 文件并发安全管理（僵尸锁自动清理，超时 300s）
  - 自动备份机制（写入前自动 state.json → state.json.bak）
  - 损坏恢复（state.json 不可读时从 .bak 恢复）
  - Default-FAIL 合约：任何异常导致写入失败时不吞错

- **Checksum 协议**: SHA-256 三层校验体系
  - 第 1 层：文件内容 → SHA-256 哈希
  - 第 2 层：version 单调递增计数器
  - 第 3 层：Sanity Check 启动时全量校验

- **Phase 分发器**: 11 个 phase 的完整调度链路
  - Part 1 设计气泡（1.1-1.3 同进程内顺序推进，支持内部回退）
  - Part 2 实施链路（各子阶段独立调用）
  - repair_context 协议（null → active → consumed 状态机）
  - Hard gate 验证闸门（Part 2.8）

- **路由引擎**: P0/P1/P2 多级检测与决策树
  - P0 检测 → Part 1 重新设计
  - P1 决策树（C1-C5 设计级条件 + N1-N4 否定条件）
  - P2 检测 → Part 2.2 repair 模式
  - convergence_counter 5 优先级操作表
  - 路由重复检测与自动暂停

- **Hermes 客户端**: 双路径调用抽象
  - SDK 路径（Python AIAgent 直接调用）
  - CLI 路径（subprocess hermes chat -q）
  - 自动引擎检测与降级
  - Guardrail 事件解析（SDK 和 CLI 双重支持）

- **Provider 回退管理器**: LLM 调用的容错机制
  - 优先级链：Claude → OpenAI → DeepSeek
  - 独立 failure_counter（5 次失败自动降级）
  - 恢复探测（每 5 分钟尝试恢复）
  - 熔断器保护（CIRCUIT_OPEN 后 10 分钟冷却）
  - 线程安全（threading.Lock）
  - 全局单例模式

- **并行委派管理器**: 多 sub-agent 并行执行
  - Thread-based 并行（BoundedSemaphore 控制最大并发）
  - fail-fast 模式（任一 agent 失败立即取消其余）
  - 单 agent 超时 + 总体超时双重保护
  - 结果合并（去重、冲突检测、guardrail 聚合）
  - Sub-agent 工作目录隔离

- **Guardrail 映射器**: Hermes 安全事件 → Issue 转换
  - HARDLINE/HARDLINE_BLOCK → P0
  - WARN/WARN_PATTERN → P1
  - APPROVAL_DENY/APPROVAL_TIMEOUT → P2
  - 终止级事件自动标记工作流失败

- **Sanity Check**: 15 项启动检查
  - Python 版本、Hermes 可用性、文件系统权限
  - 数据完整性（JSON 合法性、Schema 兼容性）
  - 配置校验（mode、provider、循环参数）
  - Artifact checksum 全量验证

- **Monitor 侧车**: 独立监控守护进程
  - 心跳检测、进程存活检查、资源使用监控
  - state.json 周期性完整性校验
  - 僵尸子进程检测、磁盘爆炸预警

- **Scheduler 调度器**: HLOOP_STATE 解析 + 外部调度参考实现
  - HLOOP_STATE block 解析器（key:value 和 JSON 双格式）
  - 四层级联终止判定
  - crontab / Task Scheduler / launchd 配置生成

- **四种运行模式**: safe / auto / unsafe / collaborative
- **配置管理**: 模式、provider 优先级链、API keys 统一管理
- **JSON Schema 校验**: 6 套完整 Schema（state / task_list / test_results / issue_list / gate_state / repair_context）
- **PyInstaller 构建脚本**: 跨平台打包支持
- **完整测试套件**: 各模块单元测试 + 黄金路径测试 + 边界测试 + 性能基准

### 变更 (Changed)

- 首个正式版本，无历史变更。

### 弃用 (Deprecated)

- 无。

### 移除 (Removed)

- 无。

### 修复 (Fixed)

- 首个正式版本，无历史修复。

### 安全 (Security)

- 原子写入协议防止 state.json 损坏
- Checksum 协议防止 artifact 文件篡改
- Guardrail 事件映射为可路由 issue
- 终止级 Guardrail 自动终止工作流
- Provider 熔断器防止级联故障
- 闸门文件数量阈值（按模式分级）
- 不可逆操作拦截（safe/auto 模式）
- 僵尸锁自动清理防止死锁
