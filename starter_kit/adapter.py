#!/usr/bin/env python3
"""LoomQ submission adapter contract v1.0.

本文件只做契约绑定，实现全部在 loomq/ 包内：
    loomq.qasm2_parser  OpenQASM 2.0 → 统一 IR
    loomq.emitters      统一 IR → 各后端原生表示
    loomq.backends      真实 SDK 执行
    loomq.counts        位序与结果 Schema 归一化
"""

from typing import Any, Dict, List, Tuple

try:  # 评测器可能以 starter_kit 包导入，也可能以 starter_kit 为工作目录直接导入
    from .loomq import agent, backends, emitters, hybrid, parse
except ImportError:  # pragma: no cover
    from loomq import agent, backends, emitters, hybrid, parse


SUPPORTED_TARGETS = ("spinq", "originq", "braket")


def transpile(qasm_str: str, target: str) -> str:
    """Translate OpenQASM 2.0 into the target backend's native representation."""
    if target not in SUPPORTED_TARGETS:
        raise ValueError("target 必须是 %s 之一，收到 %r" % (SUPPORTED_TARGETS, target))
    return emitters.emit(parse(qasm_str), target)


def run(qasm_str: str, target: str, shots: int) -> Dict[str, Any]:
    """Execute a circuit and return the unified result schema from the rules."""
    if target not in SUPPORTED_TARGETS:
        raise ValueError("target 必须是 %s 之一，收到 %r" % (SUPPORTED_TARGETS, target))
    return backends.execute(parse(qasm_str), target, shots)


def agent_chat(prompt: str) -> str:
    """Optional L2 entry point using the documented LOOMQ_LLM_* environment."""
    return agent.agent_chat(prompt)


def compile_hybrid(hybrid_qasm_str: str) -> Tuple[List[str], str]:
    """Optional L3 entry point. Return quantum operations and RISC-V assembly."""
    return hybrid.compile_hybrid(hybrid_qasm_str)
