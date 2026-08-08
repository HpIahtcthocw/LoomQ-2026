#!/usr/bin/env python3
"""LoomQ-Q：官方 riscv_emulator.py 的 fork，增加量子自定义扩展指令。

对应赛题 Bonus「自定义量子 RISC-V 扩展指令（+8）」交付物 ②。
指令编码规格见 docs/quantum-riscv-extension.md，端到端测试见
tools/selftest_quantum_ext.py。

## 相对官方 starter_kit/riscv_emulator.py 改了什么

原文件 170 行，7 条经典指令。本 fork 保留其**全部**语义（`x0` 恒零、
`load_program` 清零寄存器、`execute` 只返回非零寄存器、`max_steps` 防死循环、
标签与注释的处理方式），改动集中在三处：

  A. 新增 32 位指令编码/解码（`encode` / `decode`）。原模拟器直接执行
     文本 token，本 fork 把每条指令先编码成 32 位机器码再解码执行，
     因为 Bonus 要的是 "custom opcode 编码" 而不只是新助记符。
     经典指令走一条透明通道（原样保留 token），量子指令走真编解码。
  B. 机器状态增加 statevector，新增 4 条自定义指令的执行逻辑。
  C. 新增 `assemble_hybrid_program()`，把 L3 的两份产物缝成一条指令流。

`tools/selftest_quantum_ext.py` 里有一组差分测试，用随机程序逐条比对本 fork
与官方模拟器在**经典子集**上的寄存器终态，确保 A、B、C 没有动坏原语义。
"""

from __future__ import annotations

import os
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq.ir import Gate  # noqa: E402
from loomq.refsim import _apply_gate  # noqa: E402

# --- 编码常量（与 docs/quantum-riscv-extension.md 一一对应）-----------------

OPCODE_CUSTOM0 = 0x0B  # RISC-V 保留的 custom-0

FUNCT3_GATE = 0b000  # q.gate   非参数门
FUNCT3_GATEP = 0b001  # q.gatep  参数门，角度取自寄存器
FUNCT3_MEAS = 0b010  # q.meas   测量并塌缩
FUNCT3_INIT = 0b011  # q.init   复位到 |0>

# funct7 -> (门名, 元数, 是否带参数)。高位按元数分段，解码时据此校验操作数个数。
GATE_TABLE: Dict[int, Tuple[str, int, bool]] = {
    0x00: ("h", 1, False),
    0x01: ("x", 1, False),
    0x02: ("s", 1, False),
    0x03: ("sdg", 1, False),
    0x04: ("t", 1, False),
    0x05: ("tdg", 1, False),
    0x10: ("cx", 2, False),
    0x11: ("swap", 2, False),
    0x20: ("ccx", 3, False),
    0x40: ("ry", 1, True),
    0x41: ("rz", 1, True),
    0x50: ("cu1", 2, True),
}
GATE_FUNCT7 = {name: funct7 for funct7, (name, _, _) in GATE_TABLE.items()}

# 定点角度：θ = value × π / ANGLE_SCALE
ANGLE_SCALE = 1 << 16

MAX_QUBIT_INDEX = 31  # 5 位字段

CLASSICAL_OPS = frozenset({"li", "add", "sub", "addi", "beq", "bne", "j"})


class EncodingError(ValueError):
    """指令编码或解码失败。"""


# --- A. 32 位编码 / 解码 ----------------------------------------------------


@dataclass(frozen=True)
class QuantumInstruction:
    """解码后的量子指令。"""

    funct3: int
    funct7: int
    rs1: int
    rs2: int
    rd: int

    @property
    def gate_name(self) -> str:
        return GATE_TABLE[self.funct7][0]


def _check_field(name: str, value: int, width: int) -> int:
    if not 0 <= value < (1 << width):
        raise EncodingError("字段 %s 超出 %d 位范围: %d" % (name, width, value))
    return value


