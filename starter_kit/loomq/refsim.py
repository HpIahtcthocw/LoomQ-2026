"""参考态矢模拟器：纯标准库，不依赖任何量子 SDK。

用途（这是整个项目的离线验证基座）：
  1. 在没装任何 SDK 的机器上算出任意电路的**理想分布**，用于自造隐藏电路回归集；
  2. 数值验证 gate_identities.md 的门分解写对了没有（尤其是 cu1 必须用 u1 而非 rz）；
  3. 作为 L2 Agent 自验闭环的判定器——比调真实后端快，且不需要 SDK 就能跑。

约定与大赛一致：态矢下标的第 k 个二进制位对应 q[k]（q[0] 是最低位），
测量结果 key 为 c[n-1]…c[1]c[0]，**最右字符是 c[0]**。

这是精确模拟，不是采样。返回的是概率分布，不是 counts。
"""

from __future__ import annotations

import cmath
import math
import random
from typing import Dict, List, Sequence, Tuple

from .ir import Circuit, Gate, Measure

Matrix = Tuple[complex, complex, complex, complex]  # 行主序 [[a,b],[c,d]]

_SQRT_HALF = 1.0 / math.sqrt(2.0)


def _single_qubit_matrix(gate: Gate) -> Matrix:
    name = gate.name
    if name == "h":
        return (_SQRT_HALF, _SQRT_HALF, _SQRT_HALF, -_SQRT_HALF)
    if name == "x":
        return (0j, 1 + 0j, 1 + 0j, 0j)
    if name == "s":
        return (1 + 0j, 0j, 0j, 1j)
    if name == "sdg":
        return (1 + 0j, 0j, 0j, -1j)
    if name == "t":
        return (1 + 0j, 0j, 0j, cmath.exp(1j * math.pi / 4))
    if name == "tdg":
        return (1 + 0j, 0j, 0j, cmath.exp(-1j * math.pi / 4))
    if name == "u1":
        # u1(θ) = diag(1, e^{iθ})。与 rz(θ) 相差全局相位 e^{-iθ/2}。
        return (1 + 0j, 0j, 0j, cmath.exp(1j * gate.params[0]))
    if name == "rz":
        # rz(θ) = diag(e^{-iθ/2}, e^{iθ/2})
        theta = gate.params[0]
        return (cmath.exp(-0.5j * theta), 0j, 0j, cmath.exp(0.5j * theta))
    if name == "ry":
        theta = gate.params[0]
        cos, sin = math.cos(theta / 2), math.sin(theta / 2)
        return (cos + 0j, -sin + 0j, sin + 0j, cos + 0j)
    raise ValueError("refsim 不认识单比特门 %r" % name)


def _apply_controlled_1q(
    state: List[complex],
    n_qubits: int,
    target: int,
    matrix: Matrix,
    controls: Sequence[int] = (),
) -> None:
    a, b, c, d = matrix
    target_bit = 1 << target
    control_mask = 0
    for control in controls:
        control_mask |= 1 << control

    for index in range(len(state)):
        if index & target_bit:
            continue  # 每一对只处理 target 位为 0 的那一侧
        if control_mask and (index & control_mask) != control_mask:
            continue
        partner = index | target_bit
        low, high = state[index], state[partner]
        state[index] = a * low + b * high
        state[partner] = c * low + d * high


def _apply_gate(state: List[complex], n_qubits: int, gate: Gate) -> None:
    name = gate.name

    if name == "cx":
        control, target = gate.qubits
        _apply_controlled_1q(state, n_qubits, target, _single_qubit_matrix(Gate("x", (target,))), (control,))
        return

    if name == "ccx":
        first, second, target = gate.qubits
        _apply_controlled_1q(
            state, n_qubits, target, _single_qubit_matrix(Gate("x", (target,))), (first, second)
        )
        return

    if name == "cu1":
        # cu1(θ) = diag(1,1,1,e^{iθ})：只在两个比特都为 1 时施加相位，两个比特对称。
        first, second = gate.qubits
        phase = cmath.exp(1j * gate.params[0])
        mask = (1 << first) | (1 << second)
        for index in range(len(state)):
            if (index & mask) == mask:
                state[index] *= phase
        return

    if name == "swap":
        first, second = gate.qubits
        first_bit, second_bit = 1 << first, 1 << second
        for index in range(len(state)):
            if (index & first_bit) and not (index & second_bit):
                partner = (index & ~first_bit) | second_bit
                state[index], state[partner] = state[partner], state[index]
        return

    _apply_controlled_1q(state, n_qubits, gate.qubits[0], _single_qubit_matrix(gate))


