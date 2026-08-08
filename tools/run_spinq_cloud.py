#!/usr/bin/env python3
"""量旋云真机：查平台 / 标定位序 / 跑正式电路存证据。

真机是加分项（每平台 +5，最多两个），但更要紧的是专项奖标准里明写了"在真实量子机上
完成人生第一个实验"，所以这一项对我们不是可选的。

三个子命令，按顺序用：

    python tools/run_spinq_cloud.py platforms
        列出账号能看到的平台、比特上限、在线机器数。不提交任何任务，不消耗额度。

    python tools/run_spinq_cloud.py calibrate
        提交一个**非对称**探针电路（只对 q[0] 施加 x），据此读出真机的原生位序。
        必须先做这一步：bell/ghz 这类回文对称分布反转后完全一样，测不出位序错误。

    python tools/run_spinq_cloud.py run --qasm <路径>
        跑正式电路，把统一结果与原始返回写进 evidence/files/。

凭据只从环境变量读：
    LOOMQ_SPINQ_USERNAME   量旋云用户名
    LOOMQ_SPINQ_KEYFILE    私钥路径（PEM 格式，用 tools/make_spinq_key.py 生成）
    LOOMQ_SPINQ_HOST       可选，默认 http://cloud.spinq.cn:6060
    LOOMQ_SPINQ_PLATFORM   可选，指定平台代号；不指定则自动挑够用且在线的最小平台
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "starter_kit")
if KIT not in sys.path:
    sys.path.insert(0, KIT)

from loomq import backends  # noqa: E402
from loomq.ir import Circuit, Gate, Measure  # noqa: E402
from loomq.qasm2_parser import parse  # noqa: E402

EVIDENCE_DIR = os.path.join(KIT, "evidence", "files")


def cmd_platforms(args: argparse.Namespace) -> int:
    backend = backends.connect_spinq_cloud()
    platforms = getattr(backend, "_platforms", []) or []
    if not platforms:
        print("服务端没有返回任何平台。可能是账号权限问题，或公钥还没生效。")
        return 1

    print("登录成功。账号可见的平台：")
    print()
    print("  %-20s %-8s %-10s %s" % ("平台代号", "比特上限", "在线机器", "类型"))
    print("  " + "-" * 56)
    for platform in sorted(platforms, key=lambda item: item.max_bitnum):
        print(
            "  %-20s %-8d %-10d %s"
            % (
                platform.code,
                platform.max_bitnum,
                platform.machine_count,
                "云模拟器" if platform.simu else "真机",
            )
        )
    print()
    usable = [p for p in platforms if p.available() and not p.simu]
    if usable:
        print("当前有机器在线的真机：%s" % "、".join(p.code for p in usable))
        print("可以往下做 calibrate 了。")
    else:
        print("当前没有真机在线。真机会上下线，过一会儿再查；")
        print("排队本身不影响评奖资格，但真机分要有机器才能拿。")
    return 0


def _probe_circuit(n_qubits: int) -> Circuit:
    """非对称探针：只翻转 q[0]。

    大赛约定位串最右字符是 c[0]，所以 n=2 时正确结果必须是 "01"。
    若真机返回 "10"，说明它的原生位串以 q[0] 为最左字符，需要反转。
    对称电路（bell/ghz）在这里毫无用处——反转后分布一模一样。
    """
    return Circuit(
        n_qubits=n_qubits,
        n_clbits=n_qubits,
        ops=[Gate("x", (0,))] + [Measure(i, i) for i in range(n_qubits)],
    )


def _verdict(counts: Dict[str, int], n_qubits: int) -> Optional[bool]:
    """由探针结果判断原生位序是否与大赛约定一致。"""
    if not counts:
        return None
    dominant = max(counts, key=lambda key: counts[key]).zfill(n_qubits)
    contest = ("0" * (n_qubits - 1)) + "1"   # 最右为 c[0]，只有 q[0] 是 1
    reversed_form = "1" + ("0" * (n_qubits - 1))
    if dominant == contest:
        return True
    if dominant == reversed_form:
        return False
    return None


def cmd_calibrate(args: argparse.Namespace) -> int:
    circuit = _probe_circuit(args.qubits)
    print("提交非对称探针电路（%d 比特，只对 q[0] 施加 x），shots=%d" % (args.qubits, args.shots))
    print("大赛约定下正确结果应为 %s；若返回 %s 则说明原生位序相反。"
          % (("0" * (args.qubits - 1)) + "1", "1" + ("0" * (args.qubits - 1))))
    print()

    # 标定阶段必须看**原始**结果，所以显式传 True 让 normalize 不做任何反转
    try:
        raw = _execute_raw(circuit, args.shots, args.platform, args.timeout, "loomq-bitorder-probe")
    except backends.SpinQCloudPending as exc:
        print("任务还没出结果：%s" % exc)
        print("编号 %s，稍后可以用 --fetch 取回" % exc.task_code)
        return 2

    counts, task_code = raw
    print("任务编号（可在控制台溯源）：%s" % task_code)
    print("原始返回：%s" % json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print()

    verdict = _verdict(counts, args.qubits)
    if verdict is True:
        print("结论：原生位序与大赛约定一致。")
        print("把 counts.py 的 NATIVE_MATCHES_CONTEST[\"spinq_cloud\"] 填 True。")
    elif verdict is False:
        print("结论：原生位序与大赛约定相反，需要反转。")
        print("把 counts.py 的 NATIVE_MATCHES_CONTEST[\"spinq_cloud\"] 填 False。")
    else:
        print("结论：无法判定。主峰不是单一的 %s 或 %s，可能是真机噪声太大或映射异常。"
              % (("0" * (args.qubits - 1)) + "1", "1" + ("0" * (args.qubits - 1))))
        print("真机有噪声，主峰概率不会是 100%，但应当明显高于其他结果。请看上面的原始分布。")
        return 1
    return 0


def _execute_raw(circuit, shots, platform, timeout, task_name):
    """跑一次并返回 (未做位序归一化的 counts, task_code)。仅供标定使用。"""
    from spinqit import SpinQCloudConfig

    backend = backends.connect_spinq_cloud()
    platform = backends._pick_platform(
        backend, circuit.n_qubits, platform or os.environ.get("LOOMQ_SPINQ_PLATFORM")
    )
    print("使用平台：%s" % platform)

    ir = backends._compile_qasm(backends._measure_free_qasm(circuit))
    config = SpinQCloudConfig()
    config.configure_platform(platform)
    config.configure_shots(shots)
    config.configure_task(task_name, "LoomQ 位序标定")

    status, message, task_code = backend.submit_task(ir, config)
    if status not in (200, 202):
        raise backends.SpinQCloudPending(
            "已提交但未执行（status=%s，%s）" % (status, message), str(task_code)
        )
    result = backend.get_task_result(task_code, hanging=True, timeout=timeout)
    if result is None or result.counts is None:
        raise backends.SpinQCloudPending("尚无结果", str(task_code))
    return dict(result.counts), str(task_code)


def cmd_run(args: argparse.Namespace) -> int:
    from loomq.counts import NATIVE_MATCHES_CONTEST

    if NATIVE_MATCHES_CONTEST.get("spinq_cloud") is None:
        print("spinq_cloud 的位序还没标定。先跑：")
        print("  python tools/run_spinq_cloud.py calibrate")
        print("否则结果的位序无法保证，真机证据会站不住。")
        if not args.force:
            return 2
        print("（--force 已指定，继续，但结果仅供调试）")
        print()

    with open(args.qasm, encoding="utf-8") as handle:
        circuit = parse(handle.read())

    print("电路：%s（%d 比特，%d 门）" % (args.qasm, circuit.n_qubits, len(circuit.gates())))
    try:
        result = backends.run_spinq_cloud(
            circuit, args.shots, platform=args.platform, timeout=args.timeout,
            task_name=args.task_name,
        )
    except backends.SpinQCloudPending as exc:
        print("任务还没出结果：%s" % exc)
        print("编号 %s" % exc.task_code)
        return 2

    print()
    print("任务编号（控制台可溯源）：%s" % result["job_id"])
    print("结果分布：")
    total = sum(result["counts"].values())
    for key in sorted(result["counts"], key=lambda k: -result["counts"][k]):
        value = result["counts"][key]
        print("  %s  %6.2f%%  (%d)" % (key, 100.0 * value / total, value))

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    stem = args.name or ("spinq-cloud-" + os.path.splitext(os.path.basename(args.qasm))[0])
    result_path = os.path.join(EVIDENCE_DIR, stem + "-result.json")
    qasm_path = os.path.join(EVIDENCE_DIR, stem + "-circuit.qasm")
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
    with open(qasm_path, "w", encoding="utf-8") as handle:
        handle.write(open(args.qasm, encoding="utf-8").read())

    print()
    print("证据已保存：")
    print("  %s" % result_path)
    print("  %s" % qasm_path)
    print()
    print("接下来把任务编号、运行时间、shots 填进 starter_kit/evidence/README.md，")
    print("并在控制台把任务页截图存到 evidence/files/。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="量旋云真机操作")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("platforms", help="列出可用平台与在线机器数")
    p1.set_defaults(func=cmd_platforms)

    p2 = sub.add_parser("calibrate", help="用非对称探针标定原生位序")
    p2.add_argument("--qubits", type=int, default=2)
    p2.add_argument("--shots", type=int, default=1024)
    p2.add_argument("--platform")
    p2.add_argument("--timeout", type=int, default=1800, help="等结果的秒数，默认 30 分钟")
    p2.set_defaults(func=cmd_calibrate)

    p3 = sub.add_parser("run", help="跑正式电路并存证据")
    p3.add_argument("--qasm", required=True)
    p3.add_argument("--shots", type=int, default=1024)
    p3.add_argument("--platform")
    p3.add_argument("--timeout", type=int, default=3600, help="等结果的秒数，默认 1 小时")
    p3.add_argument("--task-name")
    p3.add_argument("--name", help="证据文件名前缀")
    p3.add_argument("--force", action="store_true", help="位序未标定也强行跑")
    p3.set_defaults(func=cmd_run)

    args = parser.parse_args()
    try:
        return args.func(args)
    except backends.BackendUnavailable as exc:
        print("后端不可用：%s" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
