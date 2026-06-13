# -*- coding: utf-8 -*-
"""G4 危险操作门（5 层 Matcher L0-L4）。

对所有 Hermes Agent 执行的 Shell 命令进行 5 层安全匹配：

    L0 - ALLOW: 明确安全的只读命令（ls, cat, grep, echo, git status 等）
    L1 - WARN: 可能有副作用的命令（git add, npm test, cargo check 等）
    L2 - CONFIRM: 需要用户确认的操作（npm install, pip install, docker build 等）
    L3 - BLOCK_SAFE: safe 模式拦截，其他模式允许
        （rm, mv, chmod, systemctl, git push, 网络监听 等）
    L4 - BLOCK_ALWAYS: 无条件拦截（擦除磁盘、后门、fork bomb、特权提升 等）

匹配优先级: L4 > L3 > L2 > L1 > L0（找到最高匹配后立即返回）
未匹配到的命令默认归入 L3（BLOCK_SAFE）

设计意图:
    提供细粒度的命令分类，取代简单的黑白名单，使安全策略可配置、
    可审计。每层都有清晰的判定标准和人类可读的 reason。

参考文献:
    - Google Shell Style Guide
    - OWASP Command Injection Prevention
    - CWE-78: OS Command Injection
"""

import re
import logging
from enum import IntEnum
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple, Pattern

logger = logging.getLogger("loop_hermes.gate_g4")

# ============================================================================
# G4 闸门常量
# ============================================================================

GATE_ID = "G4"


class GateLevel(IntEnum):
    """闸门五层等级定义。"""
    L0_ALLOW = 0       # 明确只读安全命令
    L1_WARN = 1        # 可能有副作用但通常安全
    L2_CONFIRM = 2     # 需要确认（safe/auto 暂停）
    L3_BLOCK_SAFE = 3  # safe/auto 拦截
    L4_BLOCK_ALWAYS = 4  # 所有模式拦截


# 等级到人类可读标签
LEVEL_LABELS = {
    0: "L0-ALLOW",
    1: "L1-WARN",
    2: "L2-CONFIRM",
    3: "L3-BLOCK_SAFE",
    4: "L4-BLOCK_ALWAYS",
}

# 各模式允许的最高等级（block 等级 = max_allowed + 1 及以上）
MODE_MAX_LEVEL = {
    "unsafe": 3,     # 只拦截 L4（>3）
    "auto": 2,        # L3+ 拦截（>2）
    "safe": 1,        # L2+ 拦截（>1）
    "collaborative": 2,  # L3+ 拦截（>2）
}


# ============================================================================
# Matcher 类型定义
# ============================================================================

MatcherRule = Tuple[Pattern, str, str]  # (compiled_regex, tag, description)


# ============================================================================
# L0 - ALLOW: 明确安全的只读命令
# ============================================================================

