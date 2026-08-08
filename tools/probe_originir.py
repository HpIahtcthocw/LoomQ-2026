#!/usr/bin/env python3
"""实测 pyqpanda 接受的 OriginIR 语法与门语义，不靠猜也不靠照抄文档。

要回答两个问题：

1. **含参门的参数写在哪。** 我们原先按 QASM 习惯写 `RY(1.5708) q[0]`，
   pyqpanda 3.8.5 直接报 "no viable alternative at input 'RY('"。
   这里穷举候选写法，看哪种能过解析。

2. **受控相位门的语义是 cu1 还是 crz。** 这是 PLAN.md 第一条陷阱：
       crz(θ) = diag(1, 1, e^{-iθ/2}, e^{iθ/2})   相位在受控分支上，可观测
       cu1(θ) = diag(1, 1, 1,        e^{iθ})      我们要的
   两者在 bell/ghz 上完全看不出差别，只有把它放进 QFT 风格的电路才暴露。
   所以这里不看门名叫什么，直接用一个能区分二者的电路，把实测分布同时与
   refsim 算出的 cu1 版与 crz 版比对，看它落在哪一边。

用法（需 3.10 环境且装了 pyqpanda）：
    python tools/probe_originir.py
"""

from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq.ir import Circuit, Gate, Measure  # noqa: E402
from loomq.refsim import hellinger_fidelity, ideal_distribution  # noqa: E402

try:
    import pyqpanda as pq
except ImportError:
    print("未装 pyqpanda。请在 3.10 环境执行：pip install pyqpanda")
    sys.exit(2)


def run_originir(source: str, shots: int = 16384):
    """跑一段 OriginIR，返回归一化概率分布；解析失败则抛异常。"""
    machine = pq.CPUQVM()
    machine.init_qvm()
    try:
        program, _, cbits = pq.convert_originir_str_to_qprog(source, machine)
        raw = machine.run_with_configuration(program, cbits, shots)
        total = sum(raw.values()) or 1
        return {str(key): value / total for key, value in raw.items()}
    finally:
        machine.finalize()


def try_syntax(label: str, source: str) -> bool:
    try:
        distribution = run_originir(source, 1024)
    except Exception as exc:  # noqa: BLE001
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print("  [失败] %-28s %s" % (label, first[:96]))
        return False
    top = max(distribution, key=lambda key: distribution[key])
    print("  [可用] %-28s 主峰 %s (%.1f%%)" % (label, top, distribution[top] * 100))
    return True


def probe_param_syntax() -> None:
    print("=" * 74)
    print("一、含参单比特门（RY）的参数位置")
    print("=" * 74)
    print("电路语义：RY(pi/2) 作用在 q[0]，测量应约各半。")
    head = "QINIT 1\nCREG 1\n"
    tail = "\nMEASURE q[0], c[0]\n"
    candidates = [
        ("RY q[0],(pi/2)", "RY q[0],(%.10f)" % (math.pi / 2)),
        ("RY q[0], (pi/2)", "RY q[0], (%.10f)" % (math.pi / 2)),
        ("RY(pi/2) q[0]  ← 原实现", "RY(%.10f) q[0]" % (math.pi / 2)),
        ("RY q[0] (pi/2)", "RY q[0] (%.10f)" % (math.pi / 2)),
    ]
    for label, body in candidates:
        try_syntax(label, head + body + tail)


def probe_controlled_phase_syntax() -> None:
    print()
    print("=" * 74)
    print("二、受控相位门的门名与语法")
    print("=" * 74)
    head = "QINIT 2\nCREG 2\nH q[0]\nH q[1]\n"
    tail = "\nMEASURE q[0], c[0]\nMEASURE q[1], c[1]\n"
    theta = math.pi / 2
    candidates = [
        ("CR q[0],q[1],(pi/2)", "CR q[0],q[1],(%.10f)" % theta),
        ("CU1 q[0],q[1],(pi/2)", "CU1 q[0],q[1],(%.10f)" % theta),
        ("CP q[0],q[1],(pi/2)", "CP q[0],q[1],(%.10f)" % theta),
        ("CU1(pi/2) q[0],q[1]  ← 原实现", "CU1(%.10f) q[0], q[1]" % theta),
    ]
    usable = []
    for label, body in candidates:
        if try_syntax(label, head + body + tail):
            usable.append((label.split()[0], body))
    return usable


