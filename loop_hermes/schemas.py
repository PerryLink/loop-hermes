# -*- coding: utf-8 -*-
"""JSON Schema 定义。

为 loop-hermes 中所有持久化 JSON 文件提供 jsonschema 校验规则:
    - state.json: 文件状态机核心 Schema
    - gate_state.json: 闸门状态 Schema
    - repair_context: 修复上下文 Schema
    - 05-task-list.json: 任务列表 Schema
    - 08-test-results.json: 测试结果 Schema
    - 09-issue-list.json: 问题清单 Schema

所有 Schema 采用 Draft-7 规范，通过 jsonschema 库进行校验。
"""

from jsonschema import validate, ValidationError
from typing import Dict, Any


# ============================================================================
# STATE_SCHEMA —— state.json 的 JSON Schema
# ============================================================================

STATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "loop-hermes State Schema",
    "description": "loop-hermes 文件驱动状态机的核心 Schema（schema_version=1）",
    "required": [
        "schema_version", "progress", "config",
        "tasks", "issues", "termination"
    ],
    "properties": {
        "schema_version": {
            "type": "integer",
            "enum": [1],
            "description": "Schema 版本号（当前仅支持 v1）"
        },
        "progress": {
            "type": "object",
            "required": ["phase", "cycle", "convergence_counter"],
            "properties": {
                "phase": {
                    "type": "string",
                    "description": "当前工作流阶段（init/part_1_1.../routing）"
                },
                "cycle": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "当前主循环轮次"
                },
                "convergence_counter": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "方案稳定性计数器（越高越稳定）"
                },
                "part1_round": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Part 1 设计气泡内部轮次"
                },
                "new_issues_this_round": {
                    "type": "boolean",
                    "description": "本轮是否发现新问题"
                },
                "new_issues_last_round": {
                    "type": "boolean",
                    "description": "上一轮是否发现新问题"
                },
                "issues_snapshot_at_round_start": {
                    "type": "object",
                    "description": "本轮开始时的 issue 数量快照"
                },
                "retry_count_this_phase": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "当前 phase 重试次数"
                },
                "verification_pass_count": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "验证通过次数"
                },
                "hermes_engine": {
                    "type": "string",
                    "enum": ["sdk", "cli", "unknown"],
                    "description": "Hermes 调用路径（sdk: AIAgent 类 / cli: hermes chat -q）"
                },
                "repair_context": {
                    "description": "修复上下文（null 或 repair_context 对象）"
                },
                "phase_transitions": {
                    "type": "array",
                    "description": "phase 转移历史记录"
                }
            }
        },
        "config": {
            "type": "object",
            "required": ["mode", "user_request"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["safe", "auto", "unsafe", "collaborative"],
                    "description": "运行模式"
                },
                "user_request": {
                    "type": "string",
                    "description": "用户的需求描述"
                },
                "max_cycles": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "最大循环轮次上限"
                },
                "convergence_rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "收敛所需连续无问题轮次"
                },
                "max_part1_rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Part 1 设计气泡最大轮次"
                },
                "route_repeat_max": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "同路由点最大重复次数"
                },
                "skip_testing": {
                    "type": "boolean",
                    "description": "是否跳过测试阶段"
                },
                "provider_fallback_chain": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "loop-hermes 自身 LLM 调用的 provider 回退链"
                },
                "hermes_model": {
                    "type": "string",
                    "description": "Hermes Agent 使用的模型 ID"
                },
                "hermes_toolsets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hermes Agent 启用的工具集"
                },
                "hermes_commit_pin": {
                    "type": "string",
                    "description": "Hermes Agent Git commit hash"
                },
                "gate_file_count_threshold": {
                    "type": "object",
                    "description": "按模式分级的文件变更阈值"
                },
                "gate_irreversible_ops_blocked_in": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "哪些模式下拦截不可逆操作"
                },
                "parallel_config": {
                    "type": "object",
                    "description": "并行委派配置",
                    "properties": {
                        "max_parallel_agents": {"type": "integer"},
                        "per_agent_timeout_seconds": {"type": "integer"},
                        "parallel_total_timeout_seconds": {"type": "integer"},
                        "parallel_fail_fast": {"type": "boolean"},
                        "parallel_conflict_detection": {"type": "boolean"},
                        "parallel_guardrail_aggregation": {"type": "boolean"}
                    }
                }
            }
        },
        "tasks": {
            "type": "object",
            "required": ["total", "by_status"],
            "properties": {
                "total": {
                    "type": "integer",
                    "description": "任务总数"
                },
                "by_status": {
                    "type": "object",
                    "properties": {
                        "completed": {"type": "integer"},
                        "in_progress": {"type": "integer"},
                        "pending": {"type": "integer"},
                        "failed": {"type": "integer"},
                        "skipped": {"type": "integer"}
                    }
                }
            }
        },
        "issues": {
            "type": "object",
            "required": ["active", "resolved", "all_time"],
            "properties": {
                "active": {
                    "type": "object",
                    "properties": {
                        "p0": {"type": "array", "items": {"type": "object"}},
                        "p1": {"type": "array", "items": {"type": "object"}},
                        "p2": {"type": "array", "items": {"type": "object"}}
                    }
                },
                "resolved": {
                    "type": "object",
                    "properties": {
                        "p0": {"type": "integer"},
                        "p1": {"type": "integer"},
                        "p2": {"type": "integer"}
                    }
                },
                "all_time": {
                    "type": "object",
                    "properties": {
                        "p0_total": {"type": "integer"},
                        "p1_total": {"type": "integer"},
                        "p2_total": {"type": "integer"}
                    }
                }
            }
        },
        "artifacts": {
            "type": "object",
            "description": "所有 artifact 文件的元信息（路径、checksum、版本等）"
        },
        "routing_history": {
            "type": "array",
            "description": "路由决策历史"
        },
        "routing_repeat_tracker": {
            "type": "object",
            "description": "路由重复追踪（同一决策点重复次数）"
        },
        "gate_state": {
            "type": "object",
            "required": [
                "content_safety_passed", "plan_confirmed",
                "file_modifications_this_cycle", "dangerous_ops_blocked",
                "hermes_guardrail_events"
            ],
            "properties": {
                "content_safety_passed": {"type": "boolean"},
                "plan_confirmed": {"type": "boolean"},
                "plan_confirmed_by": {},
                "file_modifications_this_cycle": {"type": "integer"},
                "dangerous_ops_blocked": {"type": "array"},
                "hermes_guardrail_events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["HARDLINE", "WARN", "APPROVAL_DENY"]
                            },
                            "tool": {"type": "string"},
                            "message": {"type": "string"},
                            "timestamp": {"type": "string"}
                        }
                    }
                }
            }
        },
        "termination": {
            "type": "object",
            "required": ["status"],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["running", "complete", "paused", "failed"],
                    "description": "终止状态"
                },
                "completed_at": {
                    "description": "完成时间（ISO 8601）"
                },
                "exit_reason": {
                    "description": "退出原因"
                }
            }
        },
        "pending_confirmation": {
            "type": "object",
            "description": "协作模式下的待确认信息"
        },
        "phase_contracts": {
            "type": "object",
            "description": "当前 phase 的合约定义"
        },
        "context_snapshot": {
            "type": "object",
            "description": "上下文快照"
        },
        "housekeeping": {
            "type": "object",
            "description": "内部管理字段"
        }
    }
}