L0_ALLOW_PATTERNS: List[MatcherRule] = [
    # 文件浏览
    (re.compile(r"^(ls|ll|dir)(\s|$)"), "LS", "列出目录内容"),
    (re.compile(r"^cat\s+\S+"), "CAT", "读取文件内容"),
    (re.compile(r"^head(\s|$)"), "HEAD", "读取文件开头"),
    (re.compile(r"^tail(\s|$)"), "TAIL", "读取文件末尾"),
    (re.compile(r"^less(\s|$)"), "LESS", "分页查看文件"),
    (re.compile(r"^wc(\s|$)"), "WC", "统计文件行数/字数"),
    (re.compile(r"^file\s+"), "FILE_TYPE", "检测文件类型"),
    (re.compile(r"^du\s+-"), "DU", "查看目录大小"),
    (re.compile(r"^df(\s|$)"), "DF", "查看磁盘空间"),
    (re.compile(r"^stat\s+"), "STAT", "查看文件状态"),
    (re.compile(r"^readlink\s+"), "READLINK", "读取符号链接"),

    # 文本处理（无写操作）
    (re.compile(r"^(echo|printf)(\s|$)"), "ECHO", "输出文本"),
    (re.compile(r"^grep(\s|$)"), "GREP", "文本搜索"),
    (re.compile(r"^find\s+\S+\s+.*-name"), "FIND_NAME", "按名称查找文件"),
    (re.compile(r"^locate(\s|$)"), "LOCATE", "文件定位"),
    (re.compile(r"^which(\s|$)"), "WHICH", "查找命令路径"),
    (re.compile(r"^whereis(\s|$)"), "WHEREIS", "查找二进制路径"),

    # Git 只读操作
    (re.compile(r"^git\s+status"), "GIT_STATUS", "Git 状态查看"),
    (re.compile(r"^git\s+log\b"), "GIT_LOG", "Git 日志查看"),
    (re.compile(r"^git\s+diff\b"), "GIT_DIFF", "Git 差异查看"),
    (re.compile(r"^git\s+branch\b"), "GIT_BRANCH", "Git 分支列表"),
    (re.compile(r"^git\s+remote\s+-v"), "GIT_REMOTE", "Git 远程信息"),
    (re.compile(r"^git\s+show\b"), "GIT_SHOW", "Git 查看提交"),
    (re.compile(r"^git\s+blame\b"), "GIT_BLAME", "Git blame"),
    (re.compile(r"^git\s+tag\b"), "GIT_TAG", "Git 标签列表"),
    (re.compile(r"^git\s+rev-parse"), "GIT_REV_PARSE", "Git 版本解析"),
    (re.compile(r"^git\s+config\s+--list"), "GIT_CONFIG_LIST", "Git 配置列表"),

    # 环境/进程查看
    (re.compile(r"^(env|printenv|set)\s*$"), "ENV", "查看环境变量"),
    (re.compile(r"^(ps|top|htop)(\s|$)"), "PS", "查看进程"),
    (re.compile(r"^(whoami|id|groups)\s*$"), "WHOAMI", "查看当前用户"),
    (re.compile(r"^(pwd|hostname|uname)(\s|$)"), "PWD", "系统信息"),

    # 语言工具只读
    (re.compile(r"^(python|python3|node)\s+-(c|e)\s+.*print"), "LANG_PRINT",
     "脚本 print 输出"),
    (re.compile(r"^(python|python3)\s+-m\s+pytest\s+.*--collect-only"), "PYTEST_COLLECT",
     "仅收集测试"),
    (re.compile(r"^(python|python3)\s+-m\s+black\s+.*--check"), "BLACK_CHECK",
     "Black 格式检查"),
    (re.compile(r"^(python|python3)\s+-m\s+ruff\s+check"), "RUFF_CHECK",
     "Ruff 代码检查"),
    (re.compile(r"^(python|python3)\s+-m\s+mypy\s+"), "MYPY",
     "Mypy 类型检查"),
    (re.compile(r"^(cargo|go|rustc)\s+(check|build\s+--check)"), "CARGO_CHECK",
     "编译检查"),

    # 网络诊断只读
    (re.compile(r"^(ping|ping6)\s+-c\s+\d+"), "PING_COUNTED",
     "有限次数 ping"),
    (re.compile(r"^(curl|wget)\s+.*-O\s+\S+"), "CURL_DOWNLOAD",
     "curl 下载文件"),
    (re.compile(r"^nslookup\s+"), "NSLOOKUP", "DNS 查询"),
    (re.compile(r"^dig\s+"), "DIG", "DNS 查询"),
    (re.compile(r"^traceroute\s+"), "TRACEROUTE", "路由追踪"),
]


# ============================================================================
# L1 - WARN: 可能有副作用的命令
# ============================================================================

