# -*- coding: utf-8 -*-
r"""CLI 入口模块。

loop-hermes 的命令行入口，基于 argparse 解析参数并派发对应操作:
    - loop-hermes --init         初始化新的 state.json
    - loop-hermes --check        运行 Sanity Check
    - loop-hermes --mode <MODE>  指定运行模式
    - loop-hermes --provider <P> 指定 provider 回退链
    - loop-hermes --requirement "..."  直接传入需求描述
    - loop-hermes "goal"         启动自动驾驶循环（单次执行）

运行模式（--mode 或对应标志）:
    --safe          安全模式 L1：全部闸门激活
    --auto          标准模式 L2（默认）
    --unsafe        无限制模式 L3：仅灾难性操作拦截
    --interactive   协作模式 L1+：关键决策点等待确认

输出:
    每次调用结束后输出 HLOOP_STATE block，供外部调度器解析。
"""

import sys
import argparse
import logging
from pathlib import Path
from typing import Optional, List

# ============================================================================
# argparse 参数定义
# ============================================================================


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。

    Returns:
        配置完整的 ArgumentParser 实例
    """
    parser = argparse.ArgumentParser(
        prog="loop-hermes",
        description=(
            "Hermes Agent 自动驾驶开发闭环 —— "
            "设定一个目标，AI 完成设计→实施→测试→验证的全闭环。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  loop-hermes --init\n"
            "  loop-hermes --check\n"
            "  loop-hermes --safe \"build a weather CLI in Python\"\n"
            "  loop-hermes --provider claude,openai \"refactor the auth module\"\n"
            "\n"
            "外部调度器示例 (crontab):\n"
            "  */5 * * * * cd /path/to/project && loop-hermes --no-pause\n"
        ),
    )

    # ---- 需求（位置参数） ----
    parser.add_argument(
        "goal",
        nargs="?",
        default=None,
        help="需求描述（自然语言 target description）",
    )

    # ---- 操作模式 ----
    parser.add_argument(
        "--init",
        action="store_true",
        help="初始化新的 state.json（如已存在则报错）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="运行 Sanity Check 并退出",
    )

    # ---- 运行模式（四选一） ----
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--safe",
        action="store_true",
        help="安全模式（L1）：全部闸门激活，方案确认暂停等待",
    )
    mode_group.add_argument(
        "--unsafe",
        action="store_true",
        help="无限制模式（L3）：仅灾难性操作拦截（需沙箱环境）",
    )
    mode_group.add_argument(
        "--interactive",
        action="store_true",
        help="协作模式（L1+）：Part 1 决策点等待确认，超时 30min 自动降级",
    )
    # --auto 是默认行为，无需显式标志

    # ---- 配置参数 ----
    parser.add_argument(
        "--mode",
        type=str,
        choices=["safe", "auto", "unsafe", "collaborative"],
        default=None,
        help="显式指定运行模式（与 --safe/--unsafe/--interactive 互斥）",
    )
    parser.add_argument(
        "--requirement",
        type=str,
        default=None,
        help="需求描述（与位置参数等效）",
    )
    parser.add_argument(
        "--provider",
        "--provider-fallback",
        dest="provider_fallback",
        type=str,
        default="claude,openai,deepseek",
        help="Provider 回退链（逗号分隔，默认: claude,openai,deepseek）",
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=".hermes/loop-hermes",
        help="state.json 存储目录（默认: .hermes/loop-hermes）",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=5,
        help="最大循环轮次（默认: 5）",
    )
    parser.add_argument(
        "--convergence-rounds",
        type=int,
        default=2,
        help="收敛所需连续无问题轮次（默认: 2）",
    )
    parser.add_argument(
        "--hermes-model",
        type=str,
        default="claude-sonnet-4-20250514",
        help="Hermes Agent 使用的模型 ID",
    )
    parser.add_argument(
        "--hermes-toolsets",
        type=str,
        default="code,shell",
        help="Hermes Agent 启用的工具集（逗号分隔，默认: code,shell）",
    )
    parser.add_argument(
        "--skip-testing",
        action="store_true",
        help="跳过测试阶段（仅用于快速验证场景）",
    )

    # ---- 调度器集成 ----
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="调度器模式：不暂停，直接执行当前 phase 后退出",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="以 JSON 格式输出 HLOOP_STATE（机器可读）",
    )

    # ---- 调试/日志 ----
    parser.add_argument(
        "--verbose", "-v",
        action="count",
        default=0,
        help="详细日志输出（-v 为 INFO，-vv 为 DEBUG）",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="日志文件路径（追加模式，默认仅输出到 stderr）",
    )

    return parser


# ============================================================================
# 参数校验与归一化
# ============================================================================


def validate_args(args: argparse.Namespace) -> List[str]:
    """校验 CLI 参数合法性。

    Args:
        args: 解析后的 argparse 命名空间

    Returns:
        错误描述列表（空列表表示全部通过）
    """
    errors = []

    # --init 与 --check 互斥
    if args.init and args.check:
        errors.append("--init 与 --check 不能同时使用")

    # --mode 与 --safe/--unsafe/--interactive 互斥
    explicit_flags = [args.safe, args.unsafe, args.interactive]
    if args.mode and any(explicit_flags):
        errors.append("--mode 与 --safe/--unsafe/--interactive 不能同时使用")

    # --init 与 goal 互斥
    if args.init and args.goal:
        errors.append("--init 不能与 goal 同时提供（init 模式下不需要需求描述）")

    # --check 不需要 goal（仅警告）
    # （不阻塞，仅记录）

    # max_cycles 边界检查
    if args.max_cycles < 1:
        errors.append(f"--max-cycles 必须 >= 1（当前: {args.max_cycles}）")

    # convergence_rounds 边界检查
    if args.convergence_rounds < 1:
        errors.append(
            f"--convergence-rounds 必须 >= 1（当前: {args.convergence_rounds}）"
        )

    return errors


def resolve_goal(args: argparse.Namespace) -> Optional[str]:
    """从多种输入方式中解析最终的需求描述。

    优先级: 位置参数 goal > --requirement > None

    Args:
        args: 解析后的 argparse 命名空间

    Returns:
        需求描述字符串；无需求时为 None
    """
    return args.goal or args.requirement


def resolve_mode(args: argparse.Namespace) -> str:
    """从多种输入方式中解析最终的运行模式。

    优先级: --mode > --safe/--unsafe/--interactive > 默认 "auto"

    Args:
        args: 解析后的 argparse 命名空间

    Returns:
        运行模式字符串
    """
    if args.mode:
        return args.mode
    if args.safe:
        return "safe"
    if args.unsafe:
        return "unsafe"
    if args.interactive:
        return "collaborative"
    return "auto"


# ============================================================================
# 日志配置
# ============================================================================


def setup_logging(verbose: int, log_file: Optional[str] = None) -> None:
    """配置全局日志。

    Args:
        verbose: 详细级别（0=WARNING, 1=INFO, 2=DEBUG）
        log_file: 日志文件路径（可选）
    """
    level = logging.WARNING
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # 控制台 handler
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)

    root = logging.getLogger("loop_hermes")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)

    # 文件 handler
    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            root.warning("无法创建日志文件 %s: %s", log_file, e)


# ============================================================================
# HLOOP_STATE 输出
# ============================================================================


def print_hloop_state(state: dict, json_mode: bool = False) -> None:
    """输出 HLOOP_STATE block 到 stdout。

    外部调度器通过解析此 block 判定终止条件。

    Args:
        state: state 字典
        json_mode: True 时以 JSON 格式输出（机器可读）
    """
    active = state["issues"]["active"]
    tr = state.get("artifacts", {}).get("test_results", {})
    all_test = "unknown"
    if tr.get("status") in ("generated", "updated"):
        try:
            data = __import__("json").loads(Path(tr["path"]).read_text())
            fails = [r for r in data.get("results", []) if r.get("status") == "fail"]
            all_test = "fail" if fails else "pass"
        except Exception:
            pass

    all_open = len(active["p0"]) + len(active["p1"]) + len(active["p2"])
    pc = state.get("pending_confirmation", {})
    gs = state.get("gate_state", {}).get("hermes_guardrail_events", [])

    if json_mode:
        payload = {
            "phase": state["progress"]["phase"],
            "cycle": state["progress"]["cycle"],
            "convergence_counter": state["progress"]["convergence_counter"],
            "new_issues_this_round": state["progress"]["new_issues_this_round"],
            "issues_active_p0": len(active["p0"]),
            "issues_active_p1": len(active["p1"]),
            "issues_active_p2": len(active["p2"]),
            "all_test_status": all_test,
            "all_issue_status": "none_open" if all_open == 0 else "has_open",
            "pending_confirmation_status": pc.get("status", "null"),
            "termination_status": state["termination"]["status"],
            "max_cycles": state["config"]["max_cycles"],
            "convergence_rounds": state["config"]["convergence_rounds"],
            "hermes_guardrail_hardlines": sum(
                1 for e in gs if e.get("type") == "HARDLINE"
            ),
            "hermes_guardrail_warns": sum(
                1 for e in gs if e.get("type") == "WARN"
            ),
        }
        print("<<<HLOOP_STATE>>>")
        print(__import__("json").dumps(payload, ensure_ascii=False, indent=2))
        print("<<<END_HLOOP_STATE>>>")
    else:
        print("<<<HLOOP_STATE>>>")
        print(f"phase: {state['progress']['phase']}")
        print(f"cycle: {state['progress']['cycle']}")
        print(f"convergence_counter: {state['progress']['convergence_counter']}")
        print(f"new_issues_this_round: {str(state['progress']['new_issues_this_round']).lower()}")
        print(f"issues_active_p0: {len(active['p0'])}")
        print(f"issues_active_p1: {len(active['p1'])}")
        print(f"issues_active_p2: {len(active['p2'])}")
        print(f"all_test_status: {all_test}")
        print(f"all_issue_status: {'none_open' if all_open == 0 else 'has_open'}")
        print(f"pending_confirmation_status: {pc.get('status', 'null')}")
        print(f"termination_status: {state['termination']['status']}")
        print(f"max_cycles: {state['config']['max_cycles']}")
        print(f"convergence_rounds: {state['config']['convergence_rounds']}")
        print(f"hermes_guardrail_hardlines: {sum(1 for e in gs if e.get('type')=='HARDLINE')}")
        print(f"hermes_guardrail_warns: {sum(1 for e in gs if e.get('type')=='WARN')}")
        print("<<<END_HLOOP_STATE>>>")


# ============================================================================
# 主入口函数
# ============================================================================


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口。

    流程:
        1. 解析参数
        2. 配置日志
        3. --init → 强制初始化 state.json
        4. --check → 运行 Sanity Check
        5. 正常模式 → 加载/初始化 state → 检查终止 → 执行 phase → 输出 HLOOP_STATE

    Args:
        argv: 命令行参数列表（用于测试注入；None 时使用 sys.argv）

    Returns:
        退出码（0=成功, 1=错误, 2=参数错误）
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 参数校验
    errors = validate_args(args)
    if errors:
        for e in errors:
            print(f"参数错误: {e}", file=sys.stderr)
        return 2

    # 日志配置
    setup_logging(args.verbose, args.log_file)
    logger = logging.getLogger("loop_hermes.cli")

    # 解析最终配置值
    goal = resolve_goal(args)
    mode = resolve_mode(args)
    state_dir = str(Path(args.state_dir))

    # ---- 操作：--init ----
    if args.init:
        return _cmd_init(state_dir, mode, goal, args, logger)

    # ---- 操作：--check ----
    if args.check:
        return _cmd_check(state_dir, logger)

    # ---- 正常模式：单次循环执行 ----
    return _cmd_run(state_dir, mode, goal, args, logger)


# ============================================================================
# 子命令实现
# ============================================================================


def _cmd_init(
    state_dir: str,
    mode: str,
    goal: str,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> int:
    """--init 子命令：初始化 state.json。

    Args:
        state_dir: state 目录
        mode: 运行模式
        goal: 需求描述
        args: 完整参数对象
        logger: 日志器

    Returns:
        退出码
    """
    import json
    from .state_machine import load_or_init_state

    state_file = Path(state_dir) / "state.json"
    if state_file.exists():
        logger.error("state.json 已存在于 %s，使用 --init --force 覆盖", state_dir)
        print(f"错误: state.json 已存在: {state_file}", file=sys.stderr)
        print("如需重新初始化请先删除或备份现有文件。", file=sys.stderr)
        return 1

    # 伪造一个 args 对象用于 load_or_init_state
    class FakeArgs:
        pass
    fa = FakeArgs()
    fa.state_dir = state_dir
    fa.safe = args.safe
    fa.unsafe = args.unsafe
    fa.interactive = args.interactive
    fa.goal = goal
    fa.max_cycles = args.max_cycles
    fa.convergence_rounds = args.convergence_rounds
    fa.hermes_model = args.hermes_model
    fa.hermes_toolsets = args.hermes_toolsets
    fa.provider_fallback = args.provider_fallback
    fa.skip_testing = args.skip_testing

    state = load_or_init_state(state_dir, fa)
    logger.info("state.json 初始化完成")
    print(f"已创建 state.json: {state_file}")
    print(f"  模式: {mode}")
    print(f"  需求: {goal or '(未指定)'}")
    print(f"  最大轮次: {args.max_cycles}")
    print(f"  收敛轮次: {args.convergence_rounds}")
    return 0


def _cmd_check(state_dir: str, logger: logging.Logger) -> int:
    """--check 子命令：运行 Sanity Check。

    Args:
        state_dir: state 目录
        logger: 日志器

    Returns:
        退出码（0=全部通过, 1=有致命失败）
    """
    from .sanity_check import (
        run_sanity_check,
        print_sanity_report,
        get_blocking_failures,
    )

    logger.info("运行 Sanity Check...")
    results = run_sanity_check(state_dir)
    print_sanity_report(results)

    blocking = get_blocking_failures(results)
    if blocking:
        return 1
    return 0


def _cmd_run(
    state_dir: str,
    mode: str,
    goal: str,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> int:
    """正常运行模式：单次循环执行。

    流程:
        1. 运行 Sanity Check（非致命项仅警告）
        2. 检测 Hermes 引擎
        3. 加载/初始化 state
        4. 检查终止条件
        5. 执行 phase（当前为占位符，M2 接入 phase_dispatch）
        6. 输出 HLOOP_STATE

    Args:
        state_dir: state 目录
        mode: 运行模式
        goal: 需求描述
        args: 完整参数对象
        logger: 日志器

    Returns:
        退出码
    """
    from .sanity_check import run_sanity_check, get_blocking_failures
    from .hermes_client import detect_hermes_engine
    from .state_machine import load_or_init_state, state_exists, is_terminated

    # 1. Sanity Check
    logger.debug("执行启动检查...")
    results = run_sanity_check(state_dir)
    blocking = get_blocking_failures(results)
    if blocking:
        for b in blocking:
            logger.error("致命检查失败 #%d: %s", b["check_id"], b.get("error", ""))
        return 1

    # 2. 检测 Hermes 引擎
    try:
        engine = detect_hermes_engine()
        logger.info("Hermes 引擎: %s", engine)
    except RuntimeError as e:
        logger.error("Hermes 引擎检测失败: %s", e)
        print(f"错误: {e}", file=sys.stderr)
        return 1

    # 3. 加载 state
    # 构造假 args 传给 load_or_init_state
    class FakeArgs:
        pass
    fa = FakeArgs()
    fa.state_dir = state_dir
    fa.safe = args.safe
    fa.unsafe = args.unsafe
    fa.interactive = args.interactive
    fa.goal = goal
    fa.max_cycles = args.max_cycles
    fa.convergence_rounds = args.convergence_rounds
    fa.hermes_model = args.hermes_model
    fa.hermes_toolsets = args.hermes_toolsets
    fa.provider_fallback = args.provider_fallback
    fa.skip_testing = args.skip_testing

    state = load_or_init_state(state_dir, fa)
    state["progress"]["hermes_engine"] = engine
    state["housekeeping"]["invocation_count"] += 1

    phase = state["progress"]["phase"]
    cycle = state["progress"]["cycle"]
    logger.info("当前 phase=%s, cycle=%d, engine=%s", phase, cycle, engine)

    # 4. 终止条件检查
    if is_terminated(state):
        logger.info(
            "工作流已终止: %s (%s)",
            state["termination"]["status"],
            state["termination"].get("exit_reason", ""),
        )
        print_hloop_state(state, args.json_output)
        return 0 if state["termination"]["status"] == "complete" else 1

    # 5. Phase 执行
    from .phase_dispatch import dispatch_phase
    logger.info("Phase 执行: %s (cycle=%d)", phase, cycle)
    result = dispatch_phase(state, state_dir)

    # 记录执行结果
    if result.get("status") == "error":
        logger.error("Phase [%s] 执行失败: %s", phase, result.get("error", "unknown"))
    else:
        logger.info(
            "Phase [%s] → %s: status=%s",
            phase, result.get("phase", phase), result.get("status", "ok"),
        )

    # 6. 输出 HLOOP_STATE
    from .state_machine import atomic_write_state
    atomic_write_state(state, state_dir)
    print_hloop_state(state, args.json_output)

    # 如果是终止状态或错误，返回非 0
    if result.get("status") == "terminated" and state["termination"]["status"] != "complete":
        return 1

    return 0


# ============================================================================
# __main__ 入口
# ============================================================================

if __name__ == "__main__":
    sys.exit(main())
