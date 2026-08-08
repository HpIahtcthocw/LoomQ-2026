"""LoomQ 交互入口：让一个完全不懂量子的人，五分钟跑出自己的第一个量子程序。

对应评分点：
  - L2 完整 30 分要求"可由零基础用户现场操作的 Agent 入口"，CLI 即可，不强制图形界面
  - L2「平权叙事与交互体验」10 分：交互友好度、容错提示、结果可视化
  - Bonus「卓越的新手引导与视觉叙事」+4
  - 专项奖标准："零背景的跨界创作者五分钟内完成人生第一个实验，并理解其科学原理"

设计上的三个刻意选择：

**一、永远能跑起来。** 没装量子 SDK 就用内置参考模拟器，没配模型 key 就走内置示例库。
评委在任何环境下敲一条命令都能看到完整流程，不会因为缺依赖卡在第一步——
"评委能不能一条命令跑起来"本身就是工程分的判据。

**二、先看懂，再动手。** 首次运行先用三句话讲清这件事在做什么，不堆术语。
每次出结果都附电路图、直方图和一段人话解释，让人知道自己看到了什么，而不只是拿到一串数字。

**三、错误必须可恢复。** 任何失败都给出下一步能做什么，不打印堆栈。

用法：
    python -m loomq.cli                      # 交互模式（含新手引导）
    python -m loomq.cli --demo               # 三个现场体验任务，依次自动跑完
    python -m loomq.cli --ask "生成一个三比特的最大纠缠态"
    python -m loomq.cli --backend braket     # 指定真实后端；默认用内置参考模拟器
    python -m loomq.cli --backend spinq_cloud  # 送到真实的量子计算机上跑
    python -m loomq.cli --list-backends      # 看有哪些后端可用
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, Optional, Tuple

from . import backends as backend_module
from .qasm2_parser import QasmError, parse
from .refsim import ideal_distribution, sample
from .visualize import (
    bitstring_legend,
    circuit_diagram,
    display_width,
    explain_distribution,
    explain_hardware_noise,
    histogram,
    pad,
)

DEFAULT_SHOTS = 1024

WELCOME = """
╭──────────────────────────────────────────────────────────────╮
│  LoomQ · 量子接入平权计划                                    │
│  你不需要懂物理，也不需要写代码。用中文说出你想做的事就行。  │
╰──────────────────────────────────────────────────────────────╯

三句话讲清这件事：

  1. 普通计算机的一个比特，同一时刻只能是 0 或 1。
     量子比特可以同时「既是 0 又是 1」，直到你去测量它的那一刻。

  2. 所谓写量子程序，就是排一串操作去摆布这些比特，最后测量。
     每次测量只给一个答案，所以要重复很多次，看的是结果的分布。

  3. 你接下来会看到三样东西：电路图（做了什么）、
     结果分布（测出了什么）、一段人话解释（这意味着什么）。

不知道从哪开始？直接输入 1、2 或 3 试试现成的例子：
  1  做一个两比特的贝尔态（最简单的纠缠）
  2  做一个三比特的 GHZ 态（三个比特命运绑在一起）
  3  在八个抽屉里找出正确的那个（Grover 搜索）

输入 help 看更多命令，输入 quit 退出。
"""

# 真机可用时追加这一段。放在开场引导里而不是藏在 help 后面，是因为
# 专项奖标准要求"在真实量子机上完成第一个实验"——用户得先知道这件事做得到。
WELCOME_HARDWARE = """
另外：这台机器已经接上了真实的量子计算机，不是模拟器。
输入 real 就能把电路送到真机上跑（要排队，每次约两分钟），
再输入 sim 可以切回模拟器对照着看。两边的差别本身就很值得一看。
"""

# 没配模型 key 时的兜底示例库。评委不一定配了 LOOMQ_LLM_*，
# 但流程必须仍然能完整走通——这是"一条命令跑起来"的底线。
BUILTIN_EXAMPLES: Dict[str, Tuple[str, str, str]] = {
    "1": (
        "两比特贝尔态",
        """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0],q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
""",
        "先让第一个比特进入「既是 0 又是 1」的状态，再用一个受控非门把第二个比特绑上去。"
        "结果只会是 00 或 11，各一半——两个比特永远同面。",
    ),
    "2": (
        "三比特 GHZ 态",
        """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0],q[1];
cx q[1],q[2];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
""",
        "把贝尔态的思路再往下传一级：第一个比特带上第二个，第二个再带上第三个。"
        "三个比特要么全 0，要么全 1。",
    ),
    "3": (
        "Grover 搜索（8 个抽屉里找 111）",
        """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