L1_WARN_PATTERNS: List[MatcherRule] = [
    # Git 修改操作
    (re.compile(r"^git\s+add\b"), "GIT_ADD", "Git 暂存文件"),
    (re.compile(r"^git\s+commit\b"), "GIT_COMMIT", "Git 提交"),
    (re.compile(r"^git\s+stash\b"), "GIT_STASH", "Git 暂存"),
    (re.compile(r"^git\s+checkout\b"), "GIT_CHECKOUT", "Git 切换分支"),
    (re.compile(r"^git\s+switch\b"), "GIT_SWITCH", "Git 切换分支"),
    (re.compile(r"^git\s+reset\s+(?!.*--hard)"), "GIT_RESET_SOFT",
     "Git 软重置（非 hard）"),
    (re.compile(r"^git\s+merge\b"), "GIT_MERGE", "Git 合并"),
    (re.compile(r"^git\s+rebase\b"), "GIT_REBASE", "Git 变基"),
    (re.compile(r"^git\s+fetch\b"), "GIT_FETCH", "Git 获取远程"),
    (re.compile(r"^git\s+pull\b"), "GIT_PULL", "Git 拉取"),

    # 测试/检查工具
    (re.compile(r"^(python|python3)\s+-m\s+pytest\b"), "PYTEST_RUN",
     "运行 pytest 测试"),
    (re.compile(r"^(npm|yarn|pnpm)\s+(test|run\s+test)"), "NPM_TEST",
     "npm 测试"),
    (re.compile(r"^(npm|yarn|pnpm)\s+run\s+(lint|check|typecheck)"), "NPM_LINT",
     "npm lint/check"),
    (re.compile(r"^cargo\s+test\b"), "CARGO_TEST", "cargo 测试"),
    (re.compile(r"^go\s+test\b"), "GO_TEST", "Go 测试"),
    (re.compile(r"^make\s+(test|check|lint)"), "MAKE_TEST", "make 测试"),

    # 构建（非安装）
    (re.compile(r"^cargo\s+build\b"), "CARGO_BUILD", "cargo 构建"),
    (re.compile(r"^go\s+build\b"), "GO_BUILD", "Go 构建"),
    (re.compile(r"^make\b"), "MAKE", "make 构建"),
    (re.compile(r"^(python|python3)\s+-m\s+build"), "PYTHON_BUILD",
     "Python 构建包"),

    # 包管理查询
    (re.compile(r"^pip\s+(list|show|freeze|check|config)\b"), "PIP_QUERY",
     "pip 查询"),
    (re.compile(r"^npm\s+(list|view|outdated|audit)\b"), "NPM_QUERY",
     "npm 查询"),
    (re.compile(r"^(apt|brew|yum|dnf)\s+(list|info|search)\b"), "PKG_QUERY",
     "系统包查询"),

    # 格式化（只改格式）
    (re.compile(r"^(python|python3)\s+-m\s+black\s+(?!.*--check)"), "BLACK_FMT",
     "Black 格式化"),
    (re.compile(r"^(python|python3)\s+-m\s+ruff\s+(?!.*check)"), "RUFF_FIX",
     "Ruff 格式化"),
    (re.compile(r"^(python|python3)\s+-m\s+isort\b"), "ISORT",
     "isort 排序导入"),
    (re.compile(r"^(npx\s+)?prettier\s+--write"), "PRETTIER",
     "Prettier 格式化"),

    # 文件创建（小范围）
    (re.compile(r"^touch\s+\S+"), "TOUCH", "创建空文件"),
    (re.compile(r"^mkdir\s+-p\s+\S+"), "MKDIR_P", "创建目录"),
    (re.compile(r"^ln\s+-s\s+"), "LN_SOFT", "创建符号链接"),

    # Git 修改操作
    (re.compile(r"^git\s+clone\b"), "GIT_CLONE", "Git 克隆仓库"),
    (re.compile(r"^git\s+init\b"), "GIT_INIT", "Git 初始化仓库"),
]


# ============================================================================
# L2 - CONFIRM: 需要用户确认
# ============================================================================

