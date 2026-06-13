# -*- coding: utf-8 -*-
r"""阶段调度器模块。

连接 state_machine → phase_dispatch → hermes_client → routing 的完整链路。

核心职责:
    1. HLOOP_STATE block 解析器 —— 从 stdout 中提取 <<<HLOOP_STATE>>> block
    2. 终止条件判定逻辑 —— 四层级联判定
    3. 外部调度器参考实现 —— cron / Task Scheduler / sleep loop

终止条件（四层级联）:
    L1: termination_status = complete/paused/failed → 立即停止
    L2: issues_active_p0=0 AND p1=0 AND p2=0 AND convergence_counter >= convergence_rounds → 完成
    L3: cycle >= max_cycles → 输出警告后停止
    L4: Default-FAIL 合约 —— 无法判定时默认停止

HLOOP_STATE Block 格式:
    <<<HLOOP_STATE>>>
    phase: part_2_3
    cycle: 2
    convergence_counter: 1
    new_issues_this_round: false
    issues_active_p0: 0
    issues_active_p1: 1
    issues_active_p2: 2
    all_test_status: pass
    all_issue_status: none_open
    pending_confirmation_status: null
    termination_status: running
    max_cycles: 5
    convergence_rounds: 2
    hermes_guardrail_hardlines: 0
    hermes_guardrail_warns: 0
    <<<END_HLOOP_STATE>>>
"""

import re
import time
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("loop_hermes.scheduler")

# ============================================================================
# HLOOP_STATE 解析器
# ============================================================================


def parse_hloop_state(stdout: str) -> Dict[str, Any]:
    """解析 stdout 中的 <<<HLOOP_STATE>>>...<<<END_HLOOP_STATE>>> block。

    支持两种格式:
        1. 简单 key: value 格式（每行一个键值对）
        2. JSON 格式（--json-output 模式）

    Args:
        stdout: loop-hermes 进程的标准输出

    Returns:
        解析后的 HLOOP_STATE 字典。未找到 block 时返回空字典。
        值自动转换类型: 数字 → int, true/false → bool, null → None
    """
    # 尝试 JSON 格式
    pattern_json = r'<<<HLOOP_STATE>>>\s*(\{.+?\})\s*<<<END_HLOOP_STATE>>>'
    match = re.search(pattern_json, stdout, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 回退到 key: value 格式
    pattern = r'<<<HLOOP_STATE>>>\s*(.+?)\s*<<<END_HLOOP_STATE>>>'
    match = re.search(pattern, stdout, re.DOTALL)
    if not match:
        logger.debug("未在 stdout 中找到 HLOOP_STATE block")
        return {}

    result = {}
    for line in match.group(1).strip().split('\n'):
        line = line.strip()
        if not line or ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip()
        value = value.strip()

        # 类型转换
        if value.isdigit():
            value = int(value)
        elif value in ('true', 'True'):
            value = True
        elif value in ('false', 'False'):
            value = False
        elif value in ('null', 'None', ''):
            value = None

        result[key] = value

    return result


def parse_hloop_state_from_file(file_path: str) -> Dict[str, Any]:
    """从文件中解析 HLOOP_STATE block。

    Args:
        file_path: 包含 HLOOP_STATE 输出的文件路径

    Returns:
        解析后的字典
    """
    content = Path(file_path).read_text(encoding="utf-8")
    return parse_hloop_state(content)


# ============================================================================
# 终止条件判定逻辑
# ============================================================================


def should_terminate(hstate: Dict[str, Any]) -> Tuple[bool, str]:
    """根据 HLOOP_STATE 判定是否应停止循环。

    四层级联判定:
        L1: termination_status 非 running → 停止
        L2: 收敛达成（无活跃 issue + counter >= rounds）→ 停止
        L3: cycle >= max_cycles → 停止
        L4: Default-FAIL → 停止（无法判定时保守停止）

    Args:
        hstate: parse_hloop_state() 返回的字典

    Returns:
        (should_stop: bool, reason: str)
    """
    # L1: termination_status
    status = hstate.get("termination_status", "running")
    if status == "complete":
        return True, "Termination status: complete"
    if status == "paused":
        return True, "Loop paused (requires user intervention)"
    if status == "failed":
        return True, "Loop failed"

    # L2: 收敛达成
    p0 = hstate.get("issues_active_p0", 0)
    p1 = hstate.get("issues_active_p1", 0)
    p2 = hstate.get("issues_active_p2", 0)
    conv = hstate.get("convergence_counter", 0)
    conv_rounds = hstate.get("convergence_rounds", 2)

    if p0 == 0 and p1 == 0 and p2 == 0 and conv >= conv_rounds:
        return True, "Convergence achieved (no issues, counter >= rounds)"

    # L3: 最大轮次
    cycle = hstate.get("cycle", 0)
    max_cycles = hstate.get("max_cycles", 5)
    if cycle >= max_cycles:
        total_issues = p0 + p1 + p2
        if total_issues > 0:
            return True, (
                f"Max cycles ({max_cycles}) reached with {total_issues} "
                f"open issues (P0={p0}, P1={p1}, P2={p2})"
            )
        return True, f"Max cycles ({max_cycles}) reached"

    # L4: Default-FAIL —— 无法判定时保守停止
    # 此处继续运行
    return False, "Continue"


def is_termination_condition_met(state: dict) -> Tuple[bool, str]:
    """直接通过 state 字典判定终止条件（无需 HLOOP_STATE 解析）。

    Args:
        state: state 字典

    Returns:
        (should_stop: bool, reason: str)
    """
    from .state_machine import is_terminated, is_converged, is_cycle_exceeded

    if is_terminated(state):
        status = state["termination"]["status"]
        reason = state["termination"].get("exit_reason", f"Status: {status}")
        return True, reason

    if is_converged(state):
        return True, "Convergence achieved"

    if is_cycle_exceeded(state):
        active = state["issues"]["active"]
        total = len(active["p0"]) + len(active["p1"]) + len(active["p2"])
        return True, f"Max cycles reached ({total} open issues)"

    return False, "Continue"


# ============================================================================
# 外部调度器参考实现
# ============================================================================


def scheduler_loop(
    state_dir: str,
    interval_seconds: int = 120,
    binary_path: str = "loop-hermes",
    extra_args: Optional[list] = None,
) -> int:
    """外部调度器参考实现 —— sleep loop 模式。

    通过 subprocess 反复调用 loop-hermes 进程，解析 HLOOP_STATE 判定终止。

    使用方法:
        # Linux crontab（替代方案）:
        # */5 * * * * cd /project && loop-hermes --no-pause

        # Python 调度器（本函数）:
        # scheduler_loop(".hermes/loop-hermes", interval_seconds=120)

    Args:
        state_dir: state.json 所在目录
        interval_seconds: 两次调用之间的等待秒数
        binary_path: loop-hermes 可执行文件路径（默认 "loop-hermes"）
        extra_args: 额外 CLI 参数列表

    Returns:
        退出码（0=正常完成, 1=异常终止）
    """
    args = [binary_path, "--state-dir", state_dir, "--no-pause"]
    if extra_args:
        args.extend(extra_args)

    iteration = 0
    while True:
        iteration += 1
        logger.info("调度器迭代 #%d: 调用 %s", iteration, " ".join(args))

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=600,  # 10 分钟超时
            )
        except subprocess.TimeoutExpired:
            logger.error("loop-hermes 调用超时")
            time.sleep(interval_seconds)
            continue
        except Exception as e:
            logger.error("调度器调用异常: %s", e)
            return 1

        # 输出到控制台
        stdout = result.stdout
        if stdout:
            print(stdout)
        if result.stderr:
            print(result.stderr, file=__import__("sys").stderr)

        # 退出码非 0
        if result.returncode != 0:
            logger.error("loop-hermes 退出码 %d", result.returncode)
            return result.returncode

        # 解析 HLOOP_STATE 并判定终止
        hstate = parse_hloop_state(stdout)
        if not hstate:
            logger.warning("未检测到 HLOOP_STATE block，继续等待...")
            time.sleep(interval_seconds)
            continue

        stop, reason = should_terminate(hstate)
        if stop:
            phase = hstate.get("phase", "?")
            cycle = hstate.get("cycle", "?")
            conv = hstate.get("convergence_counter", "?")
            logger.info(
                "循环停止: phase=%s, cycle=%s, conv=%s, reason=%s",
                phase, cycle, conv, reason,
            )
            print(f"\n[loop-hermes scheduler] 停止。原因: {reason}")
            return 0 if hstate.get("termination_status") == "complete" else 1

        # 等待下次调用
        logger.info("迭代 #%d 完成，等待 %d 秒...", iteration, interval_seconds)
        time.sleep(interval_seconds)