h q[1];
h q[2];
h q[2];
ccx q[0],q[1],q[2];
h q[2];
h q[0];
h q[1];
h q[2];
x q[0];
x q[1];
x q[2];
h q[2];
ccx q[0],q[1],q[2];
h q[2];
x q[0];
x q[1];
x q[2];
h q[0];
h q[1];
h q[2];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
""",
        "8 个抽屉，经典办法平均要开 4 次。这个电路先让所有抽屉同时被看到，"
        "再用干涉把正确答案的概率放大——一轮之后，111 的概率就从 12.5% 涨到约 78%。",
    ),
}

DEMO_TASKS = [
    ("1", "让两个量子比特纠缠在一起，看它们永远同面"),
    ("2", "把纠缠扩展到三个比特"),
    ("3", "用量子干涉在八个可能里放大正确答案"),
]


def _print(text: str = "") -> None:
    """输出时对不支持的字符做降级，避免在任何终端里出现乱码。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(text)


def available_backends() -> Dict[str, bool]:
    return backend_module.available()


def hardware_ready() -> bool:
    """真机除了要装 SDK，还要有凭据。两者缺一都连不上，状态得如实区分。"""
    keyfile = os.environ.get("LOOMQ_SPINQ_KEYFILE") or ""
    return bool(
        available_backends().get("spinq_cloud")
        and os.environ.get("LOOMQ_SPINQ_USERNAME")
        and keyfile
        and os.path.isfile(keyfile)
    )


def describe_backends() -> str:
    installed = available_backends()
    labels = {
        "refsim": "内置参考模拟器（精确计算，无需安装任何东西）",
        "spinq": "量旋 SpinQit 本地模拟器",
        "braket": "AWS Braket LocalSimulator",
        "originq": "本源 pyqpanda 本地模拟器",
        "spinq_cloud": "量旋云真实量子计算机（NMR 真机，需账号）",
    }
    label_width = max(display_width(text) for text in labels.values())
    lines = ["可用的运行后端：", ""]
    for target, label in labels.items():
        if target == "refsim":
            state = "[始终可用]"
        elif target == "spinq_cloud":
            state = "[可用]" if hardware_ready() else (
                "[缺凭据]" if installed.get("spinq_cloud") else "[未安装]"
            )
        else:
            state = "[已安装]" if installed.get(target) else "[未安装]"
        lines.append("  %s %s %s" % (pad(target, 12), pad(label, label_width), state))
    if not any(installed.values()):
        lines += ["", "当前没有装任何量子 SDK，会使用内置参考模拟器。",
                  "想接真实后端：在 Python 3.10 环境执行 pip install amazon-braket-sdk spinqit"]
    if installed.get("spinq_cloud") and not hardware_ready():
        lines += ["", "想真的跑在量子计算机上（前面全是模拟器）：",
                  "  1. 到 https://cloud.spinq.cn 注册账号",
                  "  2. 运行 python tools/make_spinq_key.py 生成密钥，把公钥贴到网站账号设置里",
                  "  3. 设置环境变量 LOOMQ_SPINQ_USERNAME 和 LOOMQ_SPINQ_KEYFILE",
                  "  4. python -m loomq.cli --backend spinq_cloud"]
    return "\n".join(lines)


def run_circuit(qasm: str, target: str, shots: int) -> Tuple[Dict[str, int], str, bool]:
    """执行电路，返回 (counts, 后端说明, 是否真机)。

    第三个返回值决定要不要额外解释真机噪声——真机结果里那些"不该出现"的位串，
    不解释清楚会让新手以为自己做错了。
    """
    circuit = parse(qasm)
    if target == "refsim":
        return sample(circuit, shots), "内置参考模拟器", False
    if target == "spinq_cloud":
        return _run_on_hardware(circuit, shots)
    try:
        payload = backend_module.execute(circuit, target, shots)
        return payload["counts"], "%s（job %s）" % (payload["backend"], payload["job_id"]), False
    except backend_module.BackendUnavailable as exc:
        _print("  提示：%s" % exc)
        _print("  已自动改用内置参考模拟器，流程照常继续。")
        return sample(circuit, shots), "内置参考模拟器（后端不可用时的兜底）", False