L2_CONFIRM_PATTERNS: List[MatcherRule] = [
    # 包安装
    (re.compile(r"^(pip|pip3|python\s+-m\s+pip)\s+install\b"), "PIP_INSTALL",
     "pip 安装包"),
    (re.compile(r"^(pip|pip3)\s+uninstall\b"), "PIP_UNINSTALL",
     "pip 卸载包"),
    (re.compile(r"^(npm|yarn|pnpm)\s+install\b"), "NPM_INSTALL",
     "npm 安装包"),
    (re.compile(r"^(npm|yarn|pnpm)\s+add\b"), "NPM_ADD",
     "npm 添加包"),
    (re.compile(r"^(npm|yarn|pnpm)\s+remove\b"), "NPM_REMOVE",
     "npm 移除包"),
    (re.compile(r"^(npm|yarn|pnpm)\s+update\b"), "NPM_UPDATE",
     "npm 更新包"),
    (re.compile(r"^gem\s+install\b"), "GEM_INSTALL", "gem 安装"),
    (re.compile(r"^cargo\s+install\b"), "CARGO_INSTALL", "cargo 安装"),
    (re.compile(r"^(apt-get|apt|brew|yum|dnf|pacman|choco)\s+install\b"),
     "SYSTEM_INSTALL", "系统包管理器安装"),
    (re.compile(r"^(apt-get|apt|brew|yum|dnf)\s+update\b"), "SYSTEM_UPDATE",
     "系统包更新"),
    (re.compile(r"^(apt-get|apt)\s+upgrade\b"), "SYSTEM_UPGRADE",
     "系统包升级"),

    # Docker 操作
    (re.compile(r"^docker\s+build\b"), "DOCKER_BUILD", "Docker 构建"),
    (re.compile(r"^docker\s+run\b"), "DOCKER_RUN", "Docker 运行"),
    (re.compile(r"^docker\s+compose\s+up"), "DOCKER_COMPOSE_UP",
     "Docker Compose 启动"),
    (re.compile(r"^docker\s+pull\b"), "DOCKER_PULL", "Docker 拉取镜像"),

    # 部署操作
    (re.compile(r"^git\s+push\b"), "GIT_PUSH", "Git 推送"),
    (re.compile(r"^git\s+push\s+--force"), "GIT_PUSH_FORCE",
     "Git 强制推送"),
    (re.compile(r"^(aws|gcloud|az|doctl)\s+"), "CLOUD_CLI", "云平台 CLI"),
    (re.compile(r"^(terraform|pulumi|ansible)\s+apply"), "IAC_APPLY",
     "IaC 应用"),
    (re.compile(r"^(helm|kubectl)\s+(install|apply|upgrade)"), "K8S_DEPLOY",
     "Kubernetes 部署"),

    # 数据库操作
    (re.compile(r"^(mysql|psql|sqlite3|mongo|redis-cli)\s+"), "DB_CLIENT",
     "数据库客户端"),
    (re.compile(r"^(alembic|flask\s+db)\s+(upgrade|downgrade)"), "DB_MIGRATE",
     "数据库迁移"),
]


# ============================================================================
# L3 - BLOCK_SAFE: safe/auto 拦截
# ============================================================================