def scheduler_loop_once(
    state_dir: str,
    binary_path: str = "loop-hermes",
    extra_args: Optional[list] = None,
) -> Tuple[int, Dict[str, Any]]:
    """单次调度执行 —— 调用一次 loop-hermes 并返回结果。

    用于 crontab / Task Scheduler 场景（外部系统负责循环驱动）。

    Args:
        state_dir: state.json 所在目录
        binary_path: loop-hermes 可执行文件路径
        extra_args: 额外 CLI 参数列表

    Returns:
        (exit_code, hloop_state_dict)
    """
    args = [binary_path, "--state-dir", state_dir, "--no-pause"]
    if extra_args:
        args.extend(extra_args)

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=__import__("sys").stderr)

    hstate = parse_hloop_state(result.stdout)
    return result.returncode, hstate


# ============================================================================
# cron / Task Scheduler 配置生成
# ============================================================================


def generate_cron_entry(
    state_dir: str,
    interval_minutes: int = 5,
    binary_path: str = "loop-hermes",
) -> str:
    """生成 crontab 配置条目。

    Args:
        state_dir: state.json 所在目录
        interval_minutes: 执行间隔（分钟）
        binary_path: loop-hermes 可执行文件路径

    Returns:
        crontab 配置行
    """
    return (
        f"*/{interval_minutes} * * * * "
        f"cd {Path(state_dir).parent} && {binary_path} "
        f"--state-dir {state_dir} --no-pause"
    )


def generate_schtasks_command(
    state_dir: str,
    interval_minutes: int = 5,
    binary_path: str = "loop-hermes.exe",
) -> str:
    """生成 Windows Task Scheduler 配置命令。

    Args:
        state_dir: state.json 所在目录
        interval_minutes: 执行间隔（分钟）
        binary_path: loop-hermes 可执行文件路径

    Returns:
        schtasks 命令字符串
    """
    task_name = "loop-hermes-scheduler"
    return (
        f'schtasks /Create /SC MINUTE /MO {interval_minutes} '
        f'/TN "{task_name}" '
        f'/TR "{binary_path} --state-dir {state_dir} --no-pause"'
    )


def generate_launchd_plist(
    state_dir: str,
    interval_seconds: int = 300,
    binary_path: str = "loop-hermes",
) -> str:
    """生成 macOS launchd plist 配置内容。

    Args:
        state_dir: state.json 所在目录
        interval_seconds: 执行间隔（秒）
        binary_path: loop-hermes 可执行文件路径

    Returns:
        plist XML 内容
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.loop-hermes.scheduler</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary_path}</string>
        <string>--state-dir</string>
        <string>{state_dir}</string>
        <string>--no-pause</string>
    </array>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