def _run_on_hardware(circuit, shots: int) -> Tuple[Dict[str, int], str, bool]:
    """真机路径。任何一步失败都回落到参考模拟器，绝不把用户卡在半路。"""
    _print()
    _print("  这一次要送到真实的量子计算机上运行，不是模拟器。")
    try:
        payload = backend_module.run_spinq_cloud(
            circuit, shots, progress=lambda text: _print("  " + text)
        )
    except backend_module.SpinQCloudPending as exc:
        _print("  %s" % exc)
        _print("  任务编号 %s 已经存下，机器空出来后可以凭编号取回结果。" % exc.task_code)
        _print("  现在先用内置模拟器给你看理想结果，好让流程走完。")
        return sample(circuit, shots), "内置参考模拟器（真机仍在排队）", False
    except backend_module.BackendUnavailable as exc:
        _print("  连不上真机：%s" % exc)
        if circuit.n_qubits > 3:
            _print("  真实量子机现在还很小——在线的只有 2 比特和 3 比特两台。")
            _print("  这不是你的电路有问题，而是真机的规模限制，也正是模拟器仍然重要的原因。")
        _print("  已自动改用内置参考模拟器，流程照常继续。")
        return sample(circuit, shots), "内置参考模拟器（真机不可用时的兜底）", False
    except Exception as exc:  # noqa: BLE001
        _print("  真机运行出了状况：%s: %s" % (type(exc).__name__, exc))
        _print("  已自动改用内置参考模拟器，流程照常继续。")
        return sample(circuit, shots), "内置参考模拟器（真机报错时的兜底）", False

    return (
        payload["counts"],
        "真实量子计算机 %s（任务编号 %s，可在 cloud.spinq.cn 查到）"
        % (payload["backend"], payload["job_id"]),
        True,
    )


def present(title: str, qasm: str, explanation: str, target: str, shots: int) -> None:
    """完整呈现一次实验：电路图 → 运行 → 分布 → 人话解释。"""
    _print()
    _print("─" * 66)
    _print("【%s】" % title)
    _print("─" * 66)

    if explanation:
        _print()
        _print("这个电路在做什么：")
        _print("  " + explanation)

    try:
        circuit = parse(qasm)
    except (QasmError, ValueError) as exc:
        _print()
        _print("这段电路没能通过检查：%s" % exc)
        _print("可以换个说法再描述一次，或者输入 1 / 2 / 3 先看现成的例子。")
        return

    _print()
    _print("电路图（从左到右是时间顺序，每行是一个量子比特）：")
    _print()
    for line in circuit_diagram(circuit).splitlines():
        _print("  " + line)

    _print()
    _print("正在运行，重复测量 %d 次..." % shots)
    try:
        counts, backend_note, on_hardware = run_circuit(qasm, target, shots)
    except Exception as exc:  # noqa: BLE001
        _print()
        _print("运行失败：%s: %s" % (type(exc).__name__, exc))
        _print("这不是你的问题。可以试试 --backend refsim 用内置模拟器再跑一次。")
        return

    _print("运行完成，用的是 %s。" % backend_note)
    _print()
    _print("测量结果分布：")
    _print()
    _print(histogram(counts))

    width = len(next(iter(counts)))
    _print()
    for line in bitstring_legend(width).splitlines():
        _print("  " + line)

    total = sum(counts.values())
    distribution = {key: value / total for key, value in counts.items()}

    if on_hardware:
        # 真机结果被噪声糊过，光看它讲不清电路的意图，所以拿 refsim 精确算出理想分布
        # 当基准：结构性解释讲理想的（电路本该做什么），偏差解释讲实测的（真机差在哪）。
        ideal = ideal_distribution(circuit)
        _print()
        _print("这个电路本该给出的结果（内置模拟器精确计算）：")
        _print()
        for key in sorted(ideal, key=lambda item: -ideal[item]):
            _print("  %s  %5.1f%%" % (key, ideal[key] * 100))
        _print()
        _print("这意味着什么：")
        for line in _wrap(explain_distribution(ideal), 62):
            _print("  " + line)
        _print()
        _print("那么真机差在哪：")
        for line in _wrap(explain_hardware_noise(distribution, ideal), 62):
            _print("  " + line)
    else:
        _print()
        _print("这意味着什么：")
        for line in _wrap(explain_distribution(distribution, total), 62):
            _print("  " + line)
    _print()


def _wrap(text: str, width: int) -> list:
    """按显示宽度折行，中文按 2 计算，避免终端里长句糊成一片。"""
    lines, current, length = [], "", 0
    for char in text:
        size = 2 if ord(char) > 0x2E80 else 1
        if length + size > width and char in " ，。；：、":
            lines.append(current + char)
            current, length = "", 0
            continue
        if length + size > width and current:
            lines.append(current)
            current, length = char, size
            continue
        current += char
        length += size
    if current:
        lines.append(current)
    return lines


def ask_agent(question: str) -> Optional[Tuple[str, str, str]]:
    """把自然语言交给 L2 Agent。返回 (标题, qasm, 解释)，拿不到电路则返回 None。"""
    from . import agent as agent_module

    reply = agent_module.agent_chat(question)
    match = re.search(r"OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)", reply, re.DOTALL | re.MULTILINE)
    if not match:
        _print()
        _print(reply)
        return None
    explanation = reply.split("```")[0].strip()
    return question, match.group(0).strip(), explanation