L3_BLOCK_SAFE_PATTERNS: List[MatcherRule] = [
    # 文件删除/修改
    (re.compile(r"^rm\s+-"), "RM_FLAG", "删除文件"),
    (re.compile(r"^rmdir\s+"), "RMDIR", "删除目录"),
    (re.compile(r"^mv\s+"), "MV", "移动文件"),
    (re.compile(r"^cp\s+-r\s+"), "CP_RECURSIVE", "递归复制"),
    (re.compile(r"^chmod\s+"), "CHMOD", "修改文件权限"),
    (re.compile(r"^chown\s+"), "CHOWN", "修改文件所有权"),
    (re.compile(r"^chattr\s+"), "CHATTR", "修改文件属性"),

    # Git 危险操作
    (re.compile(r"^git\s+reset\s+--hard"), "GIT_RESET_HARD",
     "Git 硬重置（丢失修改）"),
    (re.compile(r"^git\s+clean\s+-[fd]"), "GIT_CLEAN", "Git 清理未跟踪文件"),
    (re.compile(r"^git\s+rebase\s+-i"), "GIT_REBASE_INTERACTIVE",
     "Git 交互式变基"),
    (re.compile(r"^git\s+push\s+--force.*main"), "GIT_PUSH_FORCE_MAIN",
     "Git 强制推送 main"),

    # 系统服务
    (re.compile(r"^(systemctl|service)\s+(start|stop|restart|reload)"),
     "SYSTEMCTL_MODIFY", "系统服务管理"),
    (re.compile(r"^(systemctl|service)\s+(enable|disable|mask)"),
     "SYSTEMCTL_PERSIST", "系统服务持久修改"),

    # 网络监听
    (re.compile(r"^(nc|netcat|ncat)\s+-[lL]\s+"), "NC_LISTEN",
     "netcat 监听模式"),
    (re.compile(r"^(python|python3)\s+-m\s+http\.server"), "PY_HTTP_SERVER",
     "Python HTTP 服务器"),

    # 容器特权操作
    (re.compile(r"^docker\s+exec\s+-it"), "DOCKER_EXEC_IT",
     "Docker 交互式 exec"),
    (re.compile(r"^docker\s+system\s+prune"), "DOCKER_PRUNE",
     "Docker 清理"),
    (re.compile(r"^docker\s+rm\s+-f"), "DOCKER_RM_FORCE",
     "Docker 强制删除"),

    # 用户/权限修改
    (re.compile(r"^(useradd|adduser|usermod|userdel|groupadd|groupdel)\s+"),
     "USER_MOD", "用户/组管理"),
    (re.compile(r"^(passwd)\s+"), "PASSWD", "修改密码"),

    # 环境变量全局修改
    (re.compile(r"^export\s+\S+="), "EXPORT_ENV", "设置环境变量"),

    # 文件系统操作
    (re.compile(r"^(mount|umount|losetup)\s+"), "MOUNT", "挂载操作"),
    (re.compile(r"^mkfs\.\S+\s+"), "MKFS", "创建文件系统（非 root）"),
    (re.compile(r"^dd\s+"), "DD", "dd 磁盘操作"),
    (re.compile(r"^(fdisk|parted|gparted)\s+"), "PARTITION", "磁盘分区"),

    # 内核模块
    (re.compile(r"^(modprobe|insmod|rmmod|lsmod)\s+"), "KERNEL_MODULE",
     "内核模块操作"),

    # 防火墙
    (re.compile(r"^(iptables|nft|ufw|firewall-cmd)\s+"), "FIREWALL",
     "防火墙修改"),
]


# ============================================================================
# L4 - BLOCK_ALWAYS: 无条件拦截
# ============================================================================

