"""统一中间表示：LoomQ 转译层的唯一内部数据结构。

所有后端发射器都只读这里定义的 Circuit，任何后端特定的知识都不允许进入本模块。
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple, Union

# 输入白名单：name -> (作用比特数, 参数个数)。题面承诺评测电路只使用这 12 个门，
# 解析器据此拒绝白名单外的输入。
WHITELIST: Dict[str, Tuple[int, int]] = {
    "h": (1, 0),
    "x": (1, 0),
    "s": (1, 0),
    "sdg": (1, 0),
    "t": (1, 0),
    "tdg": (1, 0),
    "rz": (1, 1),
    "ry": (1, 1),
    "cx": (2, 0),
    "cu1": (2, 1),
    "swap": (2, 0),
    "ccx": (3, 0),
}

# IR 内部允许的门 = 输入白名单 + u1。
# u1 永远不来自输入，只由 decompose 产生：gate_identities.md 的相位门与 cu1 分解都落在 u1 上，
# 而 u1 与 rz 差一个全局相位，在受控门分解里不可互换，所以必须作为独立门存在。
IR_GATES: Dict[str, Tuple[int, int]] = dict(WHITELIST, u1=(1, 1))


@dataclass(frozen=True)
class Gate:
    name: str
    qubits: Tuple[int, ...]
    params: Tuple[float, ...] = ()


@dataclass(frozen=True)
class Measure:
    qubit: int
    clbit: int


Op = Union[Gate, Measure]


@dataclass
class Circuit:
    n_qubits: int = 0
    n_clbits: int = 0
    ops: List[Op] = field(default_factory=list)

    def gates(self) -> List[Gate]:
        return [op for op in self.ops if isinstance(op, Gate)]

    def measures(self) -> List[Measure]:
        return [op for op in self.ops if isinstance(op, Measure)]

    def depth_estimate(self) -> int:
        """粗略深度：按比特占用逐层累加，仅用于结果 meta，不参与判定。"""
        last: Dict[int, int] = {}
        depth = 0
        for op in self.ops:
            targets = op.qubits if isinstance(op, Gate) else (op.qubit,)
            level = max((last.get(q, 0) for q in targets), default=0) + 1
            for q in targets:
                last[q] = level
            depth = max(depth, level)
        return depth


# --- 参数表达式求值 ---------------------------------------------------------
# OpenQASM 允许 pi/2、-pi/4、2*pi/3 这类表达式，必须求值而不能当字符串搬运。
# 用 ast 白名单而非 eval，避免任意代码执行。

_CONSTANTS = {"pi": math.pi, "PI": math.pi, "e": math.e, "tau": math.tau}
_FUNCTIONS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "ln": math.log,
}


def eval_param(expr: str) -> float:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError("无法解析门参数表达式: %r" % expr) from exc
    return _eval_node(tree.body, expr)


def _eval_node(node: ast.AST, source: str) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("门参数只允许数值: %r" % source)
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError("未知的参数符号 %r（表达式 %r）" % (node.id, source))
    if isinstance(node, ast.UnaryOp):
        value = _eval_node(node.operand, source)
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.USub):
            return -value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, source)
        right = _eval_node(node.right, source)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            return left**right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name in _FUNCTIONS and len(node.args) == 1 and not node.keywords:
            return _FUNCTIONS[name](_eval_node(node.args[0], source))
    raise ValueError("门参数表达式中出现不支持的语法: %r" % source)


def format_param(value: float) -> str:
    """稳定的参数序列化：足够精度且不产生 1e-17 这类噪声尾巴。"""
    text = "%.12g" % value
    return "0" if text in ("-0", "-0.0") else text


def validate(circuit: Circuit) -> None:
    """结构自检。在解析之后、发射之前调用，把错误挡在转译层内部。"""
    for op in circuit.ops:
        if isinstance(op, Measure):
            if not 0 <= op.qubit < circuit.n_qubits:
                raise ValueError("measure 的量子比特下标越界: %d" % op.qubit)
            if not 0 <= op.clbit < circuit.n_clbits:
                raise ValueError("measure 的经典比特下标越界: %d" % op.clbit)
            continue
        if op.name not in IR_GATES:
            raise ValueError("门 %r 不是合法的 IR 门" % op.name)
        n_qubits, n_params = IR_GATES[op.name]
        if len(op.qubits) != n_qubits:
            raise ValueError("门 %s 需要 %d 个比特，收到 %d 个" % (op.name, n_qubits, len(op.qubits)))
        if len(op.params) != n_params:
            raise ValueError("门 %s 需要 %d 个参数，收到 %d 个" % (op.name, n_params, len(op.params)))
        if len(set(op.qubits)) != len(op.qubits):
            raise ValueError("门 %s 的作用比特重复: %s" % (op.name, op.qubits))
        for qubit in op.qubits:
            if not 0 <= qubit < circuit.n_qubits:
                raise ValueError("门 %s 的比特下标越界: %d" % (op.name, qubit))


def measured_qubits(circuit: Circuit) -> Sequence[int]:
    seen: List[int] = []
    for measure in circuit.measures():
        if measure.qubit not in seen:
            seen.append(measure.qubit)
    return seen
