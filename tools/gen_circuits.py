#!/usr/bin/env python3
"""生成隐藏电路回归集及其理想分布。纯标准库，不需要任何量子 SDK。

评测的 8 个电路里只有 bell / ghz3 随 Starter Kit 公开，其余（GHZ-5、QFT-4、Grover-3、
Random×3）由组织方用私有种子在内存中生成，选手拿不到。所以必须自己造同类电路来回归，
否则位序、相位、门覆盖上的错误要等到正式评测才暴露。

理想分布由 loomq.refsim 精确计算（不是采样），可直接作为判定基准。

用法（仓库根目录）：
    python tools/gen_circuits.py
输出：
    regression/circuits/*.qasm
    regression/ideal_distributions.json
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq import parse  # noqa: E402
from loomq.ir import WHITELIST  # noqa: E402
from loomq.refsim import ideal_distribution  # noqa: E402

OUT_DIR = os.path.join(ROOT, "regression")
CIRCUIT_DIR = os.path.join(OUT_DIR, "circuits")


def _header(n_qubits: int, n_clbits: int) -> List[str]:
    return [
        "OPENQASM 2.0;",
        'include "qelib1.inc";',
        "qreg q[%d];" % n_qubits,
        "creg c[%d];" % n_clbits,
    ]


def _measure_all(n_qubits: int) -> List[str]:
    return ["measure q[%d] -> c[%d];" % (index, index) for index in range(n_qubits)]


def ghz(n_qubits: int) -> str:
    """n 枚"永远同面的硬币"。理想分布为全 0 与全 1 各 50%。"""
    lines = _header(n_qubits, n_qubits)
    lines.append("h q[0];")
    for index in range(n_qubits - 1):
        lines.append("cx q[%d],q[%d];" % (index, index + 1))
    lines += _measure_all(n_qubits)
    return "\n".join(lines) + "\n"


def qft(n_qubits: int) -> str:
    """标准 QFT，含末尾比特反转。

    输入态用 ry(pi/2) 制备（不用 h：QFT 自身也对每个比特施加 h，用 h 制备会与之相互抵消，
    实测会把分布搅成 11 个杂峰）。制备在 q[0]、q[1] 上，输出恰好是 4 个 25% 的峰，
    与题面对 QFT-4 的描述一致。

    为什么必须这样造：对**基态**做 QFT 会得到 16 个均匀的 6.25%——所有振幅模长相同，
    相位信息完全不进入测量分布，那种电路作为回归集是无效的，任何相位错误都测不出来。
    """
    lines = _header(n_qubits, n_qubits)
    for index in range(min(2, n_qubits)):
        lines.append("ry(%.12g) q[%d];" % (math.pi / 2, index))
    for target in range(n_qubits):
        lines.append("h q[%d];" % target)
        for control in range(target + 1, n_qubits):
            angle = math.pi / (2 ** (control - target))
            lines.append("cu1(%.12g) q[%d],q[%d];" % (angle, control, target))
    for index in range(n_qubits // 2):
        lines.append("swap q[%d],q[%d];" % (index, n_qubits - 1 - index))
    lines += _measure_all(n_qubits)
    return "\n".join(lines) + "\n"


def grover3() -> str:
    """3 比特 Grover，目标态 |111>，迭代 2 次。

    2 次迭代的理论成功率是 sin²(5θ) 其中 sinθ=1/√8，即 94.53%——与题面描述一致。
    相位翻转用 h·ccx·h 构成 ccz，同时把 ccx 和 h 都压上。
    """
    lines = _header(3, 3)
    for index in range(3):
        lines.append("h q[%d];" % index)
    for _ in range(2):
        # 神谕：对 |111> 做相位翻转
        lines += ["h q[2];", "ccx q[0],q[1],q[2];", "h q[2];"]
        # 扩散：H·X·CCZ·X·H
        lines += ["h q[0];", "h q[1];", "h q[2];"]
        lines += ["x q[0];", "x q[1];", "x q[2];"]
        lines += ["h q[2];", "ccx q[0],q[1],q[2];", "h q[2];"]
        lines += ["x q[0];", "x q[1];", "x q[2];"]
        lines += ["h q[0];", "h q[1];", "h q[2];"]
    lines += _measure_all(3)
    return "\n".join(lines) + "\n"


def random_circuit(n_qubits: int, n_gates: int, seed: int) -> str:
    """随机指令流，用于检验 12 门覆盖完整性。固定种子保证可复现。"""
    rng = random.Random(seed)
    names = sorted(WHITELIST)
    lines = _header(n_qubits, n_qubits)
    for _ in range(n_gates):
        name = rng.choice(names)
        arity, n_params = WHITELIST[name]
        if arity > n_qubits:
            continue
        qubits = rng.sample(range(n_qubits), arity)
        params = ""
        if n_params:
            angle = rng.choice([math.pi, math.pi / 2, math.pi / 3, math.pi / 4,
                                -math.pi / 2, -math.pi / 3, 2 * math.pi / 3])
            params = "(%.12g)" % angle
        lines.append("%s%s %s;" % (name, params, ",".join("q[%d]" % q for q in qubits)))
    lines += _measure_all(n_qubits)
    return "\n".join(lines) + "\n"


# 随机电路的种子不是随手取的，是筛出来的：要求分布同时对相位错误和位序错误敏感。
# 随手取的种子很容易生成均匀分布（比如 32 个态各 3.125%），而均匀分布既测不出相位错误，
# 也测不出位序错误——整串反转后它和自己一模一样。下面三个种子的判别力见 _sensitivity_check。
SPECS: List[Tuple[str, str]] = [
    ("ghz5", ghz(5)),
    ("qft4", qft(4)),
    ("grover3", grover3()),
    ("random_a", random_circuit(3, 24, seed=20260807)),
    ("random_b", random_circuit(4, 32, seed=20260802)),
    ("random_c", random_circuit(5, 40, seed=20260801)),
]


def main() -> int:
    os.makedirs(CIRCUIT_DIR, exist_ok=True)
    distributions: Dict[str, Dict[str, float]] = {}

    print("=== 生成隐藏电路回归集 ===\n")
    for name, source in SPECS:
        path = os.path.join(CIRCUIT_DIR, name + ".qasm")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)

        circuit = parse(source)
        distribution = ideal_distribution(circuit)
        distributions[name] = distribution

        top = sorted(distribution.items(), key=lambda kv: -kv[1])[:4]
        summary = "  ".join("%s:%.4f" % (key, value) for key, value in top)
        print("%-10s %d 比特 %2d 门 %2d 个非零态   %s"
              % (name, circuit.n_qubits, len(circuit.gates()), len(distribution), summary))

    with open(os.path.join(OUT_DIR, "ideal_distributions.json"), "w", encoding="utf-8") as handle:
        json.dump(distributions, handle, ensure_ascii=False, indent=2, sort_keys=True)

    print("\n--- 对照题面《测试电路人话版》自检 ---")
    ghz5 = distributions["ghz5"]
    print("GHZ-5 应为全 0 / 全 1 各 50%%：%s" % {k: round(v, 4) for k, v in sorted(ghz5.items())})

    qft4 = distributions["qft4"]
    peaks = [(key, value) for key, value in sorted(qft4.items()) if value > 0.01]
    print("QFT-4 应为恰好 4 个 25%% 的峰：%d 个峰 %s"
          % (len(peaks), [(k, round(v, 4)) for k, v in peaks]))

    grover = distributions["grover3"]
    print("Grover-3 应以约 94.5%% 命中 111：111 的概率 %.4f" % grover.get("111", 0.0))

    print("\n--- 灵敏度检验：这些电路真的测得出相位错误和位序错误吗 ---")
    _sensitivity_check(distributions)

    print("\n写入 %s" % OUT_DIR)
    return 0


def _as_crz(circuit):
    """把电路里每个 cu1 换成"受控 rz"写法——一个可观测的错误，用来检验回归集的灵敏度。"""
    from loomq.ir import Circuit, Gate

    ops = []
    for op in circuit.ops:
        if isinstance(op, Gate) and op.name == "cu1":
            control, target = op.qubits
            theta = op.params[0]
            ops += [
                Gate("cx", (control, target)),
                Gate("rz", (target,), (-theta / 2,)),
                Gate("cx", (control, target)),
                Gate("rz", (target,), (theta / 2,)),
            ]
        else:
            ops.append(op)
    return Circuit(circuit.n_qubits, circuit.n_clbits, ops)


def _reversed_distribution(distribution: Dict[str, float]) -> Dict[str, float]:
    """整串位序反转——最常见的中间层错误。"""
    return {key[::-1]: value for key, value in distribution.items()}


THRESHOLD = 0.97  # 官方保真度阈值


def _sensitivity_check(distributions: Dict[str, Dict[str, float]]) -> None:
    """一个回归电路只有在错误实现下保真度跌破 0.97 时，才算这类错误的有效探测器。

    检两类错误，因为它们是两个独立的坑：
      相位：把 cu1 误写成受控 rz（crz）
      位序：把整个位串反过来输出

    注意 bell / ghz 这类对称电路对位序错误的保真度恰好是 1.0000——反转后分布完全不变。
    这正是"公开电路测不出位序错误"的量化证据。
    """
    from loomq.refsim import hellinger_fidelity

    print("%-10s %-8s %-16s %s" % ("电路", "态数", "误写成 crz", "位序整串反转"))
    phase_detectors = 0
    order_detectors = 0
    for name in ("ghz5", "qft4", "grover3", "random_a", "random_b", "random_c"):
        source = open(os.path.join(CIRCUIT_DIR, name + ".qasm"), encoding="utf-8").read()
        circuit = parse(source)
        ideal = distributions[name]

        n_cu1 = sum(1 for gate in circuit.gates() if gate.name == "cu1")
        if n_cu1:
            f_phase = hellinger_fidelity(ideal_distribution(_as_crz(circuit)), ideal)
            phase_cell = "%.4f %s" % (f_phase, "检出" if f_phase < THRESHOLD else "测不出")
            phase_detectors += f_phase < THRESHOLD
        else:
            phase_cell = "无 cu1 不适用"

        f_order = hellinger_fidelity(_reversed_distribution(ideal), ideal)
        order_cell = "%.4f %s" % (f_order, "检出" if f_order < THRESHOLD else "测不出（对称）")
        order_detectors += f_order < THRESHOLD

        print("%-10s %-8d %-16s %s" % (name, len(ideal), phase_cell, order_cell))

    print("\n相位错误探测器 %d 个，位序错误探测器 %d 个。" % (phase_detectors, order_detectors))
    if phase_detectors == 0 or order_detectors == 0:
        print("警告：有一类错误没有任何电路能探测到，回归集不合格，换种子。")


if __name__ == "__main__":
    sys.exit(main())
