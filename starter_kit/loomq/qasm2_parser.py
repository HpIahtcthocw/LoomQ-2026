"""OpenQASM 2.0 → 统一 IR。

只覆盖题面承诺会出现的语法：版本行、include、qreg/creg 声明、白名单门调用、
measure、barrier（忽略）。没有循环、条件、自定义 gate 定义。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .ir import Circuit, Gate, Measure, WHITELIST, eval_param, validate

_COMMENT = re.compile(r"//.*?$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_REG_DECL = re.compile(r"^(qreg|creg)\s+([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]$")
_MEASURE = re.compile(r"^measure\s+(.+?)\s*->\s*(.+?)$")
_BIT_REF = re.compile(r"^([A-Za-z_]\w*)\s*\[\s*(\d+)\s*\]$")
_GATE_CALL = re.compile(r"^([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s+(.+)$")


class QasmError(ValueError):
    pass


class _Registers:
    """把多个寄存器展平成一个连续的下标空间，按声明顺序分配。"""

    def __init__(self) -> None:
        self.offsets: Dict[str, int] = {}
        self.sizes: Dict[str, int] = {}
        self.total = 0

    def declare(self, name: str, size: int) -> None:
        if name in self.offsets:
            raise QasmError("寄存器 %r 重复声明" % name)
        self.offsets[name] = self.total
        self.sizes[name] = size
        self.total += size

    def index(self, name: str, position: int) -> int:
        if name not in self.offsets:
            raise QasmError("未声明的寄存器 %r" % name)
        if not 0 <= position < self.sizes[name]:
            raise QasmError("寄存器 %s 的下标 %d 越界" % (name, position))
        return self.offsets[name] + position

    def whole(self, name: str) -> List[int]:
        if name not in self.offsets:
            raise QasmError("未声明的寄存器 %r" % name)
        base = self.offsets[name]
        return list(range(base, base + self.sizes[name]))


def _statements(text: str) -> List[str]:
    text = _BLOCK_COMMENT.sub("", text)
    text = _COMMENT.sub("", text)
    return [chunk.strip() for chunk in text.split(";") if chunk.strip()]


def _resolve(token: str, registers: _Registers) -> List[int]:
    """把 q[2] 或整寄存器 q 解析为展平后的下标列表。"""
    token = token.strip()
    match = _BIT_REF.match(token)
    if match:
        return [registers.index(match.group(1), int(match.group(2)))]
    if re.fullmatch(r"[A-Za-z_]\w*", token):
        return registers.whole(token)
    raise QasmError("无法解析比特引用: %r" % token)


def parse(qasm: str) -> Circuit:
    if not isinstance(qasm, str) or not qasm.strip():
        raise QasmError("输入的 QASM 为空")

    qregs = _Registers()
    cregs = _Registers()
    circuit = Circuit()

    for statement in _statements(qasm):
        lowered = statement.lower()

        if lowered.startswith("openqasm"):
            continue
        if lowered.startswith("include"):
            continue
        if lowered.startswith("barrier"):
            continue

        decl = _REG_DECL.match(statement)
        if decl:
            kind, name, size = decl.group(1), decl.group(2), int(decl.group(3))
            (qregs if kind == "qreg" else cregs).declare(name, size)
            continue

        measure = _MEASURE.match(statement)
        if measure:
            sources = _resolve(measure.group(1), qregs)
            targets = _resolve(measure.group(2), cregs)
            if len(sources) != len(targets):
                raise QasmError("measure 两侧宽度不一致: %r" % statement)
            for qubit, clbit in zip(sources, targets):
                circuit.ops.append(Measure(qubit=qubit, clbit=clbit))
            continue

        call = _GATE_CALL.match(statement)
        if not call:
            raise QasmError("无法解析语句: %r" % statement)

        name = call.group(1).lower()
        if name not in WHITELIST:
            raise QasmError("门 %r 不在 12 门白名单内（语句 %r）" % (name, statement))

        raw_params = call.group(2)
        params: Tuple[float, ...] = ()
        if raw_params and raw_params.strip():
            params = tuple(eval_param(part) for part in raw_params.split(","))

        operand_groups = [_resolve(part, qregs) for part in call.group(3).split(",")]
        expected_qubits = WHITELIST[name][0]
        if len(operand_groups) != expected_qubits:
            raise QasmError("门 %s 需要 %d 个操作数（语句 %r）" % (name, expected_qubits, statement))

        # QASM 2.0 允许整寄存器广播：h q; 等价于对该寄存器每一位各作用一次。
        widths = {len(group) for group in operand_groups}
        if widths == {1}:
            broadcast = 1
        else:
            non_scalar = widths - {1}
            if len(non_scalar) != 1:
                raise QasmError("门 %s 的操作数宽度无法广播（语句 %r）" % (name, statement))
            broadcast = non_scalar.pop()

        for slot in range(broadcast):
            qubits = tuple(
                group[0] if len(group) == 1 else group[slot] for group in operand_groups
            )
            circuit.ops.append(Gate(name=name, qubits=qubits, params=params))

    circuit.n_qubits = qregs.total
    circuit.n_clbits = cregs.total
    if circuit.n_qubits == 0:
        raise QasmError("没有声明任何 qreg")
    validate(circuit)
    return circuit