L4_BLOCK_ALWAYS_PATTERNS: List[MatcherRule] = [
    # 擦除系统/磁盘
    (re.compile(r"rm\s+-rf\s+/"), "RM_RF_ROOT", "递归擦除根目录"),
    (re.compile(r"rm\s+-rf\s+--no-preserve-root\s+/"), "RM_RF_ROOT_FORCE",
     "强制擦除根目录"),
    (re.compile(r"dd\s+if=/dev/(zero|random|urandom)\s+of=/dev/sd[a-z]"),
     "DD_WIPE", "擦除磁盘"),
    (re.compile(r"mkfs\.\S+\s+/dev/sd[a-z]"), "MKFS_DISK",
     "格式化磁盘"),
    (re.compile(r">\s*/dev/sd[a-z]"), "REDIR_SD", "重定向到磁盘设备"),
    (re.compile(r"cp\s+/dev/(zero|null)\s+/dev/sd[a-z]"), "CP_ZERO_DISK",
     "覆盖磁盘"),

    # Fork Bomb
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:"), "FORK_BOMB_BASH",
     "Bash Fork bomb"),
    (re.compile(r"\$0\s*&\s*\$0"), "FORK_BOMB_SELF",
     "自引用 Fork bomb"),
    (re.compile(r"perl\s+-e\s+.*fork\s+while"), "FORK_BOMB_PERL",
     "Perl Fork bomb"),
    (re.compile(r"python.*while\s+True.*os\.fork"), "FORK_BOMB_PY",
     "Python Fork bomb"),

    # 后门 / 反向 Shell
    (re.compile(r"nc\s+-[lL]\s+-[pP]\s+\d+\s+-[eE]\s+/bin/(ba)?sh"),
     "NC_BACKDOOR", "netcat 后门"),
    (re.compile(r"bash\s+-i\s+>&\s+/dev/tcp/"), "BASH_REV_SHELL",
     "Bash TCP 反向 shell"),
    (re.compile(r"python.*socket\.socket.*connect"), "PY_REV_SHELL",
     "Python 反向 shell"),
    (re.compile(r"perl\s+-e\s+.*Socket.*connect"), "PERL_REV_SHELL",
     "Perl 反向 shell"),
    (re.compile(r"ruby\s+-e\s+.*TCPSocket"), "RUBY_REV_SHELL",
     "Ruby 反向 shell"),

    # 特权提升
    (re.compile(r"sudo\s+su\b"), "SUDO_SU", "sudo 切换到 root"),
    (re.compile(r"chmod\s+\+s\s+"), "CHMOD_SUID", "设置 SUID 位"),
    (re.compile(r"echo\s+\S+\s+>>\s+/etc/(sudoers|passwd|shadow)"),
     "MODIFY_SYSTEM_AUTH", "修改系统认证文件"),
    (re.compile(r"curl.*\|.*(sudo|su)"), "CURL_PIPE_SUDO",
     "curl 管道到 sudo"),
    (re.compile(r"wget.*\|.*(sudo|su)"), "WGET_PIPE_SUDO",
     "wget 管道到 sudo"),

    # 内核/引导破坏
    (re.compile(r"rm\s+-rf\s+/(boot|lib/modules)"), "RM_BOOT",
     "删除 /boot"),
    (re.compile(r"(grub|lilo|systemd-boot).*install"), "BOOTLOADER_INSTALL",
     "修改引导加载器"),
    (re.compile(r"echo\s+\S+\s+>\s+/proc/sys/"), "PROC_SYS_WRITE",
     "写入 /proc/sys 内核参数"),

    # 数据擦除/覆盖
    (re.compile(r"shred\s+"), "SHRED", "安全擦除文件"),
    (re.compile(r"wipe\s+-"), "WIPE", "擦除工具"),
    (re.compile(r"srm\s+"), "SRM", "安全删除"),

    # 网络攻击工具
    (re.compile(r"(nmap|masscan|zmap)\s+-"), "NET_SCAN_AGGRESSIVE",
     "激进网络扫描（参数化）"),
    (re.compile(r"(hydra|medusa|john|hashcat)\s+"), "CRACK_TOOL",
     "密码破解工具"),
    (re.compile(r"(aircrack|reaver|wifite)\s+"), "WIFI_ATTACK",
     "WiFi 攻击工具"),
    (re.compile(r"(ettercap|bettercap|dsniff|arpspoof)\s+"), "MITM_TOOL",
     "中间人攻击工具"),

    # 禁用安全防护
    (re.compile(r"systemctl\s+(stop|disable|mask)\s+(firewalld|ufw|apparmor|selinux|auditd)"),
     "DISABLE_SECURITY", "禁用安全服务"),
    (re.compile(r"setenforce\s+0"), "SETENFORCE_0",
     "关闭 SELinux"),
    (re.compile(r"aa-teardown|apparmor_parser\s+-R"), "DISABLE_APPARMOR",
     "关闭 AppArmor"),
]


# ============================================================================
# Matcher 引擎
# ============================================================================


def _safe_strip(cmd: str) -> str:
    """安全地清理命令字符串，移除多余空白。

    Args:
        cmd: 原始命令字符串

    Returns:
        清理后的命令字符串
    """
    return cmd.strip().lstrip("$").lstrip(">").strip()


def _match_layer(
    cmd: str,
    patterns: List[MatcherRule],
    layer_name: str,
) -> Optional[Dict[str, Any]]:
    """在给定命令上尝试匹配一层的所有模式。

    Args:
        cmd: 清理后的命令字符串
        patterns: 该层的 (regex, tag, description) 列表
        layer_name: 层名称（用于日志）

    Returns:
        匹配成功时返回匹配信息，否则返回 None
    """
    for regex, tag, description in patterns:
        if regex.search(cmd):
            logger.debug("G4 %s 匹配: [%s] %s", layer_name, tag, description)
            return {
                "layer": layer_name,
                "tag": tag,
                "description": description,
            }
    return None


