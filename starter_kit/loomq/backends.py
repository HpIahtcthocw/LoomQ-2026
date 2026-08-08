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
from typing import Any, Callable, Dict, Optional

from .counts import build_result, coerce_to_counts, normalize
from .emitters import BRAKET_DIALECT_NAMES, emit_originir, emit_qasm2, emit_qasm3
from .ir import Circuit, Measure

BACKEND_IDS = {
    # 实际调用的是 spinqit 的 basic simulator，不是 Taurus，名字如实反映
    "spinq": "spinq_basic_simulator",
    "braket": "braket_local_simulator",
    "originq": "originq_local_simulator",
    # 量旋云真机。id 用官方能力表里的规范标识，便于证据溯源
    "spinq_cloud": "spinq_cloud_qpu",
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


# --- 量旋云真机 -------------------------------------------------------------
#
# 与本地模拟器有三处硬性差异，都是从 spinqit 0.2.4 源码确认的，不是猜的：
#
# 1. **不接受显式 measure。** SpinQCloudBackend.assemble 遇到 MEASURE 节点直接抛
#    CircuitOperationValidationError（"SpinQ Cloud currently does not support explicit
#    invocation of measure gates. A measure will be done automatically at the end"）。
#    所以提交给云端的 QASM 必须摘掉所有 measure，由平台在电路末尾自动全测量。
# 2. **只能全测量。** configure_measured_qubits 仅对 sqc_25_vp 与 simulator 生效，
#    其余平台会打印警告并返回全部比特的结果。因此结果位宽按 n_qubits 而非 n_clbits。
# 3. **execute() 会无限期阻塞。** 它内部调 get_task_result(hanging=True, timeout=None)，
#    每 5 秒轮询一次，排队几小时就阻塞几小时。所以这里不用 execute，改为自己
#    submit_task + get_task_result，好处是能设超时，且**超时也能拿到 task_code**——
#    真机证据要的就是这个可溯源编号，任务还在排队时也不该丢掉它。

SPINQ_CLOUD_DEFAULT_HOST = "http://cloud.spinq.cn:6060"

# 平台代号 -> 比特上限。取自 spinqit.model.spinqCloud.platform，仅作为挑选顺序的依据；
# 真正可用的平台列表由服务端 refresh_remote_platforms 返回，以那个为准。
SPINQ_CLOUD_PLATFORMS = (
    ("gemini_vp", 2),
    ("triangulum_vp", 3),
    ("superconductor_vp", 8),
    ("sqc_25_vp", 25),
)


class SpinQCloudPending(RuntimeError):
    """任务已提交但还没出结果。带上 task_code，便于稍后凭编号取回。"""

    def __init__(self, message: str, task_code: str) -> None:
        super().__init__(message)
        self.task_code = task_code


def _measure_free_qasm(circuit: Circuit) -> str:
    """生成不含 measure 的 QASM。云端会在电路末尾自动测量全部比特。

    creg 声明保留：它不会触发 assemble 的拒绝（那里只拦 MEASURE 节点），而 spinqit 的
    qasm 编译器要靠它确定 ir.dag['cnum']，Task 请求里带的就是这个值。
    """
    stripped = Circuit(
        n_qubits=circuit.n_qubits,
        n_clbits=circuit.n_clbits,
        ops=[op for op in circuit.ops if not isinstance(op, Measure)],
    )
    return emit_qasm2(stripped)


def _compile_qasm(source: str):
    from spinqit import get_compiler

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".qasm", delete=False, encoding="utf-8"
    )
    try:
        handle.write(source)
        handle.close()
        return get_compiler("qasm").compile(handle.name, 0)
    finally:
        os.unlink(handle.name)


def _pick_platform(backend, n_qubits: int, requested: Optional[str]) -> str:
    """挑一个装得下电路、且当前有机器在线的平台。

    真机是稀缺资源，随机器上下线变化，所以先问服务端要实时列表，再按比特数从小到大选
    ——够用就行，不去占更大的机器。
    """
    if requested:
        platform = backend.get_platform(requested)  # 不存在会抛 NotFoundError
        if platform.max_bitnum < n_qubits:
            raise BackendUnavailable(
                "平台 %s 只有 %d 个比特，装不下 %d 比特的电路"
                % (requested, platform.max_bitnum, n_qubits)
            )
        if not platform.available():
            raise BackendUnavailable(
                "平台 %s 当前没有机器在线（machine_count=%d），换个平台或稍后再试"
                % (requested, platform.machine_count)
            )
        return requested

    known = dict(SPINQ_CLOUD_PLATFORMS)
    candidates = []
    for platform in getattr(backend, "_platforms", []) or []:
        if platform.max_bitnum >= n_qubits and platform.available():
            candidates.append((platform.max_bitnum, known.get(platform.code, 99), platform.code))
    if not candidates:
        raise BackendUnavailable(
            "没有任何在线平台能装下 %d 比特的电路。可用平台：%s"
            % (
                n_qubits,
                ", ".join(
                    "%s(%d 比特, %d 台在线)"
                    % (item.code, item.max_bitnum, item.machine_count)
                    for item in (getattr(backend, "_platforms", []) or [])
                )
                or "（服务端未返回任何平台）",
            )
        )
    candidates.sort()
    return candidates[0][2]


