# -*- coding: utf-8 -*-
"""G1 内容安全门。

在 Hermes Agent 每次返回输出后，扫描输出内容中是否存在
不安全的内容：恶意代码注入、PII 泄露、系统级攻击指令等。

闸门等级: L3（safe/auto 模式阻塞，unsafe 模式仅记录）
触发时机: Hermes Agent 返回 output 后，写入 artifact 之前。
处置动作: 阻塞不安全内容进入 artifact，向 state 注入 P0 issue。

Pattern 分层:
    - CRITICAL: 无条件拦截（如 rm -rf /、fork bomb）
    - HIGH: safe/auto 模式拦截（如 sudo、chmod 777）
    - MEDIUM: 仅警告（如 curl pipe bash 无验证）
"""

import re
import logging
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("loop_hermes.gate_g1")

# ============================================================================
# G1 闸门配置常量
# ============================================================================

# 闸门 ID
GATE_ID = "G1"

# 严重等级
SEVERITY_LEVELS = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})

# ========================================
# CRITICAL 模式 —— 无条件拦截
# ========================================

CRITICAL_PATTERNS: List[Tuple[str, str, str]] = [
    # (正则表达式, 标签, 人类可读描述)
    # 擦除系统
    (r"rm\s+-rf\s+/", "RM_RF_ROOT",
     "尝试递归擦除根目录"),
    (r"rm\s+-rf\s+--no-preserve-root\s+/", "RM_RF_ROOT_FORCE",
     "尝试强制擦除根目录"),
    (r"dd\s+if=/dev/(zero|random|urandom)\s+of=/dev/sd[a-z]", "DD_WIPE_DISK",
     "尝试擦除磁盘"),
    (r"mkfs\.[a-z]+\s+/dev/sd[a-z]", "MKFS_FORMAT",
     "尝试格式化磁盘设备"),
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;:", "FORK_BOMB",
     "Fork bomb 攻击"),

    # 系统破坏
    (r"chmod\s+(-R\s+)?777\s+/(etc|usr|var|bin|sbin|lib|boot)", "CHMOD_ROOT",
     "尝试修改关键系统目录权限为 777"),
    (r"mv\s+/(etc|boot|usr/bin)\s+/dev/null", "MV_SYSTEM_DIR",
     "尝试移动关键系统目录到 /dev/null"),

    # 恶意注入 —— Python
    (r"__import__\s*\(\s*['\"]os['\"]\s*\)\s*\.\s*system\s*\(\s*['\"]rm\s+-rf", "PY_OS_SYSTEM_RM",
     "Python 代码注入：os.system 执行 rm 命令"),
    (r"exec\s*\(\s*__import__\s*\(\s*['\"]base64['\"]", "PY_EXEC_B64",
     "Python 代码注入：base64 解码后 exec"),
    (r"subprocess\.(call|run|Popen)\s*\(\s*\[\s*['\"]/bin/bash", "SUBPROCESS_BASH",
     "Python subprocess 执行 shell"),

    # 恶意注入 —— Shell
    (r"curl\s+.*\|\s*(/bin/)?(ba)?sh", "CURL_PIPE_SHELL",
     "curl 管道到 shell 执行"),
    (r"wget\s+.*-O\s*-\s*\|\s*(/bin/)?(ba)?sh", "WGET_PIPE_SHELL",
     "wget 管道到 shell 执行"),

    # 网络窃取
    (r"nc\s+-[lL]\s+-[pP]\s+\d+\s+-[eE]\s+/bin/(ba)?sh", "NC_BACKDOOR",
     "netcat 反向 shell 后门"),
    (r"bash\s+-i\s+>&\s+/dev/tcp/", "BASH_REVERSE_SHELL",
     "bash TCP 反向 shell"),
    (r"python\s+-c\s+.*socket\.socket.*connect", "PY_REVERSE_SHELL",
     "Python 反向 shell"),
]

# ========================================
# HIGH 模式 —— safe/auto 拦截
# ========================================

