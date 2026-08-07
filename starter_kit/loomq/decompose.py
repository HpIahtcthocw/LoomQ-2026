"""门分解：把白名单门改写成更基础的门，供不支持该门的后端执行。

恒等式来自 starter_kit/gate_identities.md，全部由 tools/selftest_decompose.py 用参考
模拟器逐个数值验证（判据是"态矢至多相差全局相位"，比"分布一致"更严格）。

**只用于真实执行**。交给评测器判定的 transpile() 输出应保持白名单原门——三个目标的
IR 契约都接受这 12 个门，先分解只会增加出错面。

--- 关于 rz 与 u1，数值验证后的准确结论 ---
gate_identities.md 写"cu1 分解必须用 u1，作为受控门分解的组成部分时 rz 不可互换"。
实测结论比这更精确（见 tools/selftest_decompose.py 的 test_phase_gate_substitution）：

  把下面 cu1 分解里的三个 u1 **全部**换成 rz，结果只差一个全局相位，完全等价。
  因为单比特 rz(φ) = e^{-iφ/2}·u1(φ)，这个因子是纯标量；标量与一切算子交换，
  三个标量相乘仍是标量，任何测量都观测不到。

真正会出错的是把 cu1 实现成**受控 rz**（crz）：
    crz(θ) = diag(1, 1, e^{-iθ/2}, e^{iθ/2})
    cu1(θ) = diag(1, 1, 1,        e^{iθ})
此时相位挂在受控分支上，成了**相对**相位，可观测。实测在一个 QFT 风格的两比特电路上
保真度只有 0.8154，远低于官方 0.97 阈值；而 bell / ghz 不含 cu1，完全看不出问题。

落到实操上：各平台的受控相位门要认准语义是 diag(1,1,1,e^{iθ})——
Braket 用 cphaseshift（正确），OpenQASM 3 stdgates 用 cp（正确），
OriginIR 契约写的是 CU1/CR（需在真机验证阶段确认 CR 是 cu1 语义而非 crz）。
永远不要用"受控 rz"去顶替 cu1。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Set

from .ir import Circuit, Gate, Measure


def _phase_gate(qubit: int, angle: float) -> Gate:
    return Gate("u1", (qubit,), (angle,))


def decompose_gate(gate: Gate) -> List[Gate]:
    """把一个门展开为等价序列。不可分解或无需分解时返回 [gate] 本身。"""
    name = gate.name

    # 1. 相位门家族都是 u1 的特例
    if name == "s":
        return [_phase_gate(gate.qubits[0], math.pi / 2)]
    if name == "sdg":
        return [_phase_gate(gate.qubits[0], -math.pi / 2)]
    if name == "t":
        return [_phase_gate(gate.qubits[0], math.pi / 4)]
    if name == "tdg":
        return [_phase_gate(gate.qubits[0], -math.pi / 4)]

    # 3. swap = 3 个 cx
    if name == "swap":
        a, b = gate.qubits
        return [Gate("cx", (a, b)), Gate("cx", (b, a)), Gate("cx", (a, b))]

    # 4. cu1(θ)：必须用 u1，用 rz 会错（见模块文档）
    if name == "cu1":
        a, b = gate.qubits
        theta = gate.params[0]
        return [
            _phase_gate(a, theta / 2),
            Gate("cx", (a, b)),
            _phase_gate(b, -theta / 2),
            Gate("cx", (a, b)),
            _phase_gate(b, theta / 2),
        ]

    # 5. ccx：qelib1 标准分解，2 比特门 6 个 + 单比特门 9 个
    if name == "ccx":
        a, b, c = gate.qubits
        return [
            Gate("h", (c,)),
            Gate("cx", (b, c)),
            Gate("tdg", (c,)),
            Gate("cx", (a, c)),
            Gate("t", (c,)),
            Gate("cx", (b, c)),
            Gate("tdg", (c,)),
            Gate("cx", (a, c)),
            Gate("t", (b,)),
            Gate("t", (c,)),
            Gate("h", (c,)),
            Gate("cx", (a, b)),
            Gate("t", (a,)),
            Gate("tdg", (b,)),
            Gate("cx", (a, b)),
        ]

    # 6. ry 兜底：sdg · h · rz(θ) · h · s（按行序依次施加）
    if name == "ry":
        qubit = gate.qubits[0]
        return [
            Gate("sdg", (qubit,)),
            Gate("h", (qubit,)),
            Gate("rz", (qubit,), (gate.params[0],)),
            Gate("h", (qubit,)),
            Gate("s", (qubit,)),
        ]

    return [gate]


DECOMPOSABLE: Set[str] = {"s", "sdg", "t", "tdg", "swap", "cu1", "ccx", "ry"}


def decompose_circuit(
    circuit: Circuit,
    unsupported: Iterable[str],
    max_depth: int = 8,
) -> Circuit:
    """把 unsupported 里列出的门递归展开。

    递归是必要的：ccx 展开后含 t/tdg，若该后端也不支持 t，还要继续展开成 u1。
    """
    targets = set(unsupported)
    if not targets:
        return circuit

    unknown = targets - DECOMPOSABLE
    if unknown:
        raise ValueError(
            "没有 %s 的分解规则。h / x / cx / rz / u1 是分解的落点，必须由后端原生支持"
            % ", ".join(sorted(unknown))
        )

    ops: List[object] = list(circuit.ops)
    for _ in range(max_depth):
        if not any(isinstance(op, Gate) and op.name in targets for op in ops):
            break
        expanded: List[object] = []
        for op in ops:
            if isinstance(op, Gate) and op.name in targets:
                expanded.extend(decompose_gate(op))
            else:
                expanded.append(op)
        ops = expanded
    else:
        raise ValueError("分解未收敛，检查 unsupported 是否包含了分解落点门（如 cx 或 u1）")

    return Circuit(n_qubits=circuit.n_qubits, n_clbits=circuit.n_clbits, ops=ops)  # type: ignore[arg-type]


def gate_histogram(circuit: Circuit) -> Dict[str, int]:
    """门频次统计，用于确认分解前后的门集变化。"""
    histogram: Dict[str, int] = {}
    for op in circuit.ops:
        if isinstance(op, Gate):
            histogram[op.name] = histogram.get(op.name, 0) + 1
    return histogram


__all__ = [
    "DECOMPOSABLE",
    "decompose_gate",
    "decompose_circuit",
    "gate_histogram",
]