# ============================================================================
# TASK_LIST_SCHEMA —— 05-task-list.json 的 JSON Schema
# ============================================================================

TASK_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "Task List Schema",
    "description": "Part 2.1 产出的任务列表 JSON 文件 Schema",
    "required": ["meta", "tasks", "summary"],
    "properties": {
        "meta": {
            "type": "object",
            "required": ["project", "generated_by_phase", "generated_at"],
            "properties": {
                "project": {"type": "string"},
                "generated_by_phase": {"type": "string"},
                "generated_at": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
                "total_estimated_hours": {"type": "number"}
            }
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "status", "priority", "module",
                             "assigned_files", "dependencies"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "skipped"]
                    },
                    "priority": {"type": "integer", "minimum": 1},
                    "module": {"type": "string"},
                    "assigned_files": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "estimated_lines": {"type": "integer"},
                    "estimated_minutes": {"type": "integer"},
                    "verification_method": {"type": "string"},
                    "linked_issue_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "retry_count": {"type": "integer", "minimum": 0},
                    "hermes_sub_agent_id": {"type": "string"}
                }
            }
        },
        "summary": {
            "type": "object",
            "properties": {
                "total": {"type": "integer"},
                "by_status": {
                    "type": "object",
                    "properties": {
                        "completed": {"type": "integer"},
                        "in_progress": {"type": "integer"},
                        "pending": {"type": "integer"},
                        "failed": {"type": "integer"},
                        "skipped": {"type": "integer"}
                    }
                }
            }
        }
    }
}


