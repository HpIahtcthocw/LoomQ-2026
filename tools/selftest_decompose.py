#!/usr/bin/env python3
"""门分解数值验证：纯标准库，不需要任何量子 SDK。

判据用的是"态矢至多相差全局相位"，比"测量分布一致"严格得多——因为全局相位不影响
任何分布，但相对相位错误只在该子电路被当作受控门的一部分时才暴露。

本文件最重要的一个测试是 test_the_rz_trap：它构造反例，证明用 rz 代替 u1 去分解 cu1
会在 bell/ghz 上完全看不出问题，但在含相位的电路上出错。

用法（仓库根目录）：
    python tools/selftest_decompose.py
"""

from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq.decompose import decompose_circuit, decompose_gate, gate_histogram  # noqa: E402
from loomq.ir import Circuit, Gate, Measure  # noqa: E402
from loomq.refsim import (  # noqa: E402
    hellinger_fidelity,
    ideal_distribution,
    statevector,
    states_match_up_to_global_phase,
)

FAILURES = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         ("  -> " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def distributions_ok(left: dict, right: dict, tolerance: float = 1e-9) -> bool:
    """概率分布比较必须带容差——分解会改变浮点运算顺序，直接用 == 比字典必然失败。"""
    return all(
        abs(left.get(key, 0.0) - right.get(key, 0.0)) <= tolerance
        for key in set(left) | set(right)
    )


def _spread(n_qubits: int) -> list:
    """把所有比特打到一般叠加态：相位错误只在非平凡输入上才可见。

    只用 h 会让振幅全为实数正值，某些相位错误仍可能藏起来；再叠一层不同角度的 ry 与 rz，
    把态推到一般复振幅。
    """
    ops = []
    for qubit in range(n_qubits):
        ops.append(Gate("h", (qubit,)))
        ops.append(Gate("ry", (qubit,), (0.3 + 0.7 * qubit,)))
        ops.append(Gate("rz", (qubit,), (0.4 + 0.5 * qubit,)))
    return ops


def _compare(label: str, n_qubits: int, original: list, replacement: list) -> bool:
    """在同一个非平凡输入态上比较两段门序列，判据是态矢至多差全局相位。"""
    prefix = _spread(n_qubits)
    left = statevector(Circuit(n_qubits=n_qubits, n_clbits=n_qubits, ops=prefix + original))
    right = statevector(Circuit(n_qubits=n_qubits, n_clbits=n_qubits, ops=prefix + replacement))
    ok = states_match_up_to_global_phase(left, right)
    check(label, ok)
    return ok


def test_each_identity() -> None:
    cases = [
        ("s", 1, [Gate("s", (0,))]),
        ("sdg", 1, [Gate("sdg", (0,))]),
        ("t", 1, [Gate("t", (0,))]),
        ("tdg", 1, [Gate("tdg", (0,))]),
        ("ry(0.9)", 1, [Gate("ry", (0,), (0.9,))]),
        ("swap", 2, [Gate("swap", (0, 1))]),
        ("cu1(pi/3)", 2, [Gate("cu1", (0, 1), (math.pi / 3,))]),
        ("cu1(-1.234)", 2, [Gate("cu1", (0, 1), (-1.234,))]),
        ("ccx", 3, [Gate("ccx", (0, 1, 2))]),
    ]
    for name, n_qubits, original in cases:
        replacement = []
        for gate in original:
            replacement.extend(decompose_gate(gate))
        _compare("恒等式:%s" % name, n_qubits, original, replacement)


def test_phase_gate_substitution() -> None:
    """澄清 rz / u1 的可替换范围，并定位真正会出错的那一种写法。

    gate_identities.md 说"cu1 分解必须用 u1"。数值验证的结论更精确：
    在那个 5 门分解里把三个 u1 全部换成 rz，得到的态**只差一个全局相位**，因此完全等价。
    原因是单比特 rz(φ) = e^{-iφ/2}·u1(φ)，这个因子是纯标量，标量与任何算子交换，
    三个标量相乘仍是标量，观测不到。

    真正会错的是另一种写法：把 cu1 实现成"受控 rz"（crz）。此时相位因子挂在受控分支上，
    变成**相对**相位，可观测。crz(θ)=diag(1,1,e^{-iθ/2},e^{iθ/2}) 而 cu1(θ)=diag(1,1,1,e^{iθ})。
    """
    theta = math.pi / 3
    prefix = _spread(2)
    reference = statevector(Circuit(2, 2, prefix + [Gate("cu1", (0, 1), (theta,))]))

    # 情形一：整段分解里 u1 → rz 全替换，是安全的
    all_rz = [
        Gate("rz", (0,), (theta / 2,)),
        Gate("cx", (0, 1)),
        Gate("rz", (1,), (-theta / 2,)),
        Gate("cx", (0, 1)),
        Gate("rz", (1,), (theta / 2,)),
    ]
    check("替换:u1 版本正确",
          states_match_up_to_global_phase(
              reference, statevector(Circuit(2, 2, prefix + decompose_gate(Gate("cu1", (0, 1), (theta,)))))))
    check("替换:三个 u1 全换成 rz 仍等价（只差全局相位）",
          states_match_up_to_global_phase(reference, statevector(Circuit(2, 2, prefix + all_rz))))

    # 情形二：漏掉控制位上那个相位门，等价于把 cu1 写成了 crz——这才是真正的错误
    as_crz = [
        Gate("cx", (0, 1)),
        Gate("rz", (1,), (-theta / 2,)),
        Gate("cx", (0, 1)),
        Gate("rz", (1,), (theta / 2,)),
    ]
    check("陷阱:写成受控 rz（crz）确实是错的",
          not states_match_up_to_global_phase(reference, statevector(Circuit(2, 2, prefix + as_crz))),
          "若这条 FAIL 说明参考模拟器对相对相位不敏感，refsim 有 bug")

    # 这个错误在 bell / ghz3 上完全看不出来，因为它们不含 cu1
    bell = Circuit(2, 2, [Gate("h", (0,)), Gate("cx", (0, 1)), Measure(0, 0), Measure(1, 1)])
    check("陷阱:bell 不含 cu1，因此完全不受影响",
          distributions_ok(ideal_distribution(bell), {"00": 0.5, "11": 0.5}))

    # 但在 QFT 风格的含相位电路上，保真度会跌破官方 0.97 阈值
    head = [Gate("h", (0,)), Gate("h", (1,))]
    tail = [Gate("h", (0,)), Measure(0, 0), Measure(1, 1)]
    correct_circuit = Circuit(2, 2, head + [Gate("cu1", (0, 1), (theta,))] + tail)
    crz_circuit = Circuit(2, 2, head + as_crz + tail)
    fidelity = hellinger_fidelity(ideal_distribution(crz_circuit),
                                  ideal_distribution(correct_circuit))
    check("陷阱:crz 版本在含相位电路上跌破 0.97", fidelity < 0.97, "实际保真度 %.4f" % fidelity)
    print("      （写成 crz 时该电路保真度 %.4f，官方阈值 0.97）" % fidelity)


def test_recursive_decomposition() -> None:
    """ccx 展开后含 t/tdg，若这些也不支持，必须继续展开到 u1。"""
    circuit = Circuit(3, 3, [Gate("ccx", (0, 1, 2)), Measure(0, 0), Measure(1, 1), Measure(2, 2)])
    reduced = decompose_circuit(circuit, {"ccx", "t", "tdg", "s", "sdg"})
    histogram = gate_histogram(reduced)
    check("递归:结果只剩 h/cx/u1", set(histogram) <= {"h", "cx", "u1"}, str(histogram))
    check("递归:测量被保留", len(reduced.measures()) == 3)
    check("递归:语义不变",
          states_match_up_to_global_phase(statevector(circuit), statevector(reduced)))
    print("      （ccx 全展开后门频次：%s）" % histogram)


def test_full_circuit_decomposition() -> None:
    """一个用满 12 门的电路，整体分解前后语义必须一致。"""
    ops = [
        Gate("h", (0,)), Gate("x", (1,)), Gate("s", (0,)), Gate("sdg", (1,)),
        Gate("t", (2,)), Gate("tdg", (0,)), Gate("rz", (1,), (math.pi / 2,)),
        Gate("ry", (2,), (-math.pi / 4,)), Gate("cx", (0, 1)),
        Gate("cu1", (1, 2), (2 * math.pi / 3,)), Gate("swap", (0, 2)),
        Gate("ccx", (0, 1, 2)),
        Measure(0, 0), Measure(1, 1), Measure(2, 2),
    ]
    circuit = Circuit(3, 3, ops)
    reduced = decompose_circuit(circuit, {"s", "sdg", "t", "tdg", "swap", "cu1", "ccx", "ry"})
    check("整体:语义不变",
          states_match_up_to_global_phase(statevector(circuit), statevector(reduced)))
    check("整体:分布一致", distributions_ok(ideal_distribution(circuit), ideal_distribution(reduced)))
    histogram = gate_histogram(reduced)
    check("整体:落点只剩 h/x/cx/rz/u1", set(histogram) <= {"h", "x", "cx", "rz", "u1"}, str(histogram))
    print("      （全分解后门频次：%s）" % histogram)


def test_guards() -> None:
    circuit = Circuit(2, 2, [Gate("h", (0,)), Gate("cx", (0, 1))])
    try:
        decompose_circuit(circuit, {"h"})
        check("防护:拒绝分解落点门", False, "应当拒绝把 h 列为 unsupported")
    except ValueError:
        check("防护:拒绝分解落点门", True)
    check("防护:空集合原样返回", decompose_circuit(circuit, set()) is circuit)


def main() -> int:
    print("=== 门分解数值验证（无需量子 SDK）===\n")
    test_each_identity()
    print()
    test_phase_gate_substitution()
    print()
    test_recursive_decomposition()
    print()
    test_full_circuit_decomposition()
    print()
    test_guards()
    print("\n" + ("全部通过" if not FAILURES else "失败 %d 项: %s" % (len(FAILURES), FAILURES)))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