def statevector(circuit: Circuit) -> List[complex]:
    """运行电路的量子部分，返回末态态矢。测量被忽略（无中途测量依赖）。"""
    if circuit.n_qubits > 22:
        raise ValueError("参考模拟器上限 22 比特，当前 %d" % circuit.n_qubits)
    state: List[complex] = [0j] * (1 << circuit.n_qubits)
    state[0] = 1 + 0j
    for op in circuit.ops:
        if isinstance(op, Gate):
            _apply_gate(state, circuit.n_qubits, op)
    return state


def ideal_distribution(circuit: Circuit, cutoff: float = 1e-12) -> Dict[str, float]:
    """理想测量分布，key 遵循大赛约定（最右字符是 c[0]）。

    只有被 measure 覆盖的比特进入 key，其余经典位固定为 0；对未测量的量子比特做边缘化求和。
    """
    state = statevector(circuit)
    width = circuit.n_clbits or circuit.n_qubits
    mapping = [(op.qubit, op.clbit) for op in circuit.ops if isinstance(op, Measure)]
    if not mapping:
        mapping = [(index, index) for index in range(circuit.n_qubits)]

    distribution: Dict[str, float] = {}
    for index, amplitude in enumerate(state):
        probability = abs(amplitude) ** 2
        if probability <= cutoff:
            continue
        bits = ["0"] * width
        for qubit, clbit in mapping:
            if (index >> qubit) & 1:
                bits[width - 1 - clbit] = "1"
        key = "".join(bits)
        distribution[key] = distribution.get(key, 0.0) + probability

    total = sum(distribution.values())
    if total > 0:
        distribution = {key: value / total for key, value in distribution.items()}
    return distribution


def sample(circuit: Circuit, shots: int, seed: int | None = None) -> Dict[str, int]:
    """按理想分布采样出 counts，总和精确等于 shots。用于本地端到端演练。"""
    distribution = ideal_distribution(circuit)
    keys = sorted(distribution)
    weights = [distribution[key] for key in keys]
    rng = random.Random(seed)
    counts: Dict[str, int] = {key: 0 for key in keys}
    for _ in range(shots):
        counts[rng.choices(keys, weights=weights, k=1)[0]] += 1
    return {key: value for key, value in counts.items() if value}


def hellinger_fidelity(observed: Dict[str, float], expected: Dict[str, float]) -> float:
    """与 evaluator.py 中官方实现一致的保真度，便于本地按同一尺子判定。"""
    states = set(observed) | set(expected)
    distance = math.sqrt(
        sum(
            (math.sqrt(observed.get(state, 0.0)) - math.sqrt(expected.get(state, 0.0))) ** 2
            for state in states
        )
    ) / math.sqrt(2.0)
    return max(0.0, min(1.0, 1.0 - distance))


def distributions_match(
    left: Dict[str, float], right: Dict[str, float], tolerance: float = 1e-9
) -> bool:
    """精确分布比较。用于门分解验证——分解正确时两个分布应当逐项相等，而非近似。"""
    for key in set(left) | set(right):
        if abs(left.get(key, 0.0) - right.get(key, 0.0)) > tolerance:
            return False
    return True


def states_match_up_to_global_phase(
    left: Sequence[complex], right: Sequence[complex], tolerance: float = 1e-9
) -> bool:
    """态矢是否只相差一个全局相位。

    这是比"分布一致"更强的判据：全局相位不影响任何测量分布，但**相对**相位错误会在
    该态被用作受控门的一部分时暴露出来。验证 cu1 分解时必须用这个，不能只比分布。
    """
    if len(left) != len(right):
        return False
    reference = None
    for a, b in zip(left, right):
        if abs(a) > tolerance or abs(b) > tolerance:
            if abs(a) <= tolerance or abs(b) <= tolerance:
                return False
            ratio = b / a
            if reference is None:
                reference = ratio
            elif abs(ratio - reference) > tolerance:
                return False
    return reference is None or abs(abs(reference) - 1.0) <= tolerance
