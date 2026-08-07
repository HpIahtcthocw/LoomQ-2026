#!/usr/bin/env python3
"""L2 实战压测：用真实模型跑一批自造的 prompt 变体，估算正式评测的通过率。

为什么需要这个脚本：官方 `evaluator.py --level l2` 只有 1 个公开用例，而且只校验
"回复里能抽出可解析的 QASM"，不校验电路对不对。正式评测是 2 个私有种子共 12 个 case，
用未公开的同类变体，按 通过率 × 20 计分。更要紧的是 10 分的交互体验分只在客观分 ≥ 12
时才计入，也就是 12 个 case 至少要过 8 个。所以必须自己造变体来估通过率。

判定口径尽量对齐题面：
  - 意图生成 / 代码纠错：用官方 evaluator 的同一个正则抽 QASM，解析后由 refsim 无噪声
    模拟，与目标分布比 Hellinger 保真度，阈值 0.97。
  - 智能选后端：回复须包含规范后端标识。答案集不写死，由 backend_capabilities.json
    按约束算出，避免"我自己定的标准答案"这种自证。

选后端有一处题面留下的歧义：判定是"包含正确标识即可"（宽松），还是"不得出现答案集之外
的标识"（严格）。两种都统计，取严格口径作为决策依据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "starter_kit")
for path in (KIT, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from loomq.qasm2_parser import parse  # noqa: E402
from loomq.refsim import hellinger_fidelity, ideal_distribution  # noqa: E402
from loomq.agent import load_capabilities, select_backends  # noqa: E402

import adapter  # noqa: E402

FIDELITY_THRESHOLD = 0.97
QASM_RE = re.compile(r"OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)", re.DOTALL | re.MULTILINE)


def formal_model() -> str:
    with open(os.path.join(KIT, "l2_policy.json"), encoding="utf-8") as handle:
        return json.load(handle)["formal_model"]


def extract_qasm(text: str) -> Optional[str]:
    """与官方 evaluator.extract_qasm 完全一致的抽取逻辑。"""
    if not isinstance(text, str):
        return None
    match = QASM_RE.search(text)
    return match.group(0).strip() if match else None


# --- 目标分布 ---------------------------------------------------------------


def bell() -> Dict[str, float]:
    return {"00": 0.5, "11": 0.5}


def anti_bell() -> Dict[str, float]:
    return {"01": 0.5, "10": 0.5}


def ghz(n: int) -> Dict[str, float]:
    return {"0" * n: 0.5, "1" * n: 0.5}


def uniform(n: int) -> Dict[str, float]:
    weight = 1.0 / (2**n)
    return {format(i, "0%db" % n): weight for i in range(2**n)}


def single_basis(bits: str) -> Dict[str, float]:
    return {bits: 1.0}


# --- 用例 -------------------------------------------------------------------


class Case:
    def __init__(
        self,
        name: str,
        category: str,
        prompt: str,
        grader: Callable[[str], "Verdict"],
        stretch: bool = False,
    ) -> None:
        self.name = name
        self.category = category
        self.prompt = prompt
        self.grader = grader
        # stretch=True 的用例比题面示例更难，用来找边界，不计入对正式通过率的估计。
        self.stretch = stretch


class Verdict:
    def __init__(self, ok: bool, detail: str, strict_ok: Optional[bool] = None) -> None:
        self.ok = ok
        self.detail = detail
        self.strict_ok = ok if strict_ok is None else strict_ok


def grade_circuit(expected: Dict[str, float], n_qubits: int) -> Callable[[str], Verdict]:
    def grader(reply: str) -> Verdict:
        qasm = extract_qasm(reply)
        if not qasm:
            return Verdict(False, "回复里抽不出 OpenQASM 2.0 程序")
        try:
            circuit = parse(qasm)
        except Exception as exc:  # noqa: BLE001
            return Verdict(False, "QASM 无法解析：%s: %s" % (type(exc).__name__, exc))
        if not circuit.measures():
            return Verdict(False, "电路没有 measure 语句")
        actual = ideal_distribution(circuit)
        width = len(next(iter(actual)))
        if width != n_qubits:
            return Verdict(
                False,
                "测量位宽是 %d，但这个任务要求 %d 个比特（分布无法比较）" % (width, n_qubits),
            )
        fidelity = hellinger_fidelity(actual, expected)
        if fidelity >= FIDELITY_THRESHOLD:
            return Verdict(True, "保真度 %.4f" % fidelity)
        top = sorted(actual.items(), key=lambda kv: -kv[1])[:4]
        return Verdict(
            False,
            "保真度 %.4f < %.2f；期望主峰 %s，实际主峰 %s"
            % (
                fidelity,
                FIDELITY_THRESHOLD,
                sorted(expected, key=lambda k: -expected[k])[:4],
                [k for k, _ in top],
            ),
        )

    return grader


def grade_backend(**constraints: Any) -> Callable[[str], Verdict]:
    """答案集由能力表按约束算出，不写死。

    宽松口径：回复含答案集内任一标识即通过。
    严格口径：额外要求回复不出现答案集之外的后端标识。
    """
    matched, all_backends = select_backends(**constraints)
    accepted = {backend["id"] for backend in matched}
    every = {backend["id"] for backend in all_backends}

    def grader(reply: str) -> Verdict:
        mentioned = {bid for bid in every if bid in reply}
        hit = mentioned & accepted
        stray = mentioned - accepted
        if not accepted:
            # 无解用例：正确行为是明确说没有后端满足，而不是硬报一个
            says_none = any(
                token in reply for token in ("没有任何后端", "没有后端", "无法同时满足", "超出")
            )
            if says_none and not hit:
                return Verdict(True, "正确指出无解" + ("（另提到替代 %s）" % sorted(stray) if stray else ""))
            return Verdict(False, "应指出无解，但回复未明确说明（提到 %s）" % sorted(mentioned))
        if not hit:
            return Verdict(False, "未命中答案集 %s（回复提到 %s）" % (sorted(accepted), sorted(mentioned)))
        detail = "命中 %s" % sorted(hit)
        if stray:
            return Verdict(True, detail + "，但同时提到集外标识 %s" % sorted(stray), strict_ok=False)
        return Verdict(True, detail)

    return grader


def build_cases() -> List[Case]:
    cases: List[Case] = []

    # --- 一、意图生成：换措辞、换比特数、换目标态 ---------------------------
    generate = [
        ("gen-ghz3-formal", "生成一个 3 比特的最大纠缠态 (GHZ 态)，并进行全测量", ghz(3), 3),
        ("gen-ghz3-casual", "帮我写个三个量子比特一起纠缠在一起的电路，最后都测一下", ghz(3), 3),
        ("gen-ghz4", "我要一个 4 比特 GHZ 态，测量全部比特", ghz(4), 4),
        ("gen-ghz5-verbose", "请构造五量子比特的 GHZ 纠缠态电路，并对所有比特做测量，输出 OpenQASM 2.0", ghz(5), 5),
        ("gen-bell-plain", "生成一个贝尔态并测量两个比特", bell(), 2),
        ("gen-bell-desc", "我想要两个比特，测出来要么都是 0 要么都是 1，各一半概率", bell(), 2),
        ("gen-antibell", "做一个两比特电路，测量结果只可能是 01 或 10，各占一半", anti_bell(), 2),
        ("gen-uniform3", "生成一个 3 比特的均匀叠加态，所有 8 种结果等概率出现，并全部测量", uniform(3), 3),
        ("gen-uniform4-casual", "我想让四个量子比特的每种组合都等可能出现，写个电路测一下", uniform(4), 4),
        ("gen-basis-101", "写一个 3 比特电路，让测量结果确定性地等于 101", single_basis("101"), 3),
    ]
    for name, prompt, expected, n in generate:
        cases.append(Case(name, "generate", prompt, grade_circuit(expected, n)))

    # --- 二、代码纠错：prompt 明确声明目标态 -------------------------------
    repair = [
        (
            "rep-bell-official",
            "我想制备一个贝尔态，但这段代码报错了，帮我修好：\nH q[0]; CX q[0] q[1]",
            bell(),
            2,
        ),
        (
            "rep-bell-nocreg",
            "下面这段想做贝尔态，但跑不起来，请修好：\n"
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[2];\nh q[0];\ncx q[0],q[1];\n",
            bell(),
            2,
        ),
        (
            "rep-ghz3-missing-cx",
            "我的目标是 3 比特 GHZ 态，但这段代码结果不对，帮我改：\n"
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[3];\ncreg c[3];\n"
            "h q[0];\ncx q[0],q[1];\nmeasure q -> c;\n",
            ghz(3),
            3,
        ),
        (
            "rep-ghz4-wrong-gate",
            "这段代码本来要做 4 比特 GHZ 态，但我把门写错了，请在保持目标不变的前提下修好：\n"
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[4];\ncreg c[4];\n"
            "h q[0];\nswap q[0],q[1];\nswap q[1],q[2];\nswap q[2],q[3];\nmeasure q -> c;\n",
            ghz(4),
            4,
        ),
        (
            "rep-uniform3-case",
            "我要的是三个比特的均匀叠加，这段大小写全错而且缺声明，帮我修：\nHADAMARD Q[0]; HADAMARD Q[1]; HADAMARD Q[2]",
            uniform(3),
            3,
        ),
        (
            "rep-bell-nomeasure",
            "目标是贝尔态。这段代码能编译但我拿不到任何结果，帮我补全：\n"
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[2];\ncreg c[2];\nh q[0];\ncx q[0],q[1];\n",
            bell(),
            2,
        ),
    ]
    for name, prompt, expected, n in repair:
        cases.append(Case(name, "repair", prompt, grade_circuit(expected, n)))

    # --- 三、智能选后端：约束组合，答案集由能力表算出 -----------------------
    backend = [
        (
            "bk-15q-noqueue",
            "我需要运行一个 15 比特电路，且零排队等待，选哪个平台？",
            dict(min_qubits=15, require_no_queue=True),
        ),
        (
            "bk-20q-free-local",
            "有个 20 比特的电路想跑，不想花钱也不想排队，用什么后端？",
            dict(min_qubits=20, require_no_queue=True, forbid_paid=True),
        ),
        (
            "bk-28q-noqueue-free",
            "28 比特的电路，要求立刻能跑、完全免费，推荐哪个？",
            dict(min_qubits=28, require_no_queue=True, forbid_paid=True),
        ),
        (
            "bk-real-hardware-5q",
            "我想在真实的量子硬件上跑一个 5 比特电路，选哪个？",
            dict(min_qubits=5, kind="qpu"),
        ),
        (
            "bk-50q",
            "我要跑一个 50 比特的电路，选哪个平台？",
            dict(min_qubits=50),
        ),
        (
            "bk-100q-impossible",
            "我需要一个能跑 100 比特电路的后端，有吗？",
            dict(min_qubits=100),
        ),
        (
            "bk-32q-noaccount",
            "32 比特电路，我不想注册任何账号，有办法吗？",
            dict(min_qubits=32, allow_account=False),
        ),
        (
            "bk-simulator-10q",
            "给我一个能跑 10 比特的本地模拟器就行，免费的",
            dict(min_qubits=10, kind="simulator", forbid_paid=True),
        ),
    ]
    for name, prompt, constraints in backend:
        cases.append(Case(name, "select_backend", prompt, grade_backend(**constraints)))

    # --- 进阶组：比题面示例更难，用来找边界，不计入正式通过率估计 -----------
    stretch_generate = [
        ("xt-ghz6", "生成 6 比特 GHZ 态并全部测量", ghz(6), 6),
        ("xt-ghz7", "我要七个量子比特的最大纠缠态，全测量", ghz(7), 7),
        # 位序陷阱：'第一个比特'是 q[0]，对应位串最右一位
        ("xt-first-fixed", "两个比特，第一个比特测出来永远是 1，第二个比特五五开",
         {"01": 0.5, "11": 0.5}, 2),
        ("xt-custom-000-011", "3 比特电路，测量结果只可能是 000 或 011，各占一半",
         {"000": 0.5, "011": 0.5}, 3),
        ("xt-partial-uniform", "3 个比特，让前两个处于均匀叠加，第三个保持 0，全部测量",
         {"000": 0.25, "001": 0.25, "010": 0.25, "011": 0.25}, 3),
        ("xt-w-state", "生成 3 比特 W 态（001、010、100 各三分之一）并全部测量",
         {"001": 1 / 3, "010": 1 / 3, "100": 1 / 3}, 3),
        ("xt-basis-1010", "写一个 4 比特电路，测量结果确定性地等于 1010",
         single_basis("1010"), 4),
    ]
    for name, prompt, expected, n in stretch_generate:
        cases.append(Case(name, "generate", prompt, grade_circuit(expected, n), stretch=True))

    stretch_repair = [
        (
            "xt-rep-nonwhitelist",
            "我的目标是贝尔态，但这段代码用了不支持的门，请只用 h/x/s/t/rz/ry/cx/swap/ccx 改写：\n"
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[2];\ncreg c[2];\n"
            "u3(1.5707963,0,3.1415926) q[0];\ncx q[0],q[1];\nmeasure q -> c;\n",
            bell(),
            2,
        ),
        (
            "xt-rep-bitorder",
            "我要的分布是 001 和 110 各一半（最右边是 c[0]）。这段代码测量连线错了，帮我修：\n"
            "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[3];\ncreg c[3];\n"
            "h q[0];\ncx q[0],q[1];\nx q[0];\n"
            "measure q[0] -> c[2];\nmeasure q[1] -> c[1];\nmeasure q[2] -> c[0];\n",
            {"001": 0.5, "110": 0.5},
            3,
        ),
    ]
    for name, prompt, expected, n in stretch_repair:
        cases.append(Case(name, "repair", prompt, grade_circuit(expected, n), stretch=True))

    stretch_backend = [
        ("xt-bk-8q-qpu-free", "我想用免费额度在真机上跑 8 比特电路，选哪个？",
         dict(min_qubits=8, kind="qpu", forbid_paid=True)),
        ("xt-bk-34q", "有没有能跑 34 比特的后端？预算不是问题", dict(min_qubits=34)),
        ("xt-bk-24q-noqueue", "24 比特，要求零排队", dict(min_qubits=24, require_no_queue=True)),
    ]
    for name, prompt, constraints in stretch_backend:
        cases.append(Case(name, "select_backend", prompt, grade_backend(**constraints), stretch=True))

    return cases


# --- 执行 -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="L2 live pass-rate estimator")
    parser.add_argument("--only", help="只跑名字包含该子串的用例")
    parser.add_argument("--category", choices=("generate", "repair", "select_backend"))
    parser.add_argument("--repeat", type=int, default=1, help="每个用例重复次数，用于看稳定性")
    parser.add_argument("--stretch", action="store_true", help="同时跑进阶组（更难，用于找边界）")
    args = parser.parse_args()

    missing = [
        name
        for name in ("LOOMQ_LLM_BASE_URL", "LOOMQ_LLM_API_KEY", "LOOMQ_LLM_MODEL")
        if not os.environ.get(name)
    ]
    if missing:
        print("缺少环境变量：" + ", ".join(missing))
        return 2

    model = os.environ["LOOMQ_LLM_MODEL"]
    formal = formal_model()
    print("模型 = %s（l2_policy 正式模型 = %s）" % (model, formal))
    if model != formal:
        print("提醒：当前模型与正式评分模型不同，且 llm_client 只在模型名恰为 %s 时才关闭 thinking，" % formal)
        print("      结果不能代表正式环境。建议 LOOMQ_LLM_MODEL=%s 重跑。" % formal)
    print()

    cases = build_cases()
    if not args.stretch and not args.only:
        cases = [case for case in cases if not case.stretch]
    if args.only:
        cases = [case for case in cases if args.only in case.name]
    if args.category:
        cases = [case for case in cases if case.category == args.category]
    if not cases:
        print("没有匹配的用例")
        return 2

    rows: List[Dict[str, Any]] = []
    for case in cases:
        for run in range(1, args.repeat + 1):
            tag = case.name if args.repeat == 1 else "%s#%d" % (case.name, run)
            started = time.time()
            try:
                reply = adapter.agent_chat(case.prompt)
                verdict = case.grader(reply)
            except Exception as exc:  # noqa: BLE001
                reply = ""
                verdict = Verdict(False, "抛异常：%s: %s" % (type(exc).__name__, exc))
            elapsed = time.time() - started
            mark = "PASS" if verdict.ok else "FAIL"
            if verdict.ok and not verdict.strict_ok:
                mark = "PASS*"
            print("[%-5s] %-24s %5.1fs  %s" % (mark, tag, elapsed, verdict.detail))
            rows.append(
                {
                    "name": tag,
                    "category": case.category,
                    "stretch": case.stretch,
                    "ok": verdict.ok,
                    "strict_ok": verdict.strict_ok,
                    "detail": verdict.detail,
                    "elapsed": elapsed,
                    "reply": reply,
                }
            )

    print()
    print("=" * 78)
    core = [row for row in rows if not row["stretch"]]
    stretch = [row for row in rows if row["stretch"]]
    for label, subset in (("对标题面示例", core), ("进阶组（更难）", stretch)):
        if not subset:
            continue
        print("%s：" % label)
        for category in ("generate", "repair", "select_backend"):
            group = [row for row in subset if row["category"] == category]
            if group:
                print(
                    "  %-16s %2d/%2d 通过（严格 %2d/%2d）"
                    % (
                        category,
                        sum(1 for row in group if row["ok"]),
                        len(group),
                        sum(1 for row in group if row["strict_ok"]),
                        len(group),
                    )
                )

    print("-" * 78)
    if not core:
        print("本次没有跑对标组，无法折算客观分")
        return 0 if all(row["strict_ok"] for row in rows) else 1

    strict = sum(1 for row in core if row["strict_ok"])
    lenient = sum(1 for row in core if row["ok"])
    total = len(core)
    print("对标组总计 %d/%d = %.1f%%（宽松口径 %d/%d）"
          % (strict, total, 100.0 * strict / total, lenient, total))
    rate = 1.0 * strict / total
    print("按严格口径折算客观分 ≈ %.1f / 20（正式评测 12 个 case，需 ≥ 8 个通过才够 12 分）"
          % (rate * 20))
    if rate * 20 >= 12:
        print("→ 达到 12 分门槛，10 分交互体验分可以计入")
    else:
        print("→ 未达 12 分门槛，10 分交互体验分会被作废，必须先把这里修上去")
    if stretch:
        strict_x = sum(1 for row in stretch if row["strict_ok"])
        print("进阶组 %d/%d 通过——只用于找边界，不参与折算" % (strict_x, len(stretch)))

    failures = [row for row in rows if not row["ok"]]
    if failures:
        print()
        print("失败用例明细：")
        for row in failures:
            print("  - %s%s：%s"
                  % (row["name"], "（进阶）" if row["stretch"] else "", row["detail"]))

    if os.environ.get("LOOMQ_DUMP_REPLIES"):
        print()
        print("=" * 78)
        for row in rows:
            if not row["ok"] or os.environ.get("LOOMQ_DUMP_REPLIES") == "all":
                print("--- %s ---" % row["name"])
                print(row["reply"][:2000])
                print()

    return 0 if strict == total else 1


if __name__ == "__main__":  # noqa: E305
    sys.exit(main())