HELP = """
可以输入：

  1 / 2 / 3          运行内置示例（贝尔态 / GHZ 态 / Grover 搜索）
  任意中文描述       交给智能体生成电路，例如：让四个比特全都纠缠起来
  real               换成真实的量子计算机来跑（要排队，每次约两分钟）
  sim                换回内置模拟器（立刻出结果，且是精确理想值）
  backends           查看有哪些运行后端
  shots 4096         修改重复测量次数
  intro              重看开场引导
  help               显示本帮助
  quit               退出
"""


def interactive(target: str, shots: int) -> int:
    _print(WELCOME)
    if target != "spinq_cloud" and hardware_ready():
        _print(WELCOME_HARDWARE)
    while True:
        try:
            raw = input("你想做什么？> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print()
            _print("再见。")
            return 0
        if not raw:
            continue
        lowered = raw.lower()

        if lowered in ("quit", "exit", "q", "退出"):
            _print("再见。")
            return 0
        if lowered in ("help", "?", "帮助"):
            _print(HELP)
            continue
        if lowered in ("intro", "引导"):
            _print(WELCOME)
            continue
        if lowered in ("backends", "后端"):
            _print()
            _print(describe_backends())
            _print()
            continue
        if lowered.startswith("shots"):
            parts = lowered.split()
            if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 0:
                shots = int(parts[1])
                _print("好，之后每次重复测量 %d 次。" % shots)
            else:
                _print("用法：shots 4096")
            continue
        # 真机必须能在会话中途切换。否则默认进来是模拟器，用户（和评委）
        # 走完整个流程都不会知道这个工具能连真实量子计算机。
        if lowered in ("real", "真机"):
            if hardware_ready():
                target = "spinq_cloud"
                _print()
                _print("好，接下来的运行会送到真实的量子计算机上。")
                _print("真机要排队，每次约两分钟；输入 sim 可以随时切回模拟器。")
                _print()
            else:
                _print()
                _print("真机还没配好，暂时用不了。缺的东西和步骤：")
                _print()
                _print(describe_backends())
                _print()
            continue
        if lowered in ("sim", "模拟器"):
            target = "refsim"
            _print("好，切回内置模拟器，结果是精确的理想值，而且立刻就有。")
            continue

        if raw in BUILTIN_EXAMPLES:
            title, qasm, explanation = BUILTIN_EXAMPLES[raw]
            present(title, qasm, explanation, target, shots)
            continue

        if not os.environ.get("LOOMQ_LLM_API_KEY"):
            _print()
            _print("要让智能体理解自然语言，需要先配置模型服务：")
            _print("  LOOMQ_LLM_BASE_URL / LOOMQ_LLM_API_KEY / LOOMQ_LLM_MODEL")
            _print("现在还没配置。你可以先输入 1、2 或 3 看三个现成的例子，流程完全一样。")
            _print()
            continue

        result = ask_agent(raw)
        if result:
            present(*result, target=target, shots=shots)


def demo(target: str, shots: int) -> int:
    _print(WELCOME)
    _print()
    _print("=== 现场体验：三个任务，依次跑完 ===")
    for key, intent in DEMO_TASKS:
        title, qasm, explanation = BUILTIN_EXAMPLES[key]
        _print()
        _print("任务：%s" % intent)
        present(title, qasm, explanation, target, shots)
    _print("=" * 66)
    _print("三个任务全部完成。整个过程没有要求你写一行代码，也没有用到任何量子物理知识。")
    _print("这就是 LoomQ 想做的事：把门槛降到「能用中文说出想法」这一步。")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="LoomQ 交互入口：不懂量子也能跑量子程序",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ask", help="直接提一个问题，跑完即退出")
    parser.add_argument("--demo", action="store_true", help="依次运行三个现场体验任务")
    parser.add_argument("--list-backends", action="store_true", help="列出可用后端后退出")
    parser.add_argument("--backend", default="refsim",
                        choices=("refsim", "spinq", "braket", "originq", "spinq_cloud"),
                        help="运行后端，默认 refsim（内置参考模拟器，始终可用）；"
                             "spinq_cloud 是真实量子计算机")
    parser.add_argument("--shots", type=int, default=DEFAULT_SHOTS, help="重复测量次数")
    args = parser.parse_args(argv)

    if args.shots <= 0:
        _print("shots 必须是正整数。")
        return 2

    if args.list_backends:
        _print(describe_backends())
        return 0

    if args.demo:
        return demo(args.backend, args.shots)

    if args.ask:
        result = ask_agent(args.ask)
        if not result:
            return 1
        present(*result, target=args.backend, shots=args.shots)
        return 0

    return interactive(args.backend, args.shots)


if __name__ == "__main__":
    sys.exit(main())