def connect_spinq_cloud(
    username: Optional[str] = None,
    keyfile: Optional[str] = None,
    host: Optional[str] = None,
):
    """登录量旋云。凭据只从参数或环境变量来，不硬编码。"""
    try:
        from spinqit import get_spinq_cloud
    except ImportError as exc:
        raise BackendUnavailable(
            "未安装 spinqit。它只提供 cp310 wheel，必须在 Python 3.10 环境执行：pip install spinqit"
        ) from exc

    username = username or os.environ.get("LOOMQ_SPINQ_USERNAME")
    keyfile = keyfile or os.environ.get("LOOMQ_SPINQ_KEYFILE")
    host = host or os.environ.get("LOOMQ_SPINQ_HOST") or SPINQ_CLOUD_DEFAULT_HOST

    if not username or not keyfile:
        raise BackendUnavailable(
            "缺少量旋云凭据。请设置 LOOMQ_SPINQ_USERNAME 与 LOOMQ_SPINQ_KEYFILE"
            "（私钥路径，PEM 格式；用 tools/make_spinq_key.py 生成）"
        )
    if not os.path.isfile(keyfile):
        raise BackendUnavailable("私钥文件不存在：%s" % keyfile)

    return get_spinq_cloud(username, keyfile, host)


def run_spinq_cloud(
    circuit: Circuit,
    shots: int,
    platform: Optional[str] = None,
    timeout: Optional[int] = None,
    task_name: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """在量旋云真机上执行。

    progress 是可选的进度回调。真机排队加执行通常要一两分钟，CLI 要靠它在等待期间
    告诉用户"正在发生什么"以及任务编号是多少——默默卡住两分钟对零基础用户是灾难。
    """
    from spinqit import SpinQCloudConfig

    def report(text: str) -> None:
        if progress is not None:
            progress(text)

    backend = connect_spinq_cloud()
    platform = _pick_platform(
        backend, circuit.n_qubits, platform or os.environ.get("LOOMQ_SPINQ_PLATFORM")
    )
    report("已连上量旋云，使用平台 %s。" % platform)

    source = _measure_free_qasm(circuit)
    ir = _compile_qasm(source)

    config = SpinQCloudConfig()
    config.configure_platform(platform)
    config.configure_shots(shots)
    config.configure_task(task_name or "loomq-%s" % platform, "LoomQ 通用中间层真机验证")

    status, message, task_code = backend.submit_task(ir, config)
    if status in (200, 202):
        report("电路已提交，任务编号 %s。" % task_code)
        report("真机需要排队和执行，通常一到两分钟，请不要关掉窗口。")
    # 226 = 已保存但当前无机器在线；206 = 已保存但没有拓扑匹配的机器。
    # 官方 execute() 在这两种情况下会静默返回 None，这里改为如实抛出并带上编号。
    if status not in (200, 202):
        raise SpinQCloudPending(
            "任务已提交但未执行（status=%s，%s）。编号 %s 可稍后在控制台或用 "
            "--fetch 取回结果" % (status, message, task_code),
            str(task_code),
        )

    result = backend.get_task_result(task_code, hanging=True, timeout=timeout)
    if result is None or result.counts is None:
        raise SpinQCloudPending(
            "任务 %s 尚未返回结果（可能仍在排队或已失败）" % task_code, str(task_code)
        )

    counts = coerce_to_counts(dict(result.counts), shots)
    # 云端固定返回全部比特的结果，位宽按 n_qubits。
    # 位序尚未标定，NATIVE_MATCHES_CONTEST["spinq_cloud"] 是 None，normalize 会原样返回
    # 而不是瞎猜——先用 tools/probe_bitorder.py 打非对称探针，再回填。
    counts = normalize(counts, circuit.n_qubits, "spinq_cloud")

    return build_result(
        backend=BACKEND_IDS["spinq_cloud"],
        job_id=str(task_code),
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
    "spinq_cloud": run_spinq_cloud,
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
        # 装了 spinqit 只说明能尝试连接，真机是否可用还取决于凭据与机器在线状态
        "spinq_cloud": "spinqit",
    }
    import importlib.util

    return {
        target: importlib.util.find_spec(module) is not None
        for target, module in probes.items()
    }
