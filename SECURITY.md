# Security Policy

## 支持的版本

| 版本   | 支持状态           |
|--------|--------------------|
| 0.1.x  | :white_check_mark: 活跃支持 |

## 报告安全漏洞

如果您发现安全漏洞，请**不要**创建公开 Issue。

请发送邮件至项目维护者，包含以下信息：

1. 漏洞的详细描述
2. 复现步骤
3. 受影响版本
4. 建议的修复方案（如有）

我们将在 48 小时内确认收到报告，并在 7 天内提供初步评估。

## 安全架构

### 1. 原子写入协议

loop-hermes 的状态文件写入经过三层保护，防止数据损坏：

```
JSON 序列化 → 写入 .tmp → flush + fsync → 原子 rename → fsync 目录
```

- **写入前自动备份**：每次写入前自动复制 state.json → state.json.bak
- **Default-FAIL 合约**：任何步骤失败均抛出异常，确保调用方感知写入失败
- **崩溃安全**：最坏情况下留下 .tmp 残留文件，可安全删除

### 2. Checksum 篡改检测

所有 artifact 文件通过 SHA-256 三层校验防止未经授权的修改：

- **第 1 层**：文件内容 → SHA-256 哈希 → 记录到 state.json
- **第 2 层**：version 单调递增计数器（每次更新 +1）
- **第 3 层**：启动时全量校验（Sanity Check #15）

如果检测到 checksum 不匹配，系统将：
- 记录警告日志
- 返回不匹配详情（artifact 名称、记录值、实际值）
- 建议手动检查受影响的文件

### 3. Guardrail 安全映射

Hermes Agent 的安全 guardrail 事件会被映射为可路由的 issue：

| Guardrail 类型       | 严重性 | 处置动作               |
|----------------------|--------|------------------------|
| HARDLINE             | P0     | 回退到 Part 1 重新设计 |
| HARDLINE_BLOCK       | P0     | 终止工作流             |
| WARN                 | P1     | 进入决策树             |
| WARN_PATTERN         | P1     | 进入决策树             |
| APPROVAL_DENY        | P2     | 触发 repair 模式       |
| APPROVAL_TIMEOUT     | P2     | 触发 repair 模式       |
| BLOCK                | P0     | 终止工作流             |

### 4. Provider 熔断器

防止因外部 LLM 服务故障导致的级联故障：

- 每个 provider 独立 failure counter
- 5 次连续失败自动降级
- 降级后每 5 分钟尝试恢复探测（不会无限重试）
- CIRCUIT_OPEN 后等待 10 分钟冷却期
- 所有 provider 不可用时触发致命错误退出

### 5. 闸门文件数量阈值

按运行模式限制单次循环的文件修改数量：

| 模式    | 文件数量阈值 |
|---------|-------------|
| safe    | 3           |
| auto    | 10          |
| unsafe  | 999（几乎不限制）|

### 6. 不可逆操作拦截

在 safe 和 auto 模式下，以下操作被拦截：

- `rm -rf /` 及类似危险命令
- `git push --force` 到主分支
- `DROP TABLE` / `DROP DATABASE`
- `chmod 777` 递归设置
- `format` / `mkfs` 磁盘操作
- 环境变量 `PATH` / `HOME` 修改

### 7. 并发安全

- 文件级锁（.lock 文件）确保 state.json 写入的互斥性
- 僵尸锁自动清理（超时 300 秒）
- 锁文件记录持有进程 PID 便于调试

### 8. 敏感信息保护

- API keys 通过环境变量注入，不在配置文件中存储
- 日志中不记录 API key 内容
- state.json 中不包含 API key

## API Key 管理最佳实践

```bash
# 推荐：通过环境变量设置（不存储在文件中）
export ANTHROPIC_API_KEY="sk-ant-..."

# 推荐：使用密钥管理服务
# AWS Secrets Manager / GCP Secret Manager / Azure Key Vault

# 不推荐：硬编码在代码或配置文件中
# ANTHROPIC_API_KEY = "sk-ant-..."  # 绝对不要这样做
```

## 依赖安全

定期检查依赖漏洞：

```bash
# 使用 pip-audit
pip install pip-audit
pip-audit

# 使用 safety
pip install safety
safety check

# 使用 bandit 进行代码安全扫描
python run_bandit.py
```

## 已知安全限制

1. **state.json 无加密**：状态文件为明文 JSON，敏感项目信息可能泄露。后续版本计划支持 at-rest 加密。
2. **文件系统级别安全**：loop-hermes 不提供文件系统级别的访问控制，依赖操作系统的权限管理。
3. **网络传输无加密**：与 LLM provider 的通信依赖其 SDK 的 TLS 实现。请确保使用 HTTPS 端点。
4. **Sub-agent 隔离**：并行 sub-agent 共享同一文件系统，未实现 sandbox 隔离。

## 安全检查清单

在部署 loop-hermes 到生产环境前，请确认：

- [ ] 所有 API keys 通过环境变量注入，未硬编码
- [ ] 运行模式设置为 safe 或 auto（不使用 unsafe 模式）
- [ ] state_dir 目录权限设置为仅当前用户可读写
- [ ] 已运行 `python run_bandit.py` 且无高危发现
- [ ] 已运行 `pytest tests/ -v` 确保所有测试通过
- [ ] 已配置监控侧车（monitor）以检测异常
- [ ] 审查了闸门阈值配置（文件数量、不可逆操作）
- [ ] 已阅读并理解 Provider 回退链的最大重试配置

## 联系方式

安全相关问题请联系项目维护者。