# ============================================================================
# TEST_RESULTS_SCHEMA —— 08-test-results.json 的 JSON Schema
# ============================================================================

TEST_RESULTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "Test Results Schema",
    "description": "Part 2.6 产出的测试结果 JSON 文件 Schema",
    "required": ["meta", "results", "summary", "promoted_issues"],
    "properties": {
        "meta": {
            "type": "object",
            "required": ["generated_by_phase", "generated_at"],
            "properties": {
                "generated_by_phase": {"type": "string"},
                "generated_at": {"type": "string"},
                "test_framework": {"type": "string"},
                "total_duration_ms": {"type": "integer"}
            }
        },
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "name", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pass", "fail", "skip", "error"]
                    },
                    "duration_ms": {"type": "integer"},
                    "error_message": {"type": "string"},
                    "module": {"type": "string"},
                    "linked_task_id": {"type": "string"}
                }
            }
        },
        "summary": {
            "type": "object",
            "properties": {
                "total": {"type": "integer"},
                "pass": {"type": "integer"},
                "fail": {"type": "integer"},
                "skip": {"type": "integer"},
                "error": {"type": "integer"},
                "pass_rate": {"type": "number"}
            }
        },
        "promoted_issues": {
            "type": "array",
            "description": "从测试失败提升为 issue 的条目",
            "items": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string"},
                    "issue_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2"]}
                }
            }
        }
    }
}


# ============================================================================
# ISSUE_LIST_SCHEMA —— 09-issue-list.json 的 JSON Schema
# ============================================================================

ISSUE_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "Issue List Schema",
    "description": "Part 2.7 产出的问题清单 JSON 文件 Schema",
    "required": ["meta", "issues", "summary"],
    "properties": {
        "meta": {
            "type": "object",
            "required": ["generated_by_phase", "generated_at"],
            "properties": {
                "generated_by_phase": {"type": "string"},
                "generated_at": {"type": "string"},
                "version": {"type": "integer"}
            }
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "severity", "title", "source", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2"]
                    },
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": [
                            "test_failure", "code_review", "manual_inspection",
                            "build_error", "lint_warning", "external_event",
                            "self_check", "hermes_guardrail"
                        ]
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "in_progress", "fixed", "verified",
                                 "wont_fix", "duplicate"]
                    },
                    "affected_files": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "linked_task_ids": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "fix_strategy": {"type": "string"},
                    "discovered_in_phase": {"type": "string"},
                    "source_ref": {"type": "string"}
                }
            }
        },
        "summary": {
            "type": "object",
            "properties": {
                "total": {"type": "integer"},
                "by_severity": {
                    "type": "object",
                    "properties": {
                        "p0": {"type": "integer"},
                        "p1": {"type": "integer"},
                        "p2": {"type": "integer"}
                    }
                },
                "by_status": {
                    "type": "object",
                    "properties": {
                        "open": {"type": "integer"},
                        "in_progress": {"type": "integer"},
                        "fixed": {"type": "integer"},
                        "verified": {"type": "integer"}
                    }
                }
            }
        }
    }
}


# ============================================================================
# GATE_STATE_SCHEMA —— gate_state 字段展开 Schema
# ============================================================================

GATE_STATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "Gate State Schema",
    "description": "安全闸门状态独立 Schema",
    "required": [
        "content_safety_passed", "plan_confirmed",
        "file_modifications_this_cycle", "dangerous_ops_blocked",
        "hermes_guardrail_events"
    ],
    "properties": {
        "content_safety_passed": {
            "type": "boolean",
            "description": "内容安全检查是否通过"
        },
        "plan_confirmed": {
            "type": "boolean",
            "description": "方案是否已确认"
        },
        "plan_confirmed_by": {
            "description": "确认者（null / 'user' / 'auto'）"
        },
        "file_modifications_this_cycle": {
            "type": "integer",
            "minimum": 0,
            "description": "本周期内文件修改总数"
        },
        "dangerous_ops_blocked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "reason": {"type": "string"},
                    "blocked_at": {"type": "string"}
                }
            },
            "description": "被拦截的危险操作列表"
        },
        "hermes_guardrail_events": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "tool", "message", "timestamp"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["HARDLINE", "WARN", "APPROVAL_DENY"]
                    },
                    "tool": {"type": "string"},
                    "message": {"type": "string"},
                    "timestamp": {"type": "string"}
                }
            },
            "description": "本 cycle 内触发的 Hermes guardrail 事件"
        }
    }
}