def encode(funct3: int, funct7: int = 0, rs1: int = 0, rs2: int = 0, rd: int = 0) -> int:
    """按 R-type 字段划分打包成 32 位机器码。"""
    _check_field("funct3", funct3, 3)
    _check_field("funct7", funct7, 7)
    _check_field("rs1", rs1, 5)
    _check_field("rs2", rs2, 5)
    _check_field("rd", rd, 5)
    return (
        (funct7 << 25)
        | (rs2 << 20)
        | (rs1 << 15)
        | (funct3 << 12)
        | (rd << 7)
        | OPCODE_CUSTOM0
    )


def decode(word: int) -> QuantumInstruction:
    """从 32 位机器码解出量子指令，并校验未使用字段确实为 0。"""
    if not 0 <= word < (1 << 32):
        raise EncodingError("机器码不是 32 位无符号数: %r" % (word,))
    opcode = word & 0x7F
    if opcode != OPCODE_CUSTOM0:
        raise EncodingError("opcode 不是 custom-0 (0x0B): 0x%02X" % opcode)

    instruction = QuantumInstruction(
        funct3=(word >> 12) & 0x7,
        funct7=(word >> 25) & 0x7F,
        rs1=(word >> 15) & 0x1F,
        rs2=(word >> 20) & 0x1F,
        rd=(word >> 7) & 0x1F,
    )

    if instruction.funct3 in (FUNCT3_GATE, FUNCT3_GATEP):
        if instruction.funct7 not in GATE_TABLE:
            raise EncodingError("未定义的门编号 funct7=0x%02X" % instruction.funct7)
        _, arity, parametrized = GATE_TABLE[instruction.funct7]
        if parametrized != (instruction.funct3 == FUNCT3_GATEP):
            raise EncodingError(
                "门 %s 与 funct3=0b%s 的参数属性不符"
                % (instruction.gate_name, format(instruction.funct3, "03b"))
            )
        # 未用到的字段必须为 0：宁可报错，也不要把垃圾位静默执行成别的指令
        if arity < 2 and instruction.rs2:
            raise EncodingError("%s 是 %d 元门，rs2 字段应为 0" % (instruction.gate_name, arity))
        if instruction.funct3 == FUNCT3_GATE and arity < 3 and instruction.rd:
            raise EncodingError("%s 是 %d 元门，rd 字段应为 0" % (instruction.gate_name, arity))
    elif instruction.funct3 == FUNCT3_MEAS:
        if instruction.funct7 or instruction.rs2:
            raise EncodingError("q.meas 的 funct7 与 rs2 字段应为 0")
    elif instruction.funct3 == FUNCT3_INIT:
        if instruction.funct7 or instruction.rs2 or instruction.rd:
            raise EncodingError("q.init 的 funct7、rs2、rd 字段应为 0")
    else:
        raise EncodingError("未定义的 funct3=0b%s" % format(instruction.funct3, "03b"))

    return instruction


# --- 汇编：文本助记符 -> 32 位机器码 ----------------------------------------

_REGISTER_RE = re.compile(r"^x(\d+)$", re.IGNORECASE)


def _parse_register(token: str) -> int:
    match = _REGISTER_RE.match(token.strip())
    if not match:
        raise EncodingError("预期寄存器（形如 x10），实际是 %r" % token)
    index = int(match.group(1))
    if not 0 <= index <= 31:
        raise EncodingError("寄存器越界 x0-x31: %r" % token)
    return index


def _parse_qubit(token: str) -> int:
    token = token.strip()
    if not token.isdigit():
        raise EncodingError("预期量子比特下标（十进制整数），实际是 %r" % token)
    index = int(token)
    if index > MAX_QUBIT_INDEX:
        raise EncodingError("比特下标超出 5 位字段上限 %d: %d" % (MAX_QUBIT_INDEX, index))
    return index