HIGH_PATTERNS: List[Tuple[str, str, str]] = [
    (r"sudo\s+(rm|dd|chmod|chown|mkfs)", "SUDO_DANGEROUS",
     "通过 sudo 执行危险命令"),
    (r"chmod\s+(-R\s+)?777", "CHMOD_777",
     "设置文件为全局可写 (777)"),
    (r"chown\s+-R\s+\w+:\w+\s+/etc", "CHOWN_ETC",
     "递归修改 /etc 目录所有权"),
    (r"iptables\s+-F", "IPTABLES_FLUSH",
     "清空防火墙规则"),
    (r"systemctl\s+(disable|mask)\s+(sshd|firewalld|ufw)", "SYSTEMCTL_SECURITY",
     "禁用安全服务"),
    (r"pip\s+install\s+--break-system-packages", "PIP_BREAK_SYSTEM",
     "pip 破坏系统包安装"),
    (r"npm\s+install\s+-g\s+.*--unsafe-perm", "NPM_UNSAFE_PERM",
     "npm 全局安装 unsafe-perm"),
    (r"docker\s+run\s+.*--privileged", "DOCKER_PRIVILEGED",
     "特权 Docker 容器运行"),
    (r"eval\s*\(\s*(request\.|params\.|input\s*\()", "EVAL_USER_INPUT",
     "eval 用户输入（代码注入）"),
    (r"\.\.\/\.\.\/\.\.\/(etc\/passwd|etc\/shadow|proc\/self)", "PATH_TRAVERSAL_SYSTEM",
     "路径穿越访问系统敏感文件"),
]

# ========================================
# MEDIUM 模式 —— 仅警告
# ========================================

MEDIUM_PATTERNS: List[Tuple[str, str, str]] = [
    (r"os\.system\s*\(['\"]", "OS_SYSTEM",
     "Python 调用 os.system"),
    (r"subprocess\.(call|run|Popen)\s*\(\s*['\"]", "SUBPROCESS_SHELL",
     "Python subprocess 执行 shell 字符串"),
    (r"pickle\.load", "PICKLE_LOAD",
     "反序列化 pickle（RCE 风险）"),
    (r"yaml\.load\s*\(.*(?!Loader=)", "YAML_UNSAFE_LOAD",
     "PyYAML 不安全 load（RCE 风险）"),
    (r"eval\s*\(['\"]", "EVAL_STR",
     "eval 字符串执行"),
    (r"request\.args\[|request\.form\[", "FLASK_ARGS",
     "Flask 直接取用户输入（XSS/SQLi 风险）"),
    (r"\$\{.*\}|\$\(.*\)", "SHELL_EXPANSION",
     "Shell 变量/命令展开（注入风险）"),
    (r"\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}", "SHELLCODE_HEX",
     "疑似 shellcode 十六进制编码"),
]


# ============================================================================
# G1 闸门核心逻辑
# ============================================================================