def probe_controlled_phase_semantics(gate_name: str, body: str) -> None:
    """判定该门是 cu1 还是 crz。两者只在受控分支的相位上不同。

    用的电路：H 两个比特造均匀叠加，施加受控相位，再对 q[0] 做 H。
    最后那个 H 把相位差转成幅度差，否则相位在测量里完全隐形——
    这正是 PLAN.md 说"bell/ghz 测不出 cu1 语义错误"的原因。
    """
    print()
    print("=" * 74)
    print("三、%s 的语义：cu1 还是 crz？" % gate_name)
    print("=" * 74)

    theta = math.pi / 2
    source = (
        "QINIT 2\nCREG 2\nH q[0]\nH q[1]\n"
        + body
        + "\nH q[0]\nMEASURE q[0], c[0]\nMEASURE q[1], c[1]\n"
    )
    observed = run_originir(source)

    # 参考一：真正的 cu1
    cu1_circuit = Circuit(2, 2, [
        Gate("h", (0,)), Gate("h", (1,)), Gate("cu1", (0, 1), (theta,)),
        Gate("h", (0,)), Measure(0, 0), Measure(1, 1),
    ])
    # 参考二：错误的 crz 实现。crz(θ) = rz(θ/2) 挂在受控分支上，
    # 用 cu1 无法直接表达，所以按定义手工构造：cx, rz(-θ/2), cx, rz(θ/2)
    crz_circuit = Circuit(2, 2, [
        Gate("h", (0,)), Gate("h", (1,)),
        Gate("cx", (0, 1)), Gate("rz", (1,), (-theta / 2,)),
        Gate("cx", (0, 1)), Gate("rz", (1,), (theta / 2,)),
        Gate("h", (0,)), Measure(0, 0), Measure(1, 1),
    ])

    cu1_ideal = ideal_distribution(cu1_circuit)
    crz_ideal = ideal_distribution(crz_circuit)

    f_cu1 = hellinger_fidelity(observed, cu1_ideal)
    f_crz = hellinger_fidelity(observed, crz_ideal)

    print("  实测分布      %s" % _fmt(observed))
    print("  cu1 理想分布  %s" % _fmt(cu1_ideal))
    print("  crz 理想分布  %s" % _fmt(crz_ideal))
    print()
    print("  与 cu1 的保真度 %.4f" % f_cu1)
    print("  与 crz 的保真度 %.4f" % f_crz)
    print()
    if abs(f_cu1 - f_crz) < 0.02:
        print("  [无法判定] 这个电路区分不开两者，需要换一个更敏感的电路。")
    elif f_cu1 > f_crz:
        print("  [结论] %s 就是 cu1 语义 diag(1,1,1,e^{iθ})，可以直接用。" % gate_name)
    else:
        print("  [警告] %s 是 crz 语义！不能拿它顶替 cu1，" % gate_name)
        print("         必须改用 gate_identities.md 的 5 门分解（落到 u1）。")


def _fmt(distribution) -> str:
    return "  ".join(
        "%s:%.3f" % (key, distribution[key])
        for key in sorted(distribution)
        if distribution[key] > 1e-6
    )


def main() -> int:
    probe_param_syntax()
    usable = probe_controlled_phase_syntax()
    if not usable:
        print()
        print("没有任何受控相位门写法能通过解析。需要改用分解方案。")
        return 1
    name, body = usable[0]
    probe_controlled_phase_semantics(name, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