def assemble_quantum(mnemonic: str, operands: Sequence[str]) -> int:
    """把一条量子助记符汇编成 32 位机器码。"""
    mnemonic = mnemonic.lower()

    if mnemonic == "q.gate":
        if not operands:
            raise EncodingError("q.gate 缺少门名")
        name = operands[0].strip().lower()
        if name not in GATE_FUNCT7:
            raise EncodingError("未知的门名 %r" % name)
        funct7 = GATE_FUNCT7[name]
        _, arity, parametrized = GATE_TABLE[funct7]
        if parametrized:
            raise EncodingError("%s 是参数门，应当用 q.gatep" % name)
        qubits = operands[1:]
        if len(qubits) != arity:
            raise EncodingError("%s 需要 %d 个比特，收到 %d 个" % (name, arity, len(qubits)))
        indices = [_parse_qubit(token) for token in qubits]
        fields = {"rs1": indices[0]}
        if arity >= 2:
            fields["rs2"] = indices[1]
        if arity >= 3:
            fields["rd"] = indices[2]
        return encode(FUNCT3_GATE, funct7, **fields)

    if mnemonic == "q.gatep":
        if len(operands) < 3:
            raise EncodingError("q.gatep 至少需要 门名、比特、角度寄存器")
        name = operands[0].strip().lower()
        if name not in GATE_FUNCT7:
            raise EncodingError("未知的门名 %r" % name)
        funct7 = GATE_FUNCT7[name]
        _, arity, parametrized = GATE_TABLE[funct7]
        if not parametrized:
            raise EncodingError("%s 不是参数门，应当用 q.gate" % name)
        qubits = operands[1:-1]
        if len(qubits) != arity:
            raise EncodingError("%s 需要 %d 个比特，收到 %d 个" % (name, arity, len(qubits)))
        indices = [_parse_qubit(token) for token in qubits]
        fields = {"rs1": indices[0], "rd": _parse_register(operands[-1])}
        if arity >= 2:
            fields["rs2"] = indices[1]
        return encode(FUNCT3_GATEP, funct7, **fields)

    if mnemonic == "q.meas":
        if len(operands) != 2:
            raise EncodingError("q.meas 需要 比特下标、目标寄存器")
        return encode(
            FUNCT3_MEAS, 0, rs1=_parse_qubit(operands[0]), rd=_parse_register(operands[1])
        )

    if mnemonic == "q.init":
        if len(operands) != 1:
            raise EncodingError("q.init 需要 比特下标")
        return encode(FUNCT3_INIT, 0, rs1=_parse_qubit(operands[0]))

    raise EncodingError("未知的量子助记符 %r" % mnemonic)


def disassemble(word: int) -> str:
    """把机器码还原成助记符，便于人工核对与调试。"""
    instruction = decode(word)
    if instruction.funct3 == FUNCT3_GATE:
        _, arity, _ = GATE_TABLE[instruction.funct7]
        operands = [instruction.rs1, instruction.rs2, instruction.rd][:arity]
        return "q.gate %s, %s" % (
            instruction.gate_name,
            ", ".join(str(index) for index in operands),
        )
    if instruction.funct3 == FUNCT3_GATEP:
        _, arity, _ = GATE_TABLE[instruction.funct7]
        qubits = [instruction.rs1, instruction.rs2][:arity]
        return "q.gatep %s, %s, x%d" % (
            instruction.gate_name,
            ", ".join(str(index) for index in qubits),
            instruction.rd,
        )
    if instruction.funct3 == FUNCT3_MEAS:
        return "q.meas %d, x%d" % (instruction.rs1, instruction.rd)
    return "q.init %d" % instruction.rs1


# --- B. 扩展模拟器 ----------------------------------------------------------


@dataclass
class _Decoded:
    """指令流里的一条。经典指令保留 token，量子指令保留 32 位机器码。"""

    is_quantum: bool
    word: int = 0
    op: str = ""
    args: List[str] = field(default_factory=list)


