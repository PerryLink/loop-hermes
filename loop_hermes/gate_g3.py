# -*- coding: utf-8 -*-
"""G3 依赖安装门。

在 Hermes Agent 尝试安装依赖（pip/npm/apt 等）时，
验证安装命令的安全性：包名合法性、注册表可信度、
typosquatting 检测、system-wide 安装拦截。

闸门等级: L3（safe/auto 模式拦截高风险依赖，unsafe 仅警告）
触发时机: Hermes 执行 pip install / npm install 等命令前。
处置动作: 拦截可疑安装命令，注入 P1 issue。

检测维度:
    1. 包名 Typosquatting 检测（Levenshtein 距离）
    2. System-wide 安装拦截（--break-system-packages / sudo pip）
    3. 未知注册表来源检测（非 PyPI/npm 官方）
    4. 包版本 pin 检查（鼓励精确 pin）
"""

import re
import logging
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Set, Tuple

logger = logging.getLogger("loop_hermes.gate_g3")

# ============================================================================
# G3 闸门常量
# ============================================================================

GATE_ID = "G3"

# 已知可信注册表
TRUSTED_REGISTRIES = {
    "pypi.org", "pypi.python.org",
    "registry.npmjs.org",
    "rubygems.org",
    "crates.io",
}

# 已知受信任的顶级 Python 包（前 100 常用包）
TRUSTED_PYPI_PACKAGES: Set[str] = {
    "numpy", "pandas", "requests", "flask", "django", "fastapi",
    "sqlalchemy", "pytest", "black", "ruff", "mypy", "pydantic",
    "click", "rich", "typer", "uvicorn", "gunicorn", "celery",
    "redis", "psycopg2", "asyncpg", "aiosqlite", "boto3",
    "google-cloud-storage", "azure-storage-blob", "httpx",
    "aiohttp", "starlette", "jinja2", "markupsafe", "python-dotenv",
    "pyyaml", "toml", "tomli", "jsonschema", "marshmallow",
    "cryptography", "bcrypt", "passlib", "python-jose", "pyjwt",
    "opentelemetry-api", "structlog", "loguru",
    "tensorflow", "torch", "scikit-learn", "scipy", "matplotlib",
    "pillow", "opencv-python", "transformers", "datasets",
    "langchain", "openai", "anthropic", "tiktoken",
    "aiofiles", "watchfiles", "websockets", "grpcio",
    "protobuf", "orjson", "ujson", "msgpack",
    "sentry-sdk", "prometheus-client",
    "pydantic-settings", "python-multipart",
    "alembic", "pytest-asyncio", "pytest-cov", "coverage",
    "pre-commit", "isort", "autoflake",
    "polars", "duckdb", "pyarrow",
    "httptools", "uvloop", "orjson",
}

# 已知受信任的顶级 npm 包
TRUSTED_NPM_PACKAGES: Set[str] = {
    "react", "react-dom", "next", "vue", "angular", "svelte",
    "express", "koa", "fastify", "hapi",
    "lodash", "axios", "moment", "dayjs", "date-fns",
    "typescript", "eslint", "prettier", "webpack", "vite",
    "jest", "mocha", "chai", "playwright", "puppeteer",
    "tailwindcss", "bootstrap", "sass", "postcss",
    "prisma", "typeorm", "sequelize", "mongoose",
    "graphql", "apollo-server", "urql",
    "zustand", "redux", "mobx", "jotai",
    "three", "d3", "chart.js",
    "socket.io", "ws", "uuid",
    "commander", "yargs", "chalk", "ora",
    "dotenv", "cross-env", "nodemon", "ts-node",
    "@nestjs/core", "@angular/core",
}

# 高风险包名特征（typosquatting 变体检测）
SUSPICIOUS_PACKAGE_NAME_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r"^(pytorch|tensor[-_]?flow|numpy|pandas|requests)[-_]?(utils|helper|extra|lib|sdk)",
        r"^(flask|django|fastapi|express)[-_]?(core|server|app|api)",
        r"^djangoo?$", r"^reque?sts$", r"^pythoon$",
        r"^sele?nium$", r"^beautil?fulsoup",
        r"^jqurey$", r"^lodas?h$",
        r".*[-_]?(sdk|api|utils|lib).*",  # 过度泛化
    ]
]

