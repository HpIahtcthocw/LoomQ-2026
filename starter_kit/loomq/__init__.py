"""LoomQ 统一量子中间层。

分层：
    qasm2_parser  OpenQASM 2.0 → 统一 IR
    ir            Circuit / Gate / Measure，唯一的内部数据结构
    emitters      统一 IR → 各后端原生表示（门名映射集中在此）
    counts        位序与统一结果 Schema 归一化
    backends      真实 SDK 执行层（需安装对应依赖）
"""

from .ir import Circuit, Gate, Measure, WHITELIST
from .qasm2_parser import QasmError, parse
from .emitters import emit, emit_originir, emit_qasm2, emit_qasm3

__all__ = [
    "Circuit",
    "Gate",
    "Measure",
    "WHITELIST",
    "QasmError",
    "parse",
    "emit",
    "emit_qasm2",
    "emit_qasm3",
    "emit_originir",
]