class TinyQuantumRISCVEmulator:
    """经典 7 指令 + LoomQ-Q 量子扩展。

    经典部分逐字沿用官方实现的语义，量子部分按
    docs/quantum-riscv-extension.md 执行。
    """

    def __init__(self, n_qubits: int = 0, seed: Optional[int] = None):
        # --- 与官方实现一致的部分 ---
        self.registers = [0] * 32
        self.pc = 0
        self.labels: Dict[str, int] = {}
        self.instructions: List[_Decoded] = []
        self.max_steps = 1000
        # --- 扩展部分 ---
        self.n_qubits = n_qubits
        self.statevector: List[complex] = []
        self.measurements: List[Tuple[int, int]] = []  # (比特, 结果)，便于测试核对
        self._random = random.Random(seed)
        self._reset_quantum()

    # -- 官方语义：寄存器访问 --

    def set_register(self, reg: str, value: int) -> None:
        index = self._parse_reg_idx(reg)
        if index != 0:  # x0 硬连线为零
            self.registers[index] = value

    def get_register(self, reg: str) -> int:
        return self.registers[self._parse_reg_idx(reg)]

    def _parse_reg_idx(self, reg: str) -> int:
        reg = reg.strip().replace(",", "")
        if not reg.lower().startswith("x"):
            raise ValueError("无效的寄存器名称: %s" % reg)
        index = int(reg[1:])
        if index < 0 or index > 31:
            raise ValueError("寄存器索引超出范围 (x0-x31): %s" % reg)
        return index

    # -- 扩展：量子态 --

    def _reset_quantum(self) -> None:
        if self.n_qubits <= 0:
            self.statevector = []
            return
        self.statevector = [0j] * (1 << self.n_qubits)
        self.statevector[0] = 1 + 0j
        self.measurements = []

    # -- 载入：编码所有指令 --

    def load_program(self, asm_code: str) -> None:
        """解析汇编、建立标签索引，并把量子指令编码成 32 位机器码。

        标签与注释的处理方式与官方实现一致；量子指令多走一步真编码。
        """
        self.instructions = []
        self.labels = {}
        self.pc = 0
        self.registers = [0] * 32
        self._reset_quantum()

        decoded: List[_Decoded] = []
        for raw in asm_code.split("\n"):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "#" in line:
                line = line.split("#")[0].strip()
            if not line:
                continue

            # 标签：整行标签，或 "LABEL: instr"。注意量子助记符自带点号不带冒号，
            # 所以按冒号判定不会与 q.gate 冲突。
            if line.endswith(":"):
                self.labels[line[:-1].strip()] = len(decoded)
                continue
            if ":" in line:
                label, _, line = line.partition(":")
                self.labels[label.strip()] = len(decoded)
                line = line.strip()
                if not line:
                    continue

            tokens = line.replace(",", " ").split()
            op = tokens[0].lower()
            args = tokens[1:]
            if op.startswith("q."):
                decoded.append(_Decoded(True, word=assemble_quantum(op, args)))
            else:
                decoded.append(_Decoded(False, op=op, args=args))

        self.instructions = decoded

    # -- 执行 --

    def execute(self) -> Dict[str, int]:
        steps = 0
        count = len(self.instructions)

        while 0 <= self.pc < count:
            steps += 1
            if steps > self.max_steps:
                raise RuntimeError("程序执行超出最大步数限制，疑似发生死循环")

            entry = self.instructions[self.pc]
            next_pc = self.pc + 1

            if entry.is_quantum:
                self._execute_quantum(decode(entry.word))
            else:
                next_pc = self._execute_classical(entry, next_pc)

            self.pc = next_pc

        return {
            "x%d" % index: value
            for index, value in enumerate(self.registers)
            if value != 0
        }

    def _execute_classical(self, entry: _Decoded, next_pc: int) -> int:
        """官方实现的 7 条指令，语义逐条对齐，不做任何改动。"""
        op, args = entry.op, entry.args

        if op == "li":
            self.set_register(args[0], int(args[1]))
        elif op == "add":
            self.set_register(
                args[0], self.get_register(args[1]) + self.get_register(args[2])
            )
        elif op == "sub":
            self.set_register(
                args[0], self.get_register(args[1]) - self.get_register(args[2])
            )
        elif op == "addi":
            self.set_register(args[0], self.get_register(args[1]) + int(args[2]))
        elif op == "beq":
            if self.get_register(args[0]) == self.get_register(args[1]):
                next_pc = self._label(args[2])
        elif op == "bne":
            if self.get_register(args[0]) != self.get_register(args[1]):
                next_pc = self._label(args[2])
        elif op == "j":
            next_pc = self._label(args[0])
        else:
            raise ValueError("不支持的指令操作: %s" % op)
        return next_pc

    def _label(self, name: str) -> int:
        if name not in self.labels:
            raise ValueError("未定义的跳转标签: %s" % name)
        return self.labels[name]

    def _execute_quantum(self, instruction: QuantumInstruction) -> None:
        if instruction.funct3 == FUNCT3_INIT:
            # 先测量再按结果施加 x：对纠缠态也有正确定义
            if self._measure(instruction.rs1):
                self._apply(Gate("x", (instruction.rs1,)))
            return

        if instruction.funct3 == FUNCT3_MEAS:
            outcome = self._measure(instruction.rs1)
            if instruction.rd:  # 写 x0 按 RISC-V 惯例丢弃
                self.registers[instruction.rd] = outcome
            self.measurements.append((instruction.rs1, outcome))
            return

        name, arity, parametrized = GATE_TABLE[instruction.funct7]
        qubits = [instruction.rs1, instruction.rs2, instruction.rd][:arity]
        for qubit in qubits:
            if qubit >= self.n_qubits:
                raise ValueError(
                    "比特下标 %d 超出本机 %d 个比特" % (qubit, self.n_qubits)
                )
        params: Tuple[float, ...] = ()
        if parametrized:
            import math

            raw = self.registers[instruction.rd]
            params = (raw * math.pi / ANGLE_SCALE,)
        self._apply(Gate(name, tuple(qubits), params))

    def _apply(self, gate: Gate) -> None:
        if not self.statevector:
            raise ValueError("本机未配置量子比特，无法执行量子指令")
        # 直接复用 L1 参考模拟器的门实现：量子语义与 L1 判定同源，
        # 不存在扩展模拟器和参考模拟器算得不一样的风险。
        _apply_gate(self.statevector, self.n_qubits, gate)

    def _measure(self, qubit: int) -> int:
        if qubit >= self.n_qubits:
            raise ValueError("比特下标 %d 超出本机 %d 个比特" % (qubit, self.n_qubits))
        mask = 1 << qubit
        probability_one = sum(
            abs(amplitude) ** 2
            for index, amplitude in enumerate(self.statevector)
            if index & mask
        )
        outcome = 1 if self._random.random() < probability_one else 0
        # 塌缩：清零不一致的分量后重新归一化
        norm = probability_one if outcome else 1.0 - probability_one
        if norm <= 0:
            # 抽样与概率不一致只可能来自浮点边界，退回确定性分支
            outcome = 1 if probability_one > 0.5 else 0
            norm = probability_one if outcome else 1.0 - probability_one
        scale = 1.0 / (norm**0.5)
        for index in range(len(self.statevector)):
            if bool(index & mask) == bool(outcome):
                self.statevector[index] *= scale
            else:
                self.statevector[index] = 0j
        return outcome

    def probabilities(self) -> Dict[str, float]:
        """当前态的测量概率，key 用大赛约定（最右字符是 q[0]）。"""
        result: Dict[str, float] = {}
        for index, amplitude in enumerate(self.statevector):
            weight = abs(amplitude) ** 2
            if weight > 1e-12:
                key = format(index, "0%db" % self.n_qubits)
                result[key] = result.get(key, 0.0) + weight
        return result


