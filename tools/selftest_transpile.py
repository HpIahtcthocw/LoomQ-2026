#!/usr/bin/env python3
"""转译层离线自测：纯标准库，不需要任何量子 SDK，可在任意机器上跑。

覆盖：公开电路解析、三目标发射、往返幂等、参数表达式、整寄存器广播、
多寄存器展平、12 门全覆盖、错误输入拒绝。

用法（在仓库根目录）：
    python tools/selftest_transpile.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq import emit_originir, emit_qasm2, emit_qasm3, parse  # noqa: E402
from loomq.ir import Gate, Measure  # noqa: E402
from loomq.qasm2_parser import QasmError  # noqa: E402

CIRCUITS = os.path.join(ROOT, "starter_kit", "circuits")

FAILURES = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print("[%s] %s%s" % (status, label, ("  -> " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def canonical(circuit) -> tuple:
    """把 IR 压成可比较的元组，用于往返一致性判定。"""
    ops = []
    for op in circuit.ops:
        if isinstance(op, Gate):
            ops.append(("g", op.name, op.qubits, tuple(round(p, 10) for p in op.params)))
        else:
            ops.append(("m", op.qubit, op.clbit))
    return (circuit.n_qubits, circuit.n_clbits, tuple(ops))


def test_public_circuits() -> None:
    for name, expected_qubits in (("bell.qasm", 2), ("ghz3.qasm", 3)):
        path = os.path.join(CIRCUITS, name)
        if not os.path.exists(path):
            check("public:%s exists" % name, False, "文件缺失 " + path)
            continue
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        circuit = parse(source)
        check("public:%s 比特数=%d" % (name, expected_qubits), circuit.n_qubits == expected_qubits,
              "实际 %d" % circuit.n_qubits)
        check("public:%s 有测量" % name, len(circuit.measures()) == expected_qubits,
              "测量数 %d" % len(circuit.measures()))
        for target, emitter in (("spinq", emit_qasm2), ("braket", emit_qasm3), ("originq", emit_originir)):
            text = emitter(circuit)
            check("public:%s -> %s 非空" % (name, target), bool(text.strip()))
        check("public:%s 往返幂等" % name, canonical(parse(emit_qasm2(circuit))) == canonical(circuit))


def test_all_twelve_gates() -> None:
    source = """
OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
x q[1];
s q[0];
sdg q[1];
t q[2];
tdg q[0];
rz(pi/2) q[1];
ry(-pi/4) q[2];
cx q[0],q[1];
cu1(2*pi/3) q[1],q[2];
swap q[0],q[2];
ccx q[0],q[1],q[2];
measure q -> c;
"""
    circuit = parse(source)
    names = [gate.name for gate in circuit.gates()]
    expected = ["h", "x", "s", "sdg", "t", "tdg", "rz", "ry", "cx", "cu1", "swap", "ccx"]
    check("12门:全部解析", names == expected, "得到 %s" % names)
    check("12门:往返幂等", canonical(parse(emit_qasm2(circuit))) == canonical(circuit))

    qasm3 = emit_qasm3(circuit)
    check("12门:qasm3 用 cnot", "cnot q[0], q[1];" in qasm3, qasm3)
    check("12门:qasm3 用 cp", "cp(" in qasm3, qasm3)
    check("12门:qasm3 声明", "qubit[3] q;" in qasm3 and "bit[3] c;" in qasm3)

    originir = emit_originir(circuit)
    check("12门:originir QINIT", originir.startswith("QINIT 3"), originir.splitlines()[0])
    check("12门:originir TOFFOLI", "TOFFOLI q[0], q[1], q[2]" in originir)
    check("12门:originir SDAG/TDAG", "SDAG q[1]" in originir and "TDAG q[0]" in originir)
    check("12门:originir MEASURE", "MEASURE q[0], c[0]" in originir)


def test_param_expressions() -> None:
    import math

    circuit = parse(
        'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\n'
        "rz(pi) q[0];\nry(pi/4) q[0];\nrz(-2*pi/3) q[0];\nry(0.75) q[0];\n"
    )
    values = [gate.params[0] for gate in circuit.gates()]
    expected = [math.pi, math.pi / 4, -2 * math.pi / 3, 0.75]
    ok = all(abs(a - b) < 1e-12 for a, b in zip(values, expected)) and len(values) == 4
    check("参数:表达式求值", ok, "得到 %s" % values)
    check("参数:不接受注入", _raises(lambda: parse(
        'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\nrz(__import__("os")) q[0];\n')))


def test_broadcast_and_multi_registers() -> None:
    circuit = parse(
        'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg a[2];\nqreg b[2];\ncreg ca[2];\ncreg cb[2];\n'
        "h a;\ncx a[0],b[0];\nmeasure a -> ca;\nmeasure b[1] -> cb[1];\n"
    )
    check("广播:h a 展开为 2 个门", sum(1 for g in circuit.gates() if g.name == "h") == 2)
    check("多寄存器:展平为 4 比特", circuit.n_qubits == 4, "实际 %d" % circuit.n_qubits)
    check("多寄存器:b[0] 偏移为 2",
          any(g.name == "cx" and g.qubits == (0, 2) for g in circuit.gates()),
          str([g.qubits for g in circuit.gates()]))
    check("多寄存器:整寄存器 measure 按位对齐",
          [(m.qubit, m.clbit) for m in circuit.measures()] == [(0, 0), (1, 1), (3, 3)],
          str([(m.qubit, m.clbit) for m in circuit.measures()]))
    check("拒绝:measure 两侧宽度不等", _raises(lambda: parse(
        'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg a[2];\ncreg c[4];\nmeasure a -> c;\n')))


def test_rejections() -> None:
    base = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\n'
    check("拒绝:白名单外的门", _raises(lambda: parse(base + "y q[0];\n")))
    check("拒绝:比特越界", _raises(lambda: parse(base + "h q[5];\n")))
    check("拒绝:未声明寄存器", _raises(lambda: parse(base + "h w[0];\n")))
    check("拒绝:操作数个数不对", _raises(lambda: parse(base + "cx q[0];\n")))
    check("拒绝:同门重复比特", _raises(lambda: parse(base + "cx q[0],q[0];\n")))
    check("拒绝:空输入", _raises(lambda: parse("   ")))
    check("拒绝:没有 qreg", _raises(lambda: parse('OPENQASM 2.0;\ninclude "qelib1.inc";\nh q[0];\n')))


def test_comments_and_formatting() -> None:
    circuit = parse(
        'OPENQASM 2.0;  // 版本\ninclude "qelib1.inc";\n/* 块注释\n跨行 */\n'
        "qreg q[2];\ncreg c[2];\nbarrier q;\nh q[0];  // 叠加\ncx q[0],q[1];\n"
        "measure q[0] -> c[0];\nmeasure q[1] -> c[1];\n"
    )
    check("注释:被正确剥离", len(circuit.gates()) == 2, "门数 %d" % len(circuit.gates()))
    check("barrier:被忽略", all(g.name in ("h", "cx") for g in circuit.gates()))


def _raises(callable_obj) -> bool:
    try:
        callable_obj()
    except (QasmError, ValueError):
        return True
    except Exception:
        return False
    return False


def main() -> int:
    print("=== LoomQ 转译层离线自测（无需量子 SDK）===\n")
    test_public_circuits()
    test_all_twelve_gates()
    test_param_expressions()
    test_broadcast_and_multi_registers()
    test_rejections()
    test_comments_and_formatting()
    print("\n" + ("全部通过" if not FAILURES else "失败 %d 项: %s" % (len(FAILURES), FAILURES)))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
