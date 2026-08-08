#!/usr/bin/env python3
"""LoomQ-Q 量子 RISC-V 扩展的端到端测试。

对应赛题 Bonus「自定义量子 RISC-V 扩展指令（+8）」交付物 ③。
规格文档 docs/quantum-riscv-extension.md，扩展实现 tools/riscv_quantum_emulator.py。

七组测试，各自守住一件事：

  一 编解码往返：12 个门 × 各 funct3 类别，编码再解码必须回到原样。
  二 规格文档里的逐位实例：文档给的 4 个机器码必须和实现算出来的一致。
     （文档若与实现漂移，这一组会红——省掉"文档写着一套、代码跑着另一套"。）
  三 非法机器码必须被拒：错 opcode、未定义门号、该为 0 的字段有垃圾位。
  四 经典子集与官方模拟器差分：用 L3 的随机程序生成器造程序，
     逐条比对本 fork 与官方 riscv_emulator.py 的寄存器终态。
     这一组是"fork 没动坏原语义"的凭据。
  五 量子语义：门作用到正确的比特上（拿 refsim 当基准）。
  六 测量塌缩：纠缠关联、参数门定点角度、统计分布。
  七 端到端：Hybrid-QASM → compile_hybrid → 混合指令流 → 单机执行，
     测量驱动经典分支，终态与参考解释器一致。

用法（仓库根目录，纯标准库）：
    python tools/selftest_quantum_ext.py
"""

from __future__ import annotations

import math
import os
import random
import sys
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import adapter  # noqa: E402
from loomq import refsim  # noqa: E402
from loomq.hybrid import interpret, parse_classical, split_hybrid  # noqa: E402
from loomq.ir import Circuit, Gate  # noqa: E402
from loomq.qasm2_parser import parse  # noqa: E402
from riscv_emulator import TinyRISCVEmulator  # noqa: E402

import riscv_quantum_emulator as QE  # noqa: E402
from selftest_hybrid import ProgramGenerator  # noqa: E402