# --- C. 把 L3 的两份产物缝成一条指令流 --------------------------------------

_MEASURE_RE = re.compile(r"measure\s+q\[(\d+)\]\s*->\s*c\[(\d+)\]", re.IGNORECASE)
_GATE_RE = re.compile(r"^([a-z0-9]+)\s*(?:\(([^)]*)\))?\s*(.*);?$", re.IGNORECASE)
_QUBIT_RE = re.compile(r"q\[(\d+)\]")


def quantum_ops_to_asm(quantum_ops: Sequence[str], clbit_base: int = 10) -> List[str]:
    """把 compile_hybrid 的量子操作序列翻译成自定义指令。

    `measure q[k] -> c[j]` 落到 `q.meas k, x(clbit_base+j)`——正好是 L3 约定的
    测量位寄存器，于是经典段生成的代码不用改一个字就能读到测量结果。
    """
    lines: List[str] = []
    for statement in quantum_ops:
        text = statement.strip().rstrip(";").strip()
        if not text:
            continue

        measure = _MEASURE_RE.match(text)
        if measure:
            qubit, clbit = int(measure.group(1)), int(measure.group(2))
            lines.append("q.meas %d, x%d" % (qubit, clbit_base + clbit))
            continue

        match = _GATE_RE.match(text)
        if not match:
            raise EncodingError("无法翻译的量子语句: %r" % statement)
        name = match.group(1).lower()
        params = match.group(2)
        qubits = _QUBIT_RE.findall(match.group(3))
        if name not in GATE_FUNCT7:
            raise EncodingError("门 %r 不在 LoomQ-Q 扩展的门表里" % name)
        _, _, parametrized = GATE_TABLE[GATE_FUNCT7[name]]
        if not parametrized:
            lines.append("q.gate %s, %s" % (name, ", ".join(qubits)))
            continue
        if params is None:
            raise EncodingError("参数门 %s 缺少角度" % name)
        import math

        angle = float(eval_angle(params))
        # 角度是编译期常量，用一条 li 装进临时寄存器再交给 q.gatep
        raw = int(round(angle * ANGLE_SCALE / math.pi))
        lines.append("li x31, %d" % raw)
        lines.append("q.gatep %s, %s, x31" % (name, ", ".join(qubits)))
    return lines


