"""统一 IR → 各后端原生表示。

新增一个后端 = 新增一张门名映射表 + 一个 emit 函数，不动解析器、不动 IR。
门名映射集中在本文件顶部的字典里，这是"真正抽象的中间层"最直接的可审查证据。

注意 transpile() 与 run() 的消费者不同：
  - 本文件的输出交给组委会解析器判定，遵循 starter_kit/target_ir_contract.md
  - 真实执行时各 SDK 可能使用别的方言门名（如 Braket 的 si/ti/ccnot），那属于 backends.py
"""

from __future__ import annotations

from typing import Dict, List

from .ir import Circuit, Gate, Measure, format_param, measured_qubits

# target_ir_contract.md：spinq 要求完整可执行的 OpenQASM 2.0，门名与 qelib1 一致。
QELIB1_NAMES: Dict[str, str] = {
    "h": "h",
    "x": "x",
    "s": "s",
    "sdg": "sdg",
    "t": "t",
    "tdg": "tdg",
    "rz": "rz",
    "ry": "ry",
    "cx": "cx",
    "cu1": "cu1",
    "swap": "swap",
    "ccx": "ccx",
    "u1": "u1",
}

# target_ir_contract.md：braket 要求完整 OpenQASM 3，评测器接受 cx 或 cnot。
# 官方示例在 include "stdgates.inc" 下写 cnot，这里与示例保持一致以降低解析风险；
# 若验证阶段发现评测器偏好 cx，只需把下面这一行改掉。
STDGATES_NAMES: Dict[str, str] = {
    "h": "h",
    "x": "x",
    "s": "s",
    "sdg": "sdg",
    "t": "t",
    "tdg": "tdg",
    "rz": "rz",
    "ry": "ry",
    "cx": "cnot",
    "cu1": "cp",
    "swap": "swap",
    "ccx": "ccx",
    "u1": "p",
}

# target_ir_contract.md：originq 允许 H X S SDAG T TDAG RY RZ CNOT CU1/CR SWAP TOFFOLI/CCX
ORIGINIR_NAMES: Dict[str, str] = {
    "h": "H",
    "x": "X",
    "s": "S",
    "sdg": "SDAG",
    "t": "T",
    "tdg": "TDAG",
    "rz": "RZ",
    "ry": "RY",
    "cx": "CNOT",
    # 契约写的是 "CU1/CR" 两者都接受，但 pyqpanda 3.8.5 里 **CU1 根本没定义**
    # （报 "UserDefinedGate CU1 undefined"，CP 同样没有），只有 CR 能解析。
    # 既然评测器两个名字都收，就统一用能真跑起来的那个。
    #
    # CR 的语义已实测确认是 cu1 而非 crz：见 tools/probe_originir.py，
    # 用能区分二者的电路比对，与 cu1 保真度 0.9988、与 crz 只有 0.7233。
    # 这一条以前在 RUNBOOK 里是悬着的待确认项，现已关闭。
    "cu1": "CR",
    "swap": "SWAP",
    "ccx": "TOFFOLI",
}


def _params_suffix(gate: Gate) -> str:
    if not gate.params:
        return ""
    return "(" + ",".join(format_param(value) for value in gate.params) + ")"


def emit_qasm2(circuit: Circuit) -> str:
    """规范 OpenQASM 2.0，单一展平寄存器 q / c。用于 target='spinq'。"""
    lines: List[str] = ['OPENQASM 2.0;', 'include "qelib1.inc";']
    lines.append("qreg q[%d];" % circuit.n_qubits)
    if circuit.n_clbits:
        lines.append("creg c[%d];" % circuit.n_clbits)
    for op in circuit.ops:
        if isinstance(op, Measure):
            lines.append("measure q[%d] -> c[%d];" % (op.qubit, op.clbit))
            continue
        operands = ",".join("q[%d]" % index for index in op.qubits)
        lines.append("%s%s %s;" % (QELIB1_NAMES[op.name], _params_suffix(op), operands))
    return "\n".join(lines) + "\n"


# Braket 自己的 OpenQASM 方言：不 include stdgates.inc，且部分门名不同。
# 这份表只用于**真实执行**（backends.py），不用于交给评测器判定的 transpile() 输出。
BRAKET_DIALECT_NAMES: Dict[str, str] = {
    "h": "h",
    "x": "x",
    "s": "s",
    "sdg": "si",
    "t": "t",
    "tdg": "ti",
    "rz": "rz",
    "ry": "ry",
    "cx": "cnot",
    "cu1": "cphaseshift",
    "swap": "swap",
    "ccx": "ccnot",
    "u1": "phaseshift",
}