# ============================================================================
# REPAIR_CONTEXT_SCHEMA —— repair_context 对象 Schema
# ============================================================================

REPAIR_CONTEXT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "title": "Repair Context Schema",
    "description": "修复上下文对象 Schema（routing 判定 P1(实现级) 或 P2 后创建）",
    "required": ["from_phase", "routing_reason", "target_issues", "affected_files"],
    "properties": {
        "from_phase": {
            "type": "string",
            "description": "创建 repair_context 的 phase（通常为 routing）"
        },
        "routing_reason": {
            "type": "string",
            "description": "路由到此修复的原因描述"
        },
        "target_issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需要修复的 issue ID 列表"
        },
        "repair_plan": {
            "description": "修复计划（null 或修复策略描述）"
        },
        "attempt_number": {
            "type": "integer",
            "minimum": 1,
            "description": "修复尝试次数"
        },
        "review_required": {
            "type": "boolean",
            "description": "是否需要 Code Review"
        },
        "affected_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "受影响的文件列表（修复模式下仅修改这些文件）"
        },
        "hermes_guardrail_source": {
            "type": "boolean",
            "description": "是否由 Hermes guardrail 事件触发"
        }
    }
}


# ============================================================================
# Schema 校验函数
# ============================================================================

def validate_state(state: dict) -> None:
    """校验 state.json 是否符合 STATE_SCHEMA。

    先检查 schema_version 兼容性，再执行完整 jsonschema 校验。

    Args:
        state: 待校验的 state 字典

    Raises:
        ValueError: Schema 校验失败或版本不兼容
    """
    version = state.get("schema_version")
    if version != 1:
        raise ValueError(
            f"不支持的 schema_version: {version}（当前仅支持 v1）。"
            f"请升级 loop-hermes 或迁移 state.json。"
        )
    try:
        validate(instance=state, schema=STATE_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"state.json Schema 校验失败: {e.message}") from e


def validate_task_list(data: dict) -> None:
    """校验任务列表数据是否符合 TASK_LIST_SCHEMA。

    Args:
        data: 待校验的任务列表字典

    Raises:
        ValueError: Schema 校验失败
    """
    try:
        validate(instance=data, schema=TASK_LIST_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"任务列表 Schema 校验失败: {e.message}") from e


def validate_test_results(data: dict) -> None:
    """校验测试结果数据是否符合 TEST_RESULTS_SCHEMA。

    Args:
        data: 待校验的测试结果字典

    Raises:
        ValueError: Schema 校验失败
    """
    try:
        validate(instance=data, schema=TEST_RESULTS_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"测试结果 Schema 校验失败: {e.message}") from e


def validate_issue_list(data: dict) -> None:
    """校验问题清单数据是否符合 ISSUE_LIST_SCHEMA。

    Args:
        data: 待校验的问题清单字典

    Raises:
        ValueError: Schema 校验失败
    """
    try:
        validate(instance=data, schema=ISSUE_LIST_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"问题清单 Schema 校验失败: {e.message}") from e


def validate_gate_state(data: dict) -> None:
    """校验闸门状态数据是否符合 GATE_STATE_SCHEMA。

    Args:
        data: 待校验的闸门状态字典

    Raises:
        ValueError: Schema 校验失败
    """
    try:
        validate(instance=data, schema=GATE_STATE_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"闸门状态 Schema 校验失败: {e.message}") from e


def validate_repair_context(data: dict) -> None:
    """校验修复上下文数据是否符合 REPAIR_CONTEXT_SCHEMA。

    Args:
        data: 待校验的修复上下文字典

    Raises:
        ValueError: Schema 校验失败
    """
    try:
        validate(instance=data, schema=REPAIR_CONTEXT_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"修复上下文 Schema 校验失败: {e.message}") from e