# 高危安装命令特征
DANGEROUS_INSTALL_PATTERNS: List[Tuple[str, str, str]] = [
    (r"sudo\s+pip\s+install", "SUDO_PIP",
     "使用 sudo 运行 pip install（可能覆盖系统包）"),
    (r"pip\s+install\s+--break-system-packages", "BREAK_SYSTEM_PKGS",
     "pip --break-system-packages 标志（危险）"),
    (r"pip\s+install\s+\.\s*$", "PIP_INSTALL_DOT",
     "pip install .（安装当前目录，依赖未审计）"),
    (r"npm\s+install\s+-g\s+.*--unsafe-perm", "NPM_UNSAFE_GLOBAL",
     "npm 全局安装 --unsafe-perm"),
    (r"npm\s+install\s+--ignore-scripts\s*$", "NPM_IGNORE_SCRIPTS",
     "npm --ignore-scripts 可能安装恶意脚本包"),
    (r"gem\s+install\s+--no-ri\s+--no-rdoc", "GEM_NODOC",
     "gem install 无文档模式（包审计薄弱）"),
    (r"cargo\s+install\s+--git\s+https?://(?!github\.com|gitlab\.com)", "CARGO_UNKNOWN_GIT",
     "cargo install 非主流 Git 来源"),
]

# 可疑注册表 URL 模式
SUSPICIOUS_REGISTRY_PATTERNS = [
    re.compile(r, re.IGNORECASE) for r in [
        r"https?://(?!pypi\.org|files\.pythonhosted\.org)[^/]+/simple/",
        r"https?://(?!registry\.npmjs\.org)[^/]+/npm/",
        r"--index-url\s+https?://(?!pypi\.org)[^/\s]+",
        r"--registry\s+https?://(?!registry\.npmjs\.org)[^/\s]+",
        r"--extra-index-url\s+https?://",
    ]
]


# ============================================================================
# Levenshtein 距离计算
# ============================================================================


def _levenshtein_distance(s1: str, s2: str) -> int:
    """计算两个字符串之间的 Levenshtein 编辑距离。

    用于 typosquatting 检测：计算候选包名与已知受信任
    包名之间的最小编辑距离。

    Args:
        s1: 字符串 1
        s2: 字符串 2

    Returns:
        编辑距离（整数）
    """
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insert = previous_row[j + 1] + 1
            delete = current_row[j] + 1
            substitute = previous_row[j] + (c1 != c2)
            current_row.append(min(insert, delete, substitute))
        previous_row = current_row

    return previous_row[-1]


# ============================================================================
# G3 包名验证
# ============================================================================


def check_package_name(name: str, ecosystem: str = "pypi") -> Dict[str, Any]:
    """检查单个包名的安全性。

    检测维度:
        1. Typosquatting: 高编辑距离相似度 + 不在已知信任名单
        2. 高风险 namemasking: 包名伪装成常用包
        3. 过短/过长包名

    Args:
        name: 包名
        ecosystem: 包生态系统（pypi / npm / cargo / gem）

    Returns:
        {
            "package_name": str,
            "ecosystem": str,
            "trusted": bool,
            "suspicious": bool,
            "warnings": [str],
        }
    """
    warnings: List[str] = []
    trusted = False
    suspicious = False

    trusted_set = (
        TRUSTED_PYPI_PACKAGES if ecosystem == "pypi"
        else TRUSTED_NPM_PACKAGES if ecosystem == "npm"
        else set()
    )

    # 检查是否在已知信任名单
    if name in trusted_set:
        return {
            "package_name": name,
            "ecosystem": ecosystem,
            "trusted": True,
            "suspicious": False,
            "warnings": [],
        }

    # Typosquatting 检测
    for known in trusted_set:
        if len(known) < 3:
            continue
        dist = _levenshtein_distance(name, known)
        max_len = max(len(name), len(known))
        if max_len == 0:
            continue
        similarity = 1.0 - (dist / max_len)

        # 高相似度但名字不同 → 疑似 typosquatting
        if similarity >= 0.8 and name != known and dist <= 2:
            warnings.append(
                f"包名 '{name}' 与已知信任包 '{known}' 高度相似 "
                f"(相似度={similarity:.0%}, 编辑距离={dist})，疑似 typosquatting"
            )
            suspicious = True
            break

    # 检查是否匹配可疑命名模式
    for pattern in SUSPICIOUS_PACKAGE_NAME_PATTERNS:
        if pattern.search(name):
            warnings.append(f"包名 '{name}' 匹配可疑命名模式")
            suspicious = True
            break

    # 长度检查
    if len(name) < 2:
        warnings.append(f"包名 '{name}' 过短（{len(name)} 字符），不可信")
        suspicious = True
    if len(name) > 50:
        warnings.append(f"包名 '{name}' 过长（{len(name)} 字符），不可信")
        suspicious = True

    return {
        "package_name": name,
        "ecosystem": ecosystem,
        "trusted": trusted,
        "suspicious": suspicious,
        "warnings": warnings,
    }