def classify_command(cmd: str) -> Dict[str, Any]:
    """对 Shell 命令进行五层安全分类。

    按优先级从高到低依次匹配：L4 → L3 → L2 → L1 → L0。
    未匹配到任何模式时，默认归入 L3（BLOCK_SAFE）。

    Args:
        cmd: Shell 命令字符串

    Returns:
        {
            "gate_id": "G4",
            "command": str,         # 原始命令（截断到 200 字符）
            "level": int,           # 匹配等级 0-4
            "level_label": str,     # 人类可读标签
            "tag": str,             # 匹配的标签
            "description": str,     # 匹配的描述
            "timestamp": str,
        }
    """
    clean_cmd = _safe_strip(cmd)
    if len(clean_cmd) > 200:
        clean_cmd = clean_cmd[:200]

    # 空命令 → L0（安全放行）
    if not clean_cmd:
        return {
            "gate_id": GATE_ID,
            "command": clean_cmd,
            "level": GateLevel.L0_ALLOW,
            "level_label": "L0-ALLOW",
            "tag": "EMPTY_CMD",
            "description": "空命令",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # 按优先级匹配各层
    layers = [
        (L4_BLOCK_ALWAYS_PATTERNS, "L4-BLOCK_ALWAYS"),
        (L3_BLOCK_SAFE_PATTERNS, "L3-BLOCK_SAFE"),
        (L2_CONFIRM_PATTERNS, "L2-CONFIRM"),
        (L1_WARN_PATTERNS, "L1-WARN"),
        (L0_ALLOW_PATTERNS, "L0-ALLOW"),
    ]

    for patterns, layer_name in layers:
        match = _match_layer(clean_cmd, patterns, layer_name)
        if match:
            level = int(layer_name[1])
            return {
                "gate_id": GATE_ID,
                "command": clean_cmd,
                "level": level,
                "level_label": layer_name,
                "tag": match["tag"],
                "description": match["description"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    # 默认：未匹配任何模式 → L3 BLOCK_SAFE
    logger.debug("G4 未匹配任何模式，默认 L3: %s", clean_cmd[:80])
    return {
        "gate_id": GATE_ID,
        "command": clean_cmd,
        "level": GateLevel.L3_BLOCK_SAFE,
        "level_label": "L3-BLOCK_SAFE",
        "tag": "UNKNOWN_CMD",
        "description": f"未识别的命令（默认拦截）: {clean_cmd[:80]}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def is_blocked(classification: Dict[str, Any], mode: str) -> bool:
    """判断命令在当前模式下是否应被拦截。

    各模式允许的最高等级:
        - unsafe: L4（只拦截灾难性操作）
        - auto: L3（L2 仅确认，L3+ 拦截）
        - safe: L2（L2+ 需用户确认或拦截）
        - collaborative: L3（同 auto）

    Args:
        classification: classify_command() 返回的分类结果
        mode: 运行模式

    Returns:
        True 如果命令应被拦截
    """
    level = classification["level"]
    max_allowed = MODE_MAX_LEVEL.get(mode, 2)  # 默认 auto: L3+ 拦截
    return level > max_allowed


def is_warned(classification: Dict[str, Any], mode: str) -> bool:
    """判断命令在当前模式下是否应发出警告。

    Args:
        classification: 分类结果
        mode: 运行模式

    Returns:
        True 如果命令应触发警告
    """
    level = classification["level"]
    return level >= GateLevel.L1_WARN


# ============================================================================
# 批量命令审计
# ============================================================================


def audit_commands(
    commands: List[str],
    mode: str = "auto",
) -> Dict[str, Any]:
    """批量审计多条 Shell 命令。

    对每条命令单独分类，汇总后返回整体判决。
    如果任何一条命令被拦截，整体判定为 blocked。

    Args:
        commands: 命令字符串列表
        mode: 运行模式

    Returns:
        {
            "gate_id": "G4",
            "passed": bool,
            "blocked": bool,
            "total_commands": int,
            "classifications": [dict],
            "blocked_commands": [dict],
            "warned_commands": [dict],
            "summary": {
                "L0": int, "L1": int, "L2": int, "L3": int, "L4": int,
            },
            "timestamp": str,
        }
    """
    classifications = []
    blocked_cmds = []
    warned_cmds = []
    summary = {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0}

    for cmd in commands:
        c = classify_command(cmd)
        classifications.append(c)
        summary[c["level_label"].split("-")[0]] += 1

        if is_blocked(c, mode):
            blocked_cmds.append(c)
        elif is_warned(c, mode):
            warned_cmds.append(c)

    blocked = len(blocked_cmds) > 0
    passed = not blocked

    if blocked_cmds:
        logger.warning(
            "G4 审计: %d/%d 命令被拦截",
            len(blocked_cmds), len(commands),
        )
    elif warned_cmds:
        logger.info(
            "G4 审计: %d/%d 命令需警告（全部通过）",
            len(warned_cmds), len(commands),
        )
    else:
        logger.debug("G4 审计: %d 条命令全部通过", len(commands))

    return {
        "gate_id": GATE_ID,
        "passed": passed,
        "blocked": blocked,
        "total_commands": len(commands),
        "classifications": classifications,
        "blocked_commands": blocked_cmds,
        "warned_commands": warned_cmds,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Issue 注入
# ============================================================================


def inject_g4_issues_into_state(
    state: dict,
    audit_result: Dict[str, Any],
) -> int:
    """将 G4 审计中被拦截的命令注入 state issue 列表。

    每个被拦截的命令生成一个 P0 issue。

    Args:
        state: state 字典（原地修改）
        audit_result: audit_commands() 返回结果

    Returns:
        注入的 issue 数量
    """
    import uuid

    blocked = audit_result.get("blocked_commands", [])
    if not blocked:
        return 0

    phase = state["progress"].get("phase", "unknown")
    count = 0

    for b in blocked:
        cmd = b.get("command", "unknown")
        desc = b.get("description", "")
        tag = b.get("tag", "UNKNOWN")

        issue = {
            "id": f"g4-{uuid.uuid4().hex[:8]}",
            "severity": "P0",
            "title": f"G4 拦截危险命令: [{tag}] {cmd[:60]}",
            "description": (
                f"G4 危险操作门拦截了命令。\n"
                f"命令: {cmd}\n"
                f"等级: {b.get('level_label', '')}\n"
                f"标签: {tag}\n"
                f"描述: {desc}"
            )[:500],
            "source": "hermes_guardrail",
            "source_ref": f"gate_g4@{tag}",
            "discovered_in_phase": phase,
            "status": "open",
            "affected_files": [],
            "linked_task_ids": [],
            "fix_strategy": "需要人工审查命令安全性后重新执行。",
        }

        state["issues"]["active"]["p0"].append(issue)
        state["issues"]["all_time"]["p0_total"] += 1
        count += 1

    if count > 0:
        state["progress"]["new_issues_this_round"] = True

    logger.info("G4 注入 %d 个 issue 到 state", count)
    return count


# ============================================================================
# 高层接口
# ============================================================================


def run_gate_g4(
    commands: List[str],
    state: dict,
) -> Dict[str, Any]:
    """运行 G4 危险操作门完整流程。

    1. 对每条命令进行五层分类
    2. 根据模式判断是否拦截
    3. 如被拦截，注入 issue 并更新 gate_state

    Args:
        commands: 待审计的 Shell 命令列表
        state: state 字典（原地修改）

    Returns:
        完整审计结果字典
    """
    mode = state.get("config", {}).get("mode", "auto")
    audit_result = audit_commands(commands, mode)

    if audit_result["blocked"]:
        inject_g4_issues_into_state(state, audit_result)

        # 更新 gate_state
        gate = state.setdefault("gate_state", {})
        for b in audit_result["blocked_commands"]:
            gate.setdefault("dangerous_ops_blocked", []).append({
                "operation": b.get("command", ""),
                "reason": f"{b.get('level_label', '')}: {b.get('description', '')}",
                "blocked_at": b.get("timestamp", ""),
            })

    return audit_result


def run_gate_g4_single(
    command: str,
    state: dict,
) -> Dict[str, Any]:
    """运行 G4 危险操作门 —— 单条命令模式。

    Args:
        command: 单条 Shell 命令
        state: state 字典（原地修改）

    Returns:
        单条命令的分类结果 + 阻断判定
    """
    return run_gate_g4([command], state)