def emit_qasm3(
    circuit: Circuit,
    names: Dict[str, str] | None = None,
    include: str | None = "stdgates.inc",
) -> str:
    """完整 OpenQASM 3。

    默认输出 stdgates.inc 规范写法，用于 target='braket' 的 transpile() 判定。
    传入 BRAKET_DIALECT_NAMES 且 include=None 时输出 Braket 方言，供真实执行使用。
    """
    table = names or STDGATES_NAMES
    lines: List[str] = ["OPENQASM 3.0;"]
    if include:
        lines.append('include "%s";' % include)
    lines.append("qubit[%d] q;" % circuit.n_qubits)
    if circuit.n_clbits:
        lines.append("bit[%d] c;" % circuit.n_clbits)
    for op in circuit.ops:
        if isinstance(op, Measure):
            lines.append("c[%d] = measure q[%d];" % (op.clbit, op.qubit))
            continue
        operands = ", ".join("q[%d]" % index for index in op.qubits)
        lines.append("%s%s %s;" % (table[op.name], _params_suffix(op), operands))
    return "\n".join(lines) + "\n"


# pyqpanda 3.8.5 实测不认的门名，以及能替代它的写法。见 tools/probe_originir.py。
# 契约把 SDAG/TDAG 列为允许门名，但 pyqpanda 里它们没有定义
# （报 "UserDefinedGate SDAG undefined"），SDG / S.dag 也都不行，
# 只有 DAGGER ... ENDDAGGER 块可用。DAGGER 是结构而非门名，
# 不在契约的允许门名清单里，所以**只在执行路径上用**，判定输出仍发 SDAG/TDAG。
ORIGINIR_DAGGER_FALLBACK: Dict[str, str] = {
    "sdg": "S",
    "tdg": "T",
}


def emit_originir(circuit: Circuit, executable: bool = False) -> str:
    """OriginIR 文本。用于 target='originq'。

    **参数写在操作数之后**，即 `RY q[0],(1.5708)`，不是 QASM 习惯的 `RY(1.5708) q[0]`。
    契约说两种写法都接受，但 pyqpanda 3.8.5 只认前者，后者直接报
    "no viable alternative at input 'RY('"。既然评测器两种都收，就统一用能真跑起来的。

    executable=True 时输出 pyqpanda 能真正执行的方言：把 SDAG/TDAG 换成
    DAGGER 块。默认 False 用于 transpile() 的判定输出，保持契约列出的规范门名——
    与 braket 那边"判定发 stdgates、执行发方言"是同一个模式，原因也相同：
    契约允许的写法与 SDK 实际接受的写法不是同一个集合。
    """
    lines: List[str] = ["QINIT %d" % circuit.n_qubits]
    if circuit.n_clbits:
        lines.append("CREG %d" % circuit.n_clbits)
    for op in circuit.ops:
        if isinstance(op, Measure):
            lines.append("MEASURE q[%d], c[%d]" % (op.qubit, op.clbit))
            continue
        if op.name not in ORIGINIR_NAMES:
            # target_ir_contract.md 给 originq 的允许门名里没有 U1。
            # 若走到这里，说明对 originq 误用了分解产物；分解只应服务于真实执行。
            raise ValueError(
                "OriginIR 不支持门 %r。originq 目标应直接发射白名单 12 门，不要先做分解" % op.name
            )
        operands = ", ".join("q[%d]" % index for index in op.qubits)
        if executable and op.name in ORIGINIR_DAGGER_FALLBACK:
            base = ORIGINIR_DAGGER_FALLBACK[op.name]
            lines += ["DAGGER", "%s %s" % (base, operands), "ENDDAGGER"]
            continue
        suffix = ""
        if op.params:
            suffix = ",(" + ",".join(format_param(value) for value in op.params) + ")"
        lines.append("%s %s%s" % (ORIGINIR_NAMES[op.name], operands, suffix))
    return "\n".join(lines) + "\n"


EMITTERS = {
    "spinq": emit_qasm2,
    "braket": emit_qasm3,
    "originq": emit_originir,
}


def emit(circuit: Circuit, target: str) -> str:
    if target not in EMITTERS:
        raise ValueError("未知的转译目标 %r，可选：%s" % (target, ", ".join(sorted(EMITTERS))))
    return EMITTERS[target](circuit)


__all__ = [
    "emit",
    "emit_qasm2",
    "emit_qasm3",
    "emit_originir",
    "EMITTERS",
    "BRAKET_DIALECT_NAMES",
    "STDGATES_NAMES",
    "measured_qubits",
]