# ============================================================================
# G3 命令解析
# ============================================================================


def extract_packages_from_pip_command(cmd: str) -> List[str]:
    """从 pip install 命令中提取包名列表。

    Args:
        cmd: pip install 命令字符串

    Returns:
        提取到的包名列表
    """
    packages: List[str] = []

    # 匹配 pip install 后面的包名
    # 排除标志参数（以 - 或 -- 开头）
    words = cmd.split()
    in_packages = False
    for i, w in enumerate(words):
        if w in ("pip", "pip3", "python", "-m") and i + 1 < len(words):
            if words[i + 1] == "install":
                in_packages = True
                continue
        if w == "install" and words[i - 1] in ("pip", "pip3"):
            in_packages = True
            continue
        if in_packages:
            if w.startswith("-"):
                # 跳过带值的长标志
                if "=" in w:
                    continue
                continue
            if w in ("&&", "||", "|", ";", ">", "<"):
                break
            # 提取包名（去掉版本约束）
            pkg = re.split(r"[=<>!~;]", w)[0]
            pkg = pkg.strip().strip("'").strip('"')
            if pkg and not pkg.startswith("-") and pkg != ".":
                packages.append(pkg)

    return packages


def extract_packages_from_npm_command(cmd: str) -> List[str]:
    """从 npm install 命令中提取包名列表。

    Args:
        cmd: npm install 命令字符串

    Returns:
        提取到的包名列表
    """
    packages: List[str] = []
    words = cmd.split()
    in_packages = False
    for i, w in enumerate(words):
        if w == "npm" and i + 1 < len(words) and words[i + 1] == "install":
            in_packages = True
            continue
        if w == "install" and i > 0 and words[i - 1] == "npm":
            in_packages = True
            continue
        if in_packages:
            if w.startswith("-"):
                if "=" in w:
                    continue
                continue
            if w in ("&&", "||", "|", ";", ">", "<"):
                break
            pkg = w.split("@")[0] if w.startswith("@") else w
            pkg = w if w.startswith("@") and w.count("/") == 1 else pkg
            if pkg and not pkg.startswith("-"):
                packages.append(pkg)

    return packages


# ============================================================================
# G3 核心逻辑
# ============================================================================