def eval_angle(expression: str) -> float:
    from loomq.ir import eval_param

    return eval_param(expression)


def assemble_hybrid_program(
    quantum_ops: Sequence[str], classical_asm: str, clbit_base: int = 10
) -> str:
    """量子指令 + 经典汇编 → 一条混合指令流。

    量子部分在前、经典部分在后：测量先发生，经典分支才有东西可读。
    """
    lines = ["# LoomQ-Q 混合指令流：量子自定义指令 + 经典控制"]
    lines += quantum_ops_to_asm(quantum_ops, clbit_base)
    lines.append("# --- 经典段 ---")
    lines.append(classical_asm.rstrip())
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    # 最小演示：Bell 态 + 测量驱动经典分支
    program = """
    q.gate h, 0
    q.gate cx, 0, 1
    q.meas 0, x10
    q.meas 1, x11
    # 两个测量位相等就把 x1 置 1，否则置 0（Bell 态下应恒为 1）
    sub x20, x10, x11
    bne x20, x0, DIFFER
    li x1, 1
    j END
    DIFFER:
    li x1, 0
    END:
    """
    for trial in range(5):
        machine = TinyQuantumRISCVEmulator(n_qubits=2, seed=trial)
        machine.load_program(program)
        state = machine.execute()
        print(
            "seed=%d  测量=%s  x1=%d"
            % (trial, machine.measurements, state.get("x1", 0))
        )
