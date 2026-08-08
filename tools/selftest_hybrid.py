#!/usr/bin/env python3
"""L3 差分测试：随机生成 Hybrid-QASM，穷举注入所有测量值组合，比对终态。

对齐官方判定方式。题面原话：

> 评测系统按第三节文法**随机生成 N 组 Hybrid-QASM 用例**（不同分支结构、
> 不同常量、不同测量位数）。对每组用例：将 compile_hybrid 输出的 RISC-V 汇编
> 载入官方 riscv_emulator.py，**穷举注入所有测量值组合**，逐一比对寄存器终态
> 与参考解释器的结果；同时校验量子操作序列与原电路量子部分语义等价。

所以这里做同样的三件事：随机生成、穷举注入、比对参考解释器。
用的是**官方那个** riscv_emulator.py，不是自己写的模拟器。

## 关于"同错"风险

参考解释器 `interpret` 和代码生成 `Emitter` 都出自同一双手，两者一起错、
错得还一样，差分测试就会给出虚假的绿灯。两条防线：

1. 两条路径结构上差得很远——一条是直接在 AST 上树求值，另一条要经过
   寄存器分配、分支取反、标签跳转。同一个逻辑错误很难在两边表现一致。
2. 更要紧的是下面 FIXED_CASES 里的期望值**全部手算**，不来自任何一条路径。
   它们是独立锚点：即便解释器和代码生成一起错了，这些用例也会红。

用法（仓库根目录，纯标准库）：
    python tools/selftest_hybrid.py
    python tools/selftest_hybrid.py --programs 400 --seed 7
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import sys
from typing import Dict, List, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "starter_kit")
sys.path.insert(0, KIT)

import adapter  # noqa: E402
from loomq.hybrid import interpret, parse_classical, split_hybrid  # noqa: E402
from loomq.qasm2_parser import parse  # noqa: E402
from riscv_emulator import TinyRISCVEmulator  # noqa: E402

FAILURES: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         ("  -> " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# --- 手算期望的固定用例 -----------------------------------------------------
# 每条的 expected 都是人工推导出来的，独立于解释器与代码生成两条路径。
# 格式：(名称, Hybrid-QASM, {测量值元组: {变量下标: 期望值}})

FIXED_CASES: List[Tuple[str, str, Dict[Tuple[int, ...], Dict[int, int]]]] = [
    (
        "题面原版示例（带 // 注释）",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
measure q[0] -> c[0];
classical {                 // 经典控制块：测量结果 c[0] 由评测系统注入 x10 寄存器
  if (c[0] == 1) {
    r1 = 100;               // r1..r9 映射到 RISC-V x1..x9 通用寄存器
  } else {
    r1 = 10;
  }
  r1 = r1 + 5;
}
cx q[0], q[1];
''',
        # c[0]=1 -> 100+5=105；c[0]=0 -> 10+5=15
        {(1, 0): {1: 105}, (0, 0): {1: 15}},
    ),
    (
        "官方公开自测用例",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
measure q[0] -> c[0];
classical { if (c[0] == 1) { r1 = 7; } else { r1 = 3; } }
''',
        {(1,): {1: 7}, (0,): {1: 3}},
    ),
    (
        "嵌套 if + 两个测量位",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
measure q[0] -> c[0];
measure q[1] -> c[1];
classical {
  if (c[0] == 1) {
    if (c[1] == 1) { r1 = 3; } else { r1 = 2; }
  } else {
    if (c[1] == 1) { r1 = 1; } else { r1 = 0; }
  }
  r2 = r1 + r1;
}
''',
        # r1 = 2*c0 + c1；r2 = 2*r1
        {
            (0, 0): {1: 0, 2: 0},
            (0, 1): {1: 1, 2: 2},
            (1, 0): {1: 2, 2: 4},
            (1, 1): {1: 3, 2: 6},
        },
    ),
    (
        "无 else 的 if",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
measure q[0] -> c[0];
classical {
  r1 = 40;
  if (c[0] != 0) { r1 = r1 + 2; }
}
''',
        {(0,): {1: 40}, (1,): {1: 42}},
    ),
    (
        "括号、一元负号、寄存器间减法",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
measure q[0] -> c[0];
classical {
  r1 = 10;
  r2 = 4;
  r3 = (r1 - r2) - (2 - 5);
  r4 = -r2 + 1;
}
''',
        # r3 = (10-4) - (-3) = 9；r4 = -4+1 = -3
        {(0,): {1: 10, 2: 4, 3: 9, 4: -3}, (1,): {1: 10, 2: 4, 3: 9, 4: -3}},
    ),
    (
        "赋值右侧引用自身与他人（顺序敏感）",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[1];
measure q[0] -> c[0];
classical {
  r1 = 7;
  r2 = 3;
  r1 = r2 - r1;
  r2 = r1 + r2;
}
''',
        # r1 = 3-7 = -4；r2 = -4+3 = -1
        {(0,): {1: -4, 2: -1}, (1,): {1: -4, 2: -1}},
    ),
    (
        "多个 classical 块，状态跨块延续",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
measure q[0] -> c[0];
classical { r1 = 5; }
h q[1];
measure q[1] -> c[1];
classical { r1 = r1 + c[1]; r2 = r1 - c[0]; }
''',
        # r1 = 5 + c1；r2 = r1 - c0
        {
            (0, 0): {1: 5, 2: 5},
            (0, 1): {1: 6, 2: 6},
            (1, 0): {1: 5, 2: 4},
            (1, 1): {1: 6, 2: 5},
        },
    ),
    (
        "三个测量位、深层嵌套",
        '''OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
classical {
  r9 = c[0] + c[1] + c[2];
  if (r9 == 0) { r1 = 100; } else {
    if (r9 == 1) { r1 = 200; } else {
      if (r9 == 2) { r1 = 300; } else { r1 = 400; }
    }
  }
}
''',
        {
            (0, 0, 0): {9: 0, 1: 100},
            (1, 0, 0): {9: 1, 1: 200},
            (0, 1, 0): {9: 1, 1: 200},
            (1, 1, 0): {9: 2, 1: 300},
            (1, 1, 1): {9: 3, 1: 400},
        },
    ),
]


# --- 随机程序生成 -----------------------------------------------------------


class ProgramGenerator:
    """按题面文法随机生成 Hybrid-QASM。

    刻意覆盖题面点名的三个维度：不同分支结构、不同常量、不同测量位数。
    """

    def __init__(self, rng: random.Random, n_clbits: int, max_depth: int) -> None:
        self.rng = rng
        self.n_clbits = n_clbits
        self.max_depth = max_depth

    def expression(self, depth: int = 0) -> str:
        choices = ["int", "var", "clbit"]
        if depth < 2:
            choices += ["binary", "binary", "paren", "neg"]
        kind = self.rng.choice(choices)
        if kind == "int":
            return str(self.rng.randint(-50, 200))
        if kind == "var":
            return "r%d" % self.rng.randint(1, 9)
        if kind == "clbit":
            return "c[%d]" % self.rng.randrange(self.n_clbits)
        if kind == "neg":
            return "-" + self.expression(depth + 1)
        if kind == "paren":
            return "(" + self.expression(depth + 1) + ")"
        return "%s %s %s" % (
            self.expression(depth + 1),
            self.rng.choice(["+", "-"]),
            self.expression(depth + 1),
        )

    def statement(self, depth: int) -> List[str]:
        if depth < self.max_depth and self.rng.random() < 0.45:
            return self.branch(depth)
        return ["r%d = %s;" % (self.rng.randint(1, 9), self.expression())]

    def branch(self, depth: int) -> List[str]:
        condition = "%s %s %s" % (
            self.expression(),
            self.rng.choice(["==", "!="]),
            self.expression(),
        )
        lines = ["if (%s) {" % condition]
        lines += ["  " + line for line in self.body(depth + 1)]
        if self.rng.random() < 0.75:
            lines.append("} else {")
            lines += ["  " + line for line in self.body(depth + 1)]
        lines.append("}")
        return lines

    def body(self, depth: int) -> List[str]:
        lines: List[str] = []
        for _ in range(self.rng.randint(1, 3)):
            lines += self.statement(depth)
        return lines

    def program(self) -> str:
        n_qubits = max(self.n_clbits, 1)
        lines = [
            "OPENQASM 2.0;",
            'include "qelib1.inc";',
            "qreg q[%d];" % n_qubits,
            "creg c[%d];" % self.n_clbits,
        ]
        for qubit in range(n_qubits):
            if self.rng.random() < 0.6:
                lines.append("%s q[%d];" % (self.rng.choice(["h", "x", "s", "t"]), qubit))
        for index in range(self.n_clbits):
            lines.append("measure q[%d] -> c[%d];" % (index, index))
        lines.append("classical {")
        lines += ["  " + line for line in self.body(0)]
        lines.append("}")
        if n_qubits >= 2 and self.rng.random() < 0.5:
            lines.append("cx q[0], q[1];")
        return "\n".join(lines) + "\n"


# --- 差分执行 ---------------------------------------------------------------


def registers_from_emulator(assembly: str, measurements: Sequence[int]) -> Dict[int, int]:
    emulator = TinyRISCVEmulator()
    # 顺序要紧：load_program 会把寄存器清零，注入必须在它之后
    emulator.load_program(assembly)
    for index, value in enumerate(measurements):
        emulator.set_register("x%d" % (10 + index), value)
    state = emulator.execute()
    return {index: state.get("x%d" % index, 0) for index in range(1, 10)}


def differential(name: str, source: str) -> bool:
    """穷举所有测量值组合，比对解释器与真实模拟器的寄存器终态。"""
    quantum_source, blocks = split_hybrid(source)
    circuit = parse(quantum_source)
    n_clbits = circuit.n_clbits

    body: List = []
    for block in blocks:
        body.extend(parse_classical(block))

    quantum_ops, assembly = adapter.compile_hybrid(source)

    # 量子部分：剥离出的序列必须与原电路的量子语句一一对应
    expected_ops = len(circuit.ops)
    if len(quantum_ops) != expected_ops:
        check("%s：量子操作数量" % name, False,
              "期望 %d 条，实际 %d 条" % (expected_ops, len(quantum_ops)))
        return False
    # 再解析回去，确认这些字符串是合法且语义一致的 QASM
    rebuilt = "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[%d];\ncreg c[%d];\n%s\n" % (
        circuit.n_qubits, max(circuit.n_clbits, 1), "\n".join(quantum_ops)
    )
    try:
        rebuilt_circuit = parse(rebuilt)
    except Exception as exc:  # noqa: BLE001
        check("%s：量子序列可重新解析" % name, False, "%s: %s" % (type(exc).__name__, exc))
        return False
    if rebuilt_circuit.ops != circuit.ops:
        check("%s：量子序列语义等价" % name, False, "重新解析后与原电路不一致")
        return False

    if n_clbits > 12:
        raise ValueError("测量位太多，穷举不划算：%d" % n_clbits)

    for combination in itertools.product((0, 1), repeat=n_clbits):
        expected = interpret(body, {index: value for index, value in enumerate(combination)})
        actual = registers_from_emulator(assembly, combination)
        for index in range(1, 10):
            if expected.get(index, 0) != actual[index]:
                check(
                    "%s：测量 %s 下 r%d" % (name, combination, index),
                    False,
                    "解释器 %d，模拟器 %d\n--- 源 ---\n%s\n--- 汇编 ---\n%s"
                    % (expected.get(index, 0), actual[index], source, assembly),
                )
                return False
    return True


# --- 测试组 -----------------------------------------------------------------


def test_fixed_cases() -> None:
    print("=" * 74)
    print("一、固定用例：期望值全部手算，独立于解释器与代码生成")
    print("=" * 74)
    for name, source, expectations in FIXED_CASES:
        _, assembly = adapter.compile_hybrid(source)
        ok = True
        detail = ""
        for measurements, expected in expectations.items():
            actual = registers_from_emulator(assembly, measurements)
            for index, want in expected.items():
                if actual[index] != want:
                    ok = False
                    detail = "测量 %s 下 r%d：期望 %d，实际 %d\n--- 汇编 ---\n%s" % (
                        measurements, index, want, actual[index], assembly
                    )
                    break
            if not ok:
                break
        check(name, ok, detail)


def test_fixed_cases_differential() -> None:
    print()
    print("=" * 74)
    print("二、固定用例走一遍穷举差分（同时校验量子序列）")
    print("=" * 74)
    for name, source, _ in FIXED_CASES:
        if differential(name, source):
            check(name, True)


def test_random_programs(count: int, seed: int) -> None:
    print()
    print("=" * 74)
    print("三、随机程序 %d 组：穷举注入所有测量值组合" % count)
    print("=" * 74)
    rng = random.Random(seed)
    combinations = 0
    failed = 0
    for serial in range(count):
        n_clbits = rng.randint(1, 4)
        max_depth = rng.randint(0, 3)
        generator = ProgramGenerator(rng, n_clbits, max_depth)
        source = generator.program()
        name = "随机 #%d（%d 测量位，嵌套深度上限 %d）" % (serial + 1, n_clbits, max_depth)
        try:
            if not differential(name, source):
                failed += 1
                if failed >= 3:
                    print("   已连续报错，停止以免刷屏")
                    break
                continue
        except Exception as exc:  # noqa: BLE001
            check(name, False, "%s: %s\n--- 源 ---\n%s" % (type(exc).__name__, exc, source))
            failed += 1
            if failed >= 3:
                break
            continue
        combinations += 2**n_clbits
    check("随机程序全部通过（共 %d 组，%d 次注入）" % (count, combinations), failed == 0)


def test_error_handling() -> None:
    print()
    print("=" * 74)
    print("四、非法输入要给出可读错误，不能崩成 AttributeError")
    print("=" * 74)
    from loomq.hybrid import HybridError

    bad = [
        ("变量越界 r10", "qreg q[1];\ncreg c[1];\nclassical { r10 = 1; }"),
        ("花括号不配对", "qreg q[1];\ncreg c[1];\nclassical { if (c[0] == 1) { r1 = 1; }"),
        ("条件缺比较符", "qreg q[1];\ncreg c[1];\nclassical { if (c[0]) { r1 = 1; } }"),
        ("表达式不完整", "qreg q[1];\ncreg c[1];\nclassical { r1 = 1 + ; }"),
    ]
    for name, source in bad:
        try:
            adapter.compile_hybrid(source)
            check(name, False, "本该报错却通过了")
        except HybridError:
            check(name, True)
        except Exception as exc:  # noqa: BLE001
            check(name, False, "抛的不是 HybridError 而是 %s: %s" % (type(exc).__name__, exc))


def test_instruction_whitelist() -> None:
    print()
    print("=" * 74)
    print("五、生成的汇编只能用题面允许的七条指令")
    print("=" * 74)
    allowed = {"li", "add", "sub", "addi", "beq", "bne", "j"}
    rng = random.Random(20260808)
    used = set()
    longest = 0
    for _ in range(120):
        generator = ProgramGenerator(rng, rng.randint(1, 4), rng.randint(0, 3))
        _, assembly = adapter.compile_hybrid(generator.program())
        count = 0
        for line in assembly.splitlines():
            line = line.split("#")[0].strip()
            if not line or line.endswith(":"):
                continue
            used.add(line.split()[0].lower())
            count += 1
        longest = max(longest, count)
    check("只用了允许的指令", used <= allowed, "越界：%s" % (used - allowed))
    print("      （实际用到：%s）" % ", ".join(sorted(used)))
    # 模拟器 max_steps=1000，指令条数是执行步数的上界
    check("最长程序 %d 条指令，未逼近模拟器 1000 步上限" % longest, longest < 500)


def test_pressure_boundaries() -> None:
    """两个边界，都是加压时真栽过的，固化下来防回归。

    一是测量位多到挤占临时寄存器区：c[k] 映射到 x10+k，位数一多就会向上
    吃掉临时寄存器。按 AST 递归生成代码时 18 位还行、20 位就报寄存器不够。
    二是深嵌套把指令数顶到模拟器 max_steps=1000 以上（实测到过 1311 条）。
    两者的根因相同——临时寄存器消耗随嵌套深度增长——归一化成仿射式后一起消失。
    """
    print()
    print("=" * 74)
    print("六、边界：测量位挤占临时寄存器 / 深嵌套顶到步数上限")
    print("=" * 74)

    for n_clbits in (8, 12, 16, 18, 20):
        rng = random.Random(n_clbits)
        source = ProgramGenerator(rng, n_clbits, 2).program()
        try:
            _, assembly = adapter.compile_hybrid(source)
        except Exception as exc:  # noqa: BLE001
            check("%d 个测量位可编译" % n_clbits, False, "%s: %s" % (type(exc).__name__, exc))
            continue
        quantum_source, blocks = split_hybrid(source)
        body: List = []
        for block in blocks:
            body.extend(parse_classical(block))
        # 位数多时穷举不现实，抽查若干组合
        ok = True
        for _ in range(8):
            combination = tuple(rng.randint(0, 1) for _ in range(n_clbits))
            expected = interpret(body, dict(enumerate(combination)))
            actual = registers_from_emulator(assembly, combination)
            if any(expected.get(i, 0) != actual[i] for i in range(1, 10)):
                ok = False
                break
        check("%d 个测量位结果正确" % n_clbits, ok)

    rng = random.Random(5)
    longest = 0
    tripped = None
    for _ in range(300):
        source = ProgramGenerator(rng, 3, 5).program()
        try:
            _, assembly = adapter.compile_hybrid(source)
        except Exception as exc:  # noqa: BLE001
            tripped = "编译失败 %s: %s" % (type(exc).__name__, exc)
            break
        count = len([line for line in assembly.splitlines()
                     if line.split("#")[0].strip()
                     and not line.split("#")[0].strip().endswith(":")])
        longest = max(longest, count)
        for combination in itertools.product((0, 1), repeat=3):
            try:
                registers_from_emulator(assembly, combination)
            except RuntimeError as exc:  # 模拟器超出 max_steps 会抛这个
                tripped = "%s\n--- 源 ---\n%s" % (exc, source)
                break
        if tripped:
            break
    check("深嵌套 300 组均未触发模拟器 max_steps（最长 %d 条指令）" % longest,
          tripped is None, tripped or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="L3 混合编译差分测试")
    parser.add_argument("--programs", type=int, default=300, help="随机程序数量")
    parser.add_argument("--seed", type=int, default=20260808, help="随机种子")
    args = parser.parse_args()

    print("=== L3 Hybrid-QASM 编译差分测试（纯标准库，用官方 riscv_emulator）===\n")
    test_fixed_cases()
    test_fixed_cases_differential()
    test_random_programs(args.programs, args.seed)
    test_error_handling()
    test_instruction_whitelist()
    test_pressure_boundaries()
    print("\n" + ("全部通过" if not FAILURES else "失败 %d 项: %s" % (len(FAILURES), FAILURES[:5])))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