def audit_install_command(
    cmd: str,
    mode: str = "auto",
) -> Dict[str, Any]:
    """审计依赖安装命令的安全性。

    检测维度:
        1. 危险命令标志（sudo, --break-system-packages 等）
        2. 包名 typosquatting
        3. 可疑注册表来源
        4. 生态系统识别

    Args:
        cmd: 安装命令字符串
        mode: 运行模式

    Returns:
        {
            "gate_id": "G3",
            "passed": bool,
            "blocked": bool,
            "ecosystem": str,       # pypi / npm / gem / cargo / unknown
            "packages": [str],
            "findings": [dict],
            "warnings": [str],
            "timestamp": str,
        }
    """
    findings: List[Dict[str, Any]] = []
    warnings: List[str] = []

    # 识别生态系统
    ecosystem = "unknown"
    cmd_lower = cmd.lower()
    if "pip" in cmd_lower and "install" in cmd_lower:
        ecosystem = "pypi"
    elif "npm" in cmd_lower and "install" in cmd_lower:
        ecosystem = "npm"
    elif "gem" in cmd_lower and "install" in cmd_lower:
        ecosystem = "gem"
    elif "cargo" in cmd_lower and "install" in cmd_lower:
        ecosystem = "cargo"

    # 1. 危险命令标志检测
    for pattern, tag, description in DANGEROUS_INSTALL_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            findings.append({
                "type": "dangerous_flag",
                "tag": tag,
                "description": description,
                "severity": "HIGH",
            })
            warnings.append(f"[{tag}] {description}")

    # 2. 提取包名并检测
    if ecosystem == "pypi":
        packages = extract_packages_from_pip_command(cmd)
    elif ecosystem == "npm":
        packages = extract_packages_from_npm_command(cmd)
    else:
        packages = []

    for pkg in packages:
        result = check_package_name(pkg, ecosystem)
        if result["suspicious"]:
            for w in result["warnings"]:
                findings.append({
                    "type": "suspicious_package",
                    "package": pkg,
                    "ecosystem": ecosystem,
                    "warning": w,
                    "severity": "MEDIUM",
                })
                warnings.append(f"[{pkg}] {w}")

    # 3. 可疑注册表检测
    for pattern in SUSPICIOUS_REGISTRY_PATTERNS:
        match = pattern.search(cmd)
        if match:
            findings.append({
                "type": "suspicious_registry",
                "url_fragment": match.group(0)[:100],
                "severity": "HIGH",
            })
            warnings.append(f"可疑注册表: {match.group(0)[:100]}")

    # 4. 判定阻塞
    has_high = any(f.get("severity") == "HIGH" for f in findings)
    blocked = has_high and mode in ("safe", "auto", "collaborative")
    passed = not blocked

    if findings:
        logger.warning("G3 发现 %d 个依赖安装问题（blocked=%s）", len(findings), blocked)

    return {
        "gate_id": GATE_ID,
        "passed": passed,
        "blocked": blocked,
        "ecosystem": ecosystem,
        "packages": packages,
        "findings": findings,
        "warnings": warnings,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def inject_g3_issues_into_state(
    state: dict,
    audit_result: Dict[str, Any],
) -> int:
    """将 G3 审计发现注入 state 的 issue 列表。

    Args:
        state: state 字典（原地修改）
        audit_result: audit_install_command() 返回结果

    Returns:
        注入的 issue 数量
    """
    import uuid

    findings = audit_result.get("findings", [])
    if not findings:
        return 0

    phase = state["progress"].get("phase", "unknown")
    count = 0

    for f in findings:
        severity = "P1" if f.get("severity") == "HIGH" else "P2"
        issue = {
            "id": f"g3-{uuid.uuid4().hex[:8]}",
            "severity": severity,
            "title": f"G3 依赖安全: {f.get('type', 'unknown')}",
            "description": f"依赖安装审计发现问题。\n{f.get('warning', f.get('description', ''))}",
            "source": "hermes_guardrail",
            "source_ref": f"gate_g3@{f.get('tag', f.get('type', ''))}",
            "discovered_in_phase": phase,
            "status": "open",
            "affected_files": [],
            "linked_task_ids": [],
            "fix_strategy": "人工审查依赖安全性后放行。",
        }
        sev_key = severity.lower()
        state["issues"]["active"][sev_key].append(issue)
        state["issues"]["all_time"][f"{sev_key}_total"] += 1
        count += 1

    if count > 0:
        state["progress"]["new_issues_this_round"] = True

    return count


# ============================================================================
# 高层接口
# ============================================================================


def run_gate_g3(
    cmd: str,
    state: dict,
) -> Dict[str, Any]:
    """运行 G3 依赖安装门完整流程。

    Args:
        cmd: 要审计的安装命令
        state: state 字典（原地修改）

    Returns:
        审计结果字典
    """
    mode = state.get("config", {}).get("mode", "auto")
    audit_result = audit_install_command(cmd, mode)

    if audit_result["blocked"]:
        inject_g3_issues_into_state(state, audit_result)
        # 记录到 dangerous_ops_blocked
        gate = state.setdefault("gate_state", {})
        gate.setdefault("dangerous_ops_blocked", []).append({
            "operation": f"dependency_install:{cmd[:120]}",
            "reason": "; ".join(audit_result.get("warnings", [])[:3]),
            "blocked_at": audit_result["timestamp"],
        })

    return audit_result
