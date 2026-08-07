"""执行层：统一 IR → 真实 SDK 运行 → 统一结果 Schema。

本文件是唯一允许出现后端 SDK 依赖的地方，且全部为惰性导入——没装某个 SDK 时，
只有用到它的那个 target 会失败，不影响其他后端和离线自测。

已于 2026-08-07 实测通过：Python 3.10.11 + amazon-braket-sdk 1.97.0 +
amazon-braket-default-simulator 1.27.0 + spinqit 0.2.4（Windows）。
公开电路 evaluator L1 四个 case 全 PASS，六个隐藏回归电路 × 两后端保真度均 ≥0.989。
originq 未装 pyqpanda，仍未验证。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict

from .counts import build_result, coerce_to_counts, normalize
from .emitters import BRAKET_DIALECT_NAMES, emit_originir, emit_qasm2, emit_qasm3
from .ir import Circuit

BACKEND_IDS = {
    # 实际调用的是 spinqit 的 basic simulator，不是 Taurus，名字如实反映
    "spinq": "spinq_basic_simulator",
    "braket": "braket_local_simulator",
    "originq": "originq_local_simulator",
}


class BackendUnavailable(RuntimeError):
    """SDK 未安装或后端不可用。信息里必须写清装什么，方便另一台机器排错。"""


def _meta(circuit: Circuit) -> Dict[str, int]:
    return {
        "transpiled_gates": len(circuit.gates()),
        "depth": circuit.depth_estimate(),
    }


# --- AWS Braket LocalSimulator ---------------------------------------------


def run_braket(circuit: Circuit, shots: int) -> Dict[str, Any]:
    try:
        from braket.devices import LocalSimulator
        from braket.ir.openqasm import Program
    except ImportError as exc:
        raise BackendUnavailable(
            "未安装 amazon-braket-sdk。请在 Python 3.10 环境执行：pip install amazon-braket-sdk"
        ) from exc

    # Braket 的 OpenQASM 方言不 include stdgates.inc，门名用 si/ti/cnot/ccnot/cphaseshift。
    # 已实测：这套方言名 LocalSimulator 全部接受，六个回归电路（含 cu1/ccx/swap）均跑通。
    # 不要拿 emit_qasm3 的默认输出来执行——那份是交给评测器判定的，必须保持 stdgates 规范。
    source = emit_qasm3(circuit, names=BRAKET_DIALECT_NAMES, include=None)

    device = LocalSimulator()
    task = device.run(Program(source=source), shots=shots)
    result = task.result()

    raw = dict(result.measurement_counts)
    counts = coerce_to_counts(raw, shots)
    # 已实测：Braket 原生位串以 q[0] 为最左字符，与大赛约定（最右为 c[0]）相反，
    # 所以 counts.NATIVE_MATCHES_CONTEST["braket"] = False，由 normalize 反转。
    counts = normalize(counts, circuit.n_clbits or circuit.n_qubits, "braket")

    return build_result(
        backend=BACKEND_IDS["braket"],
        job_id=str(result.task_metadata.id),
        shots=shots,
        counts=counts,
        timestamp=None,
        **_meta(circuit),
    )


# --- 量旋 SpinQit 本地模拟器 ------------------------------------------------


def run_spinq(circuit: Circuit, shots: int) -> Dict[str, Any]:
    try:
        from spinqit import BasicSimulatorConfig, get_basic_simulator, get_compiler
    except ImportError as exc:
        raise BackendUnavailable(
            "未安装 spinqit。它只提供 cp310 wheel，必须在 Python 3.10 环境执行：pip install spinqit"
        ) from exc

    # SpinQit 原生吃 OpenQASM 2.0，所以转译产物与执行输入是同一份字符串。
    source = emit_qasm2(circuit)

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".qasm", delete=False, encoding="utf-8"
    )
    try:
        handle.write(source)
        handle.close()
        ir = get_compiler("qasm").compile(handle.name, 0)
    finally:
        os.unlink(handle.name)

    config = BasicSimulatorConfig()
    config.configure_shots(shots)
    result = get_basic_simulator().execute(ir, config)

    # 已实测（spinqit 0.2.4）：result.counts 是整数计数且总和等于 shots，但**不是随机采样**——
    # 它把精确概率乘以 shots，所以 1000 shots 的 GHZ 会稳定给出 500/500。
    # 于是 spinq 侧的保真度总是 1.0000，采样噪声只会在 braket 侧出现，别把这当成 bug。
    # coerce_to_counts 仍保留：别的小版本返回概率时它能兜住，且保证总和精确等于 shots。
    counts = coerce_to_counts(dict(result.counts), shots)
    counts = normalize(counts, circuit.n_clbits or circuit.n_qubits, "spinq")

    job_id = (
        getattr(result, "job_id", None)
        or getattr(result, "task_id", None)
        or "spinq-local-%04x" % (hash(source) & 0xFFFF)
    )

    return build_result(
        backend=BACKEND_IDS["spinq"],
        job_id=str(job_id),
        shots=shots,
        counts=counts,
        timestamp=None,
        **_meta(circuit),
    )


# --- 本源量子 pyqpanda 本地模拟器（进阶项，装不上可跳过）--------------------


def run_originq(circuit: Circuit, shots: int) -> Dict[str, Any]:
    try:
        import pyqpanda as pq
    except ImportError as exc:
        raise BackendUnavailable(
            "未安装 pyqpanda。originq 属于 L1 进阶项，装不上不影响入门档资格线"
        ) from exc

    source = emit_originir(circuit)

    machine = pq.CPUQVM()
    machine.init_qvm()
    try:
        program, _, cbits = pq.convert_originir_str_to_qprog(source, machine)
        raw = machine.run_with_configuration(program, cbits, shots)
        counts = coerce_to_counts(dict(raw), shots)
        counts = normalize(counts, circuit.n_clbits or circuit.n_qubits, "originq")
    finally:
        machine.finalize()

    return build_result(
        backend=BACKEND_IDS["originq"],
        job_id="originq-local-%04x" % (hash(source) & 0xFFFF),
        shots=shots,
        counts=counts,
        timestamp=None,
        **_meta(circuit),
    )


RUNNERS = {
    "spinq": run_spinq,
    "braket": run_braket,
    "originq": run_originq,
}


def execute(circuit: Circuit, target: str, shots: int) -> Dict[str, Any]:
    if target not in RUNNERS:
        raise ValueError("未知的执行目标 %r，可选：%s" % (target, ", ".join(sorted(RUNNERS))))
    if not isinstance(shots, int) or shots <= 0:
        raise ValueError("shots 必须是正整数，收到 %r" % (shots,))
    return RUNNERS[target](circuit, shots)


def available() -> Dict[str, bool]:
    """探测哪些后端当前可用，供 RUNBOOK 里的环境检查使用。"""
    probes = {
        "spinq": "spinqit",
        "braket": "braket",
        "originq": "pyqpanda",
    }
    import importlib.util

    return {
        target: importlib.util.find_spec(module) is not None
        for target, module in probes.items()
    }