def scan_content(
    content: str,
    mode: str = "auto",
) -> Dict[str, Any]:
    """扫描 Hermes 返回内容中的安全风险。

    按严重等级逐层匹配，找到第一个匹配即记录。
    CRITICAL 无条件返回 blocked=True，
    HIGH 在 safe/auto 模式返回 blocked=True，
    MEDIUM 仅记录不阻塞。

    Args:
        content: Hermes Agent 返回的文本内容
        mode: 运行模式（safe / auto / unsafe / collaborative）

    Returns:
        {
            "gate_id": "G1",
            "passed": bool,         # 是否通过内容安全检查
            "blocked": bool,        # 是否应阻塞（取决于模式和严重等级）
            "findings": [           # 匹配到的安全问题列表
                {
                    "pattern_tag": str,
                    "severity": str,
                    "description": str,
                    "match_snippet": str,  # 匹配到的文本片段（≤80 chars）
                }
            ],
            "timestamp": str,       # ISO 8601 时间戳
        }
    """
    findings: List[Dict[str, str]] = []

    # 逐层扫描
    for severity_level, patterns in [
        ("CRITICAL", CRITICAL_PATTERNS),
        ("HIGH", HIGH_PATTERNS),
        ("MEDIUM", MEDIUM_PATTERNS),
    ]:
        for pattern, tag, description in patterns:
            match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
            if match:
                snippet = match.group(0)
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                findings.append({
                    "pattern_tag": tag,
                    "severity": severity_level,
                    "description": description,
                    "match_snippet": snippet,
                })

    # 判定闸门结果
    has_critical = any(f["severity"] == "CRITICAL" for f in findings)
    has_high = any(f["severity"] == "HIGH" for f in findings)

    blocked = False
    if has_critical:
        blocked = True  # CRITICAL: 无条件拦截
    elif has_high and mode in ("safe", "auto", "collaborative"):
        blocked = True  # HIGH: safe/auto/collaborative 模式拦截

    passed = not blocked

    result = {
        "gate_id": GATE_ID,
        "passed": passed,
        "blocked": blocked,
        "findings": findings,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if findings:
        logger.warning(
            "G1 发现 %d 个安全问题（blocked=%s, mode=%s）",
            len(findings), blocked, mode,
        )
        for f in findings:
            logger.warning("  [%s] %s: %s", f["severity"], f["pattern_tag"], f["description"])
    else:
        logger.debug("G1 内容安全检查通过")

    return result


def inject_g1_issues_into_state(
    state: dict,
    scan_result: Dict[str, Any],
) -> int:
    """将 G1 扫描结果中的安全问题注入 state 的 issue 列表。

    每个 CRITICAL/HIGH finding 转换为一个 P0 issue。
    每个 MEDIUM finding 转换为一个 P2 issue。

    Args:
        state: state 字典（原地修改）
        scan_result: scan_content() 的返回结果

    Returns:
        注入的 issue 数量
    """
    import uuid

    findings = scan_result.get("findings", [])
    if not findings:
        return 0

    phase = state["progress"].get("phase", "unknown")
    count = 0

    for finding in findings:
        severity = finding["severity"]
        if severity == "MEDIUM":
            issue_severity = "P2"
        else:
            issue_severity = "P0"

        issue = {
            "id": f"g1-{uuid.uuid4().hex[:8]}",
            "severity": issue_severity,
            "title": f"G1 内容安全风险: {finding['pattern_tag']}",
            "description": (
                f"G1 内容安全扫描发现 {finding['severity']} 级风险。\n"
                f"模式: {finding['pattern_tag']}\n"
                f"描述: {finding['description']}\n"
                f"匹配片段: {finding.get('match_snippet', 'N/A')}"
            ),
            "source": "hermes_guardrail",
            "source_ref": f"gate_g1@{finding['pattern_tag']}",
            "discovered_in_phase": phase,
            "status": "open",
            "affected_files": [],
            "linked_task_ids": [],
            "fix_strategy": (
                "CRITICAL/HIGH: 重新生成内容，需显式安全审查。"
                if issue_severity == "P0" else
                "MEDIUM: 人工审查确认安全后可放行。"
            ),
        }

        sev_key = issue_severity.lower()
        state["issues"]["active"][sev_key].append(issue)
        state["issues"]["all_time"][f"{sev_key}_total"] += 1
        count += 1

    if count > 0:
        state["progress"]["new_issues_this_round"] = True

    logger.info("G1 注入 %d 个 issue 到 state", count)
    return count


# ============================================================================
# 高层接口
# ============================================================================


def run_gate_g1(
    content: str,
    state: dict,
) -> Dict[str, Any]:
    """运行 G1 内容安全门完整流程。

    1. 扫描内容安全风险
    2. 如果 blocked，注入 P0 issue 并设置 gate_state
    3. 返回扫描结果

    Args:
        content: Hermes 返回的输出内容
        state: state 字典（原地修改）

    Returns:
        完整的扫描结果字典（含是否阻断信息）
    """
    mode = state.get("config", {}).get("mode", "auto")
    scan_result = scan_content(content, mode)

    # 更新 gate_state
    gate = state.setdefault("gate_state", {})
    gate["content_safety_passed"] = scan_result["passed"]

    # 如果被阻断，注入 issue
    if scan_result["blocked"]:
        inject_g1_issues_into_state(state, scan_result)

    return scan_result