FAILURES: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         ("  -> " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def banner(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# --- 一、编解码往返 ---------------------------------------------------------


def test_roundtrip() -> None:
    banner("一、编解码往返：12 个门 × 各 funct3 类别")
    ok = True
    detail = ""
    checked = 0
    for funct7, (name, arity, parametrized) in sorted(QE.GATE_TABLE.items()):
        # 用不同的比特下标，才能发现"字段搞混"这类错
        qubits = [3, 17, 29][:arity]
        if parametrized:
            text = "q.gatep %s, %s, x7" % (name, ", ".join(str(q) for q in qubits))
        else:
            text = "q.gate %s, %s" % (name, ", ".join(str(q) for q in qubits))
        tokens = text.replace(",", " ").split()
        word = QE.assemble_quantum(tokens[0], tokens[1:])
        instruction = QE.decode(word)
        recovered = QE.disassemble(word)
        if instruction.gate_name != name:
            ok, detail = False, "%s 解码成了 %s" % (name, instruction.gate_name)
            break
        if recovered.replace(" ", "") != text.replace(" ", ""):
            ok, detail = False, "%r 反汇编成 %r" % (text, recovered)
            break
        checked += 1
    check("所有门的 q.gate/q.gatep 往返一致（%d 条）" % checked, ok, detail)

    # q.meas / q.init：比特下标与寄存器号不能串
    word = QE.assemble_quantum("q.meas", ["5", "x21"])
    instruction = QE.decode(word)
    check("q.meas 字段不串（比特 5 -> rs1，寄存器 x21 -> rd）",
          instruction.rs1 == 5 and instruction.rd == 21,
          "rs1=%d rd=%d" % (instruction.rs1, instruction.rd))
    word = QE.assemble_quantum("q.init", ["9"])
    check("q.init 往返", QE.disassemble(word) == "q.init 9", QE.disassemble(word))

    # opcode 必须落在 custom-0
    words = [QE.assemble_quantum("q.gate", ["h", "0"]),
             QE.assemble_quantum("q.meas", ["0", "x10"])]
    check("opcode 全部为 custom-0 (0x0B)",
          all((word & 0x7F) == 0x0B for word in words))


# --- 二、规格文档里的逐位实例 -----------------------------------------------


def test_doc_examples() -> None:
    banner("二、规格文档第 5 节的逐位实例必须与实现一致")
    # 文档 docs/quantum-riscv-extension.md 第 5 节列出的 4 个机器码
    expected = [
        ("q.gate h, 0", 0x0000000B),
        ("q.gate cx, 0, 1", 0x2010000B),
        ("q.meas 1, x10", 0x0000A50B),
        ("q.gatep rz, 0, x5", 0x8200128B),
    ]
    for text, want in expected:
        tokens = text.replace(",", " ").split()
        got = QE.assemble_quantum(tokens[0], tokens[1:])
        check("%-22s = 0x%08X" % (text, want), got == want, "实际 0x%08X" % got)

    # 文档说定点单位是 π/2^16
    check("定点角度单位为 π/2^16", QE.ANGLE_SCALE == (1 << 16))
    # 文档说 rz(π/2) -> 32768
    check("rz(π/2) 的定点值为 32768",
          round(math.pi / 2 * QE.ANGLE_SCALE / math.pi) == 32768)


# --- 三、非法机器码必须被拒 -------------------------------------------------


def test_decode_rejects() -> None:
    banner("三、非法机器码必须被拒，不能静默执行成别的指令")
    cases = [
        ("错误的 opcode", 0x0000000F),
        ("未定义的门号 funct7=0x7F", (0x7F << 25) | QE.OPCODE_CUSTOM0),
        ("未定义的 funct3=0b111", (0b111 << 12) | QE.OPCODE_CUSTOM0),
        # h 是单比特门，rs2 里有垃圾位
        ("单比特门的 rs2 有垃圾位", QE.encode(QE.FUNCT3_GATE, 0x00, rs1=1, rs2=2)),
        # cx 是双比特门，rd 里有垃圾位
        ("双比特门的 rd 有垃圾位", QE.encode(QE.FUNCT3_GATE, 0x10, rs1=0, rs2=1, rd=3)),
        # rz 是参数门，不能用 q.gate 的 funct3
        ("参数门用了非参数 funct3", QE.encode(QE.FUNCT3_GATE, 0x41, rs1=0)),
        # 非参数门不能用 q.gatep 的 funct3
        ("非参数门用了参数 funct3", QE.encode(QE.FUNCT3_GATEP, 0x00, rs1=0, rd=5)),
        ("q.init 的 rd 有垃圾位", QE.encode(QE.FUNCT3_INIT, 0, rs1=0, rd=1)),
    ]
    for name, word in cases:
        try:
            QE.decode(word)
            check(name, False, "本该拒绝 0x%08X 却接受了" % word)
        except QE.EncodingError:
            check(name, True)
        except Exception as exc:  # noqa: BLE001
            check(name, False, "抛的不是 EncodingError 而是 %s" % type(exc).__name__)

    # 汇编层也要挡住错误用法
    for name, mnemonic, operands in [
        ("q.gate 用在参数门上", "q.gate", ["rz", "0"]),
        ("q.gatep 用在非参数门上", "q.gatep", ["h", "0", "x5"]),
        ("cx 只给了一个比特", "q.gate", ["cx", "0"]),
        ("比特下标超出 5 位", "q.gate", ["h", "32"]),
        ("未知门名", "q.gate", ["foo", "0"]),
    ]:
        try:
            QE.assemble_quantum(mnemonic, operands)
            check(name, False, "本该报错却通过了")
        except QE.EncodingError:
            check(name, True)


# --- 四、经典子集与官方模拟器差分 -------------------------------------------


def test_classical_parity() -> None:
    banner("四、经典子集与官方 riscv_emulator.py 差分（fork 没动坏原语义）")
    rng = random.Random(20260808)
    mismatched = None
    programs = 0
    for _ in range(400):
        n_clbits = rng.randint(1, 4)
        source = ProgramGenerator(rng, n_clbits, rng.randint(0, 3)).program()
        _, assembly = adapter.compile_hybrid(source)
        for _ in range(3):
            injected = [rng.randint(0, 1) for _ in range(n_clbits)]

            official = TinyRISCVEmulator()
            official.load_program(assembly)
            for index, value in enumerate(injected):
                official.set_register("x%d" % (10 + index), value)
            expected = official.execute()

            fork = QE.TinyQuantumRISCVEmulator(n_qubits=0, seed=0)
            fork.load_program(assembly)
            for index, value in enumerate(injected):
                fork.set_register("x%d" % (10 + index), value)
            actual = fork.execute()

            if expected != actual:
                mismatched = "注入 %s\n官方 %s\n本 fork %s\n--- 汇编 ---\n%s" % (
                    injected, expected, actual, assembly
                )
                break
        if mismatched:
            break
        programs += 1
    check("%d 组随机程序上寄存器终态与官方模拟器逐一相同" % programs,
          mismatched is None, mismatched or "")

    # 官方文件自带的那段功能测试也要一字不差地过
    official_demo = """
    li x1, 5
    li x2, 10
    beq x1, x2, EQUAL
    add x3, x1, x2
    j END
    EQUAL:
    sub x3, x2, x1
    END:
    addi x3, x3, 1
    """
    fork = QE.TinyQuantumRISCVEmulator(seed=0)
    fork.load_program(official_demo)
    check("官方文件自带的 demo（期望 x3=16）",
          fork.execute().get("x3") == 16)

    # x0 硬连线为零
    fork = QE.TinyQuantumRISCVEmulator(seed=0)
    fork.load_program("li x0, 42\naddi x0, x0, 7")
    check("x0 恒为 0（写入被忽略）", fork.execute().get("x0", 0) == 0)


# --- 五、量子语义：门作用到正确的比特 ---------------------------------------


def test_gate_placement() -> None:
    banner("五、量子语义：门作用到正确的比特上（refsim 当基准）")
    # 这一组真正验证的是编解码把比特下标放对了位置，
    # 而不是验证门矩阵本身——门实现与 refsim 是同一份代码。
    cases = [
        ("Bell", 2, "q.gate h, 0\nq.gate cx, 0, 1",
         [Gate("h", (0,)), Gate("cx", (0, 1))]),
        ("GHZ-3", 3, "q.gate h, 0\nq.gate cx, 0, 1\nq.gate cx, 1, 2",
         [Gate("h", (0,)), Gate("cx", (0, 1)), Gate("cx", (1, 2))]),
        ("非对称：门作用在高位比特", 3, "q.gate h, 2\nq.gate x, 1",
         [Gate("h", (2,)), Gate("x", (1,))]),
        ("ccx 三个比特的顺序", 3, "q.gate x, 0\nq.gate x, 1\nq.gate ccx, 0, 1, 2",
         [Gate("x", (0,)), Gate("x", (1,)), Gate("ccx", (0, 1, 2))]),
        ("swap 不对称", 2, "q.gate x, 0\nq.gate swap, 0, 1",
         [Gate("x", (0,)), Gate("swap", (0, 1))]),
        ("cx 反向控制", 2, "q.gate x, 1\nq.gate cx, 1, 0",
         [Gate("x", (1,)), Gate("cx", (1, 0))]),
    ]
    for name, n_qubits, program, gates in cases:
        machine = QE.TinyQuantumRISCVEmulator(n_qubits=n_qubits, seed=0)
        machine.load_program(program)
        machine.execute()
        reference = refsim.statevector(Circuit(n_qubits=n_qubits, n_clbits=n_qubits, ops=list(gates)))
        deviation = max(abs(a - b) for a, b in zip(machine.statevector, reference))
        check("%s 末态与 refsim 一致" % name, deviation < 1e-12, "最大偏差 %.3g" % deviation)

    # 参数门：定点角度必须解出正确的 θ
    for angle_name, angle in [("π/2", math.pi / 2), ("π/8", math.pi / 8),
                              ("-π/4", -math.pi / 4)]:
        raw = int(round(angle * QE.ANGLE_SCALE / math.pi))
        machine = QE.TinyQuantumRISCVEmulator(n_qubits=1, seed=0)
        machine.load_program("li x5, %d\nq.gatep ry, 0, x5" % raw)
        machine.execute()
        reference = refsim.statevector(
            Circuit(n_qubits=1, n_clbits=1, ops=[Gate("ry", (0,), (angle,))])
        )
        deviation = max(abs(a - b) for a, b in zip(machine.statevector, reference))
        check("ry(%s) 定点角度正确" % angle_name, deviation < 1e-9,
              "最大偏差 %.3g" % deviation)


# --- 六、测量塌缩 -----------------------------------------------------------


def test_measurement() -> None:
    banner("六、测量塌缩：纠缠关联与统计分布")

    # Bell 态：两个测量位必须永远相等。这条是塌缩逻辑对不对的硬判据——
    # 塌缩写错的话，q[1] 会独立抽样，一半的次数就会不等。
    correlated = True
    seen = set()
    for seed in range(300):
        machine = QE.TinyQuantumRISCVEmulator(n_qubits=2, seed=seed)
        machine.load_program("q.gate h, 0\nq.gate cx, 0, 1\nq.meas 0, x10\nq.meas 1, x11")
        state = machine.execute()
        first, second = state.get("x10", 0), state.get("x11", 0)
        seen.add((first, second))
        if first != second:
            correlated = False
            break
    check("Bell 态 300 次测量两位恒相等", correlated)
    check("两种结果都出现过（不是恒定塌缩到同一个）",
          seen == {(0, 0), (1, 1)}, "实际出现 %s" % sorted(seen))

    # GHZ-3：三位必须全等
    all_equal = True
    for seed in range(200):
        machine = QE.TinyQuantumRISCVEmulator(n_qubits=3, seed=seed)
        machine.load_program(
            "q.gate h, 0\nq.gate cx, 0, 1\nq.gate cx, 1, 2\n"
            "q.meas 0, x10\nq.meas 1, x11\nq.meas 2, x12"
        )
        state = machine.execute()
        values = {state.get("x1%d" % k, 0) for k in range(3)}
        if len(values) != 1:
            all_equal = False
            break
    check("GHZ-3 的三个测量位恒全等", all_equal)

    # 统计分布：H 之后 0/1 各半，与 refsim 的理想分布比 Hellinger
    counts: Dict[str, int] = {}
    shots = 4000
    for seed in range(shots):
        machine = QE.TinyQuantumRISCVEmulator(n_qubits=1, seed=seed)
        machine.load_program("q.gate h, 0\nq.meas 0, x10")
        outcome = machine.execute().get("x10", 0)
        key = str(outcome)
        counts[key] = counts.get(key, 0) + 1
    observed = {key: value / shots for key, value in counts.items()}
    ideal = refsim.ideal_distribution(
        parse('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\ncreg c[1];\n'
              "h q[0];\nmeasure q[0] -> c[0];\n")
    )
    fidelity = refsim.hellinger_fidelity(observed, ideal)
    check("H 之后 %d 次抽样与理想分布保真度 %.4f > 0.99" % (shots, fidelity),
          fidelity > 0.99, "观测 %s" % observed)

    # q.init：无论之前是什么状态，复位后必须回到 |0>
    reset_ok = True
    for seed in range(50):
        machine = QE.TinyQuantumRISCVEmulator(n_qubits=2, seed=seed)
        machine.load_program(
            "q.gate h, 0\nq.gate cx, 0, 1\nq.init 0\nq.init 1\nq.meas 0, x10\nq.meas 1, x11"
        )
        state = machine.execute()
        if state.get("x10", 0) or state.get("x11", 0):
            reset_ok = False
            break
    check("q.init 把纠缠态的两个比特都复位到 |0>", reset_ok)


# --- 七、端到端：Hybrid-QASM 在单机上跑完混合逻辑 ---------------------------

END_TO_END_CASES = [
    (
        "题面示例：测量结果决定经典分支",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
measure q[0] -> c[0];
classical {
  if (c[0] == 1) { r1 = 100; } else { r1 = 10; }
  r1 = r1 + 5;
}
cx q[0], q[1];
''',
    ),
    (
        "Bell 态：两位一致性用经典代码校验",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0], q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
classical {
  r1 = c[0] - c[1];
  if (r1 == 0) { r2 = 1; } else { r2 = 0; }
}
''',
    ),
    (
        "三比特 GHZ + 嵌套分支",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0], q[1];
cx q[1], q[2];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
classical {
  r9 = c[0] + c[1] + c[2];
  if (r9 == 0) { r1 = 1; } else {
    if (r9 == 3) { r1 = 1; } else { r1 = 0; }
  }
}
''',
    ),
    (
        "带参数门（ry 走定点角度通道）",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
ry(pi/2) q[0];
measure q[0] -> c[0];
classical {
  r1 = 20;
  if (c[0] != 0) { r1 = r1 - 3; }
}
''',
    ),
]


def test_end_to_end() -> None:
    banner("七、端到端：Hybrid-QASM → 混合指令流 → 单机执行")
    for name, source in END_TO_END_CASES:
        quantum_ops, classical_asm = adapter.compile_hybrid(source)
        program = QE.assemble_hybrid_program(quantum_ops, classical_asm)
        circuit = parse(split_hybrid(source)[0])

        body: List = []
        for block in split_hybrid(source)[1]:
            body.extend(parse_classical(block))

        ok = True
        detail = ""
        invariant_holds = True
        for seed in range(40):
            machine = QE.TinyQuantumRISCVEmulator(n_qubits=circuit.n_qubits, seed=seed)
            machine.load_program(program)
            state = machine.execute()

            # 机器实际测到了什么，就拿这组值去问参考解释器该得什么
            outcomes: Dict[int, int] = {}
            for statement, (qubit, value) in zip(circuit.measures(), machine.measurements):
                if statement.qubit != qubit:
                    ok, detail = False, "测量顺序不符：期望 q[%d]，实际 q[%d]" % (
                        statement.qubit, qubit)
                    break
                outcomes[statement.clbit] = value
            if not ok:
                break

            expected = interpret(body, outcomes)
            for index in range(1, 10):
                if expected.get(index, 0) != state.get("x%d" % index, 0):
                    ok = False
                    detail = "seed=%d 测量=%s r%d：解释器 %d，机器 %d\n--- 指令流 ---\n%s" % (
                        seed, outcomes, index, expected.get(index, 0),
                        state.get("x%d" % index, 0), program)
                    break
            if not ok:
                break

            # Bell / GHZ 用例额外校验纠缠关联确实由机器体现出来
            if "Bell" in name and state.get("x2", 0) != 1:
                invariant_holds = False
            if "GHZ" in name and state.get("x1", 0) != 1:
                invariant_holds = False

        check(name, ok, detail)
        if ok and ("Bell" in name or "GHZ" in name):
            check("  └ 纠缠关联在经典侧恒成立", invariant_holds)


def main() -> int:
    print("=== LoomQ-Q 量子 RISC-V 扩展端到端测试（纯标准库）===")
    test_roundtrip()
    test_doc_examples()
    test_decode_rejects()
    test_classical_parity()
    test_gate_placement()
    test_measurement()
    test_end_to_end()
    print()
    print("全部通过" if not FAILURES else "失败 %d 项: %s" % (len(FAILURES), FAILURES[:5]))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
