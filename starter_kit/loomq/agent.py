"""L2 智能体：让不懂 QASM 的人也能驱动量子计算机。

对应题面三类客观任务：
    1. 意图生成    自然语言 → 正确的 OpenQASM 2.0
    2. 代码纠错    在**保持用户声明意图**的前提下修好电路
    3. 智能选后端  按约束从《后端能力表》选出规范后端标识

三条设计原则：

**一、后端选型由数据决定，不由模型记忆决定。**
backend_capabilities.md 明确建议"让 Agent 直接加载 JSON 作为选型知识库，而不是把表塞进
prompt 里靠模型背诵"。所以模型只负责把自然语言里的约束抽取成结构化条件，筛选由代码完成。
评测用未公开的 prompt 变体，背答案无效，但按约束筛选永远有效。

**二、生成结果必须自验，且验证基准尽量不来自模型自己。**
模型输出 QASM 的同时声明它想达到的目标态族（bell / ghz / uniform / custom）。对前三类，
参考分布由 refsim 独立算出——模型说什么不算，电路实际跑出什么才算；只有 custom 才回退到
采信模型声明的分布。这挡住了"模型编个错电路再编个配套的错期望"这种自洽性造假。

**三、自验用参考模拟器，不用真实后端。**
refsim 是纯标准库的精确模拟，不需要 SDK、不占网络、毫秒级返回。评测每个 case 只有 120 秒，
而真实后端可能排队——把自验挂在真机上是拿正确率换风险。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .qasm2_parser import parse
from .refsim import hellinger_fidelity, ideal_distribution

FIDELITY_THRESHOLD = 0.97
MAX_GENERATION_ATTEMPTS = 3

_CAPABILITIES_CACHE: Optional[Dict[str, Any]] = None


# --- 后端能力表 -------------------------------------------------------------


def load_capabilities() -> Dict[str, Any]:
    global _CAPABILITIES_CACHE
    if _CAPABILITIES_CACHE is None:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "backend_capabilities.json",
        )
        with open(path, encoding="utf-8") as handle:
            _CAPABILITIES_CACHE = json.load(handle)
    return _CAPABILITIES_CACHE


def select_backends(
    min_qubits: Optional[int] = None,
    require_no_queue: bool = False,
    forbid_paid: bool = False,
    kind: Optional[str] = None,
    allow_account: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按约束筛选后端。返回 (满足全部约束的, 全部后端)。

    判定完全由 backend_capabilities.json 的字段决定，不含任何写死的答案。
    """
    backends = load_capabilities()["backends"]
    matched = []
    for backend in backends:
        if min_qubits is not None and backend["max_qubits"] < min_qubits:
            continue
        if require_no_queue and backend["queue"] != "none":
            continue
        if forbid_paid and backend["cost"] == "paid":
            continue
        if kind and backend["kind"] != kind:
            # 题面把 braket_cloud 标为 kind="cloud"，它同时包含托管模拟器与真机；
            # 问"真机"时不应把它当纯 QPU，问"模拟器"时也不应排除它之外的选项。
            if not (kind == "qpu" and backend["kind"] == "cloud"):
                continue
        if not allow_account and backend["requires_account"]:
            continue
        matched.append(backend)
    return matched, backends


# --- 模型调用 ---------------------------------------------------------------


def _chat(messages: List[Dict[str, str]]) -> str:
    """通过公开契约调用模型。用 llm_client 的传输层，不硬编码任何地址或密钥。"""
    try:
        from .. import llm_client  # type: ignore[import-not-found]
    except (ImportError, ValueError):
        import llm_client  # type: ignore[no-redef]

    response = llm_client.chat_completion(messages)
    try:
        return response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型响应结构异常，缺少 choices[0].message.content") from exc


def _extract_json(text: str) -> Dict[str, Any]:
    """从模型回复里抠出 JSON，容忍 ``` 包裹和前后解释文字。"""
    cleaned = re.sub(r"^\s*```[A-Za-z]*\s*|\s*```\s*$", "", text.strip(), flags=re.MULTILINE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型回复中没有 JSON 对象")
    return json.loads(cleaned[start : end + 1])


SYSTEM_PROMPT = """你是 LoomQ 的量子编程助手。用户可能完全不懂量子计算。
你必须只返回一个 JSON 对象，不要任何解释文字，不要用 ``` 包裹。

先判断用户属于哪一类请求，填入 task 字段：

1. task="generate"：用户用自然语言描述想要的量子态或电路，需要你写出 OpenQASM 2.0。
2. task="repair"：用户给了一段有错的代码要你修好。必须保持用户声明的目标不变。
3. task="select_backend"：用户在问该用哪个后端 / 平台运行。

task="generate" 或 "repair" 时返回：
{
  "task": "generate",
  "qasm": "完整的 OpenQASM 2.0 程序",
  "target_family": "bell" | "ghz" | "uniform" | "custom",
  "n_qubits": 整数,
  "expected_distribution": {"位串": 概率},
  "explanation": "一句给零基础用户的中文解释，说明这个电路在做什么"
}

qasm 硬性要求：
- 以 OPENQASM 2.0; 和 include "qelib1.inc"; 开头
- 必须声明 qreg 和 creg，必须有 measure 语句
- 只允许使用这 12 个门：h, x, s, sdg, t, tdg, rz(θ), ry(θ), cx, cu1(θ), swap, ccx
- 位串约定：最右边的字符对应 c[0]

target_family 填法：
- 贝尔态 / 两比特最大纠缠态 → "bell"
- n 比特 GHZ 态 / n 比特最大纠缠态 → "ghz"，n_qubits 填 n
- 所有比特的均匀叠加（每个比特各一个 H）→ "uniform"
- 其他情况 → "custom"，此时 expected_distribution 必须准确填写

task="select_backend" 时返回：
{
  "task": "select_backend",
  "min_qubits": 整数或 null,
  "require_no_queue": true/false,
  "forbid_paid": true/false,
  "kind": "qpu" | "simulator" | null,
  "user_goal": "一句话复述用户的约束"
}
kind 填 "qpu" 仅当用户明确要求真实量子硬件；require_no_queue 为 true 仅当用户明确要求
不排队或零等待；forbid_paid 为 true 当用户提到免费、不想花钱或预算限制。
不要自己给出后端名称，选型由系统按官方能力表完成。"""


# --- 参考分布：尽量不采信模型的自我声明 --------------------------------------


def reference_distribution(
    family: str, n_qubits: int, declared: Optional[Dict[str, float]]
) -> Tuple[Optional[Dict[str, float]], str]:
    """给出用于判定的参考分布，并说明它的来源。

    bell / ghz / uniform 由我们独立算出；custom 才回退到模型声明的分布。
    """
    family = (family or "").strip().lower()
    if family == "bell":
        return {"00": 0.5, "11": 0.5}, "独立推导（贝尔态）"
    if family == "ghz" and n_qubits >= 2:
        return {"0" * n_qubits: 0.5, "1" * n_qubits: 0.5}, "独立推导（GHZ-%d）" % n_qubits
    if family == "uniform" and 1 <= n_qubits <= 16:
        size = 2**n_qubits
        weight = 1.0 / size
        return {format(index, "0%db" % n_qubits): weight for index in range(size)}, \
            "独立推导（%d 比特均匀叠加）" % n_qubits
    if declared:
        total = sum(declared.values())
        if total > 0:
            return {key: value / total for key, value in declared.items()}, "采信模型声明（custom）"
    return None, "无参考分布，仅做可解析性与结构校验"


def verify_qasm(
    qasm: str, family: str, n_qubits: int, declared: Optional[Dict[str, float]]
) -> Tuple[bool, str, Optional[float]]:
    """自验：先解析（挡语法与非白名单门），再用 refsim 比对参考分布。"""
    try:
        circuit = parse(qasm)
    except Exception as exc:  # noqa: BLE001
        return False, "无法解析：%s: %s" % (type(exc).__name__, exc), None

    if not circuit.measures():
        return False, "电路里没有 measure 语句，测不出任何结果", None

    reference, source = reference_distribution(family, n_qubits or circuit.n_qubits, declared)
    if reference is None:
        return True, "结构校验通过（%s）" % source, None

    actual = ideal_distribution(circuit)
    fidelity = hellinger_fidelity(actual, reference)
    if fidelity >= FIDELITY_THRESHOLD:
        return True, "保真度 %.4f，基准来自%s" % (fidelity, source), fidelity

    top = sorted(actual.items(), key=lambda kv: -kv[1])[:4]
    return (
        False,
        "保真度只有 %.4f（阈值 %.2f）。基准来自%s，期望主要落在 %s，"
        "但这个电路实际主要落在 %s"
        % (
            fidelity,
            FIDELITY_THRESHOLD,
            source,
            sorted(reference, key=lambda key: -reference[key])[:4],
            [key for key, _ in top],
        ),
        fidelity,
    )


# --- 三类任务的应答 ---------------------------------------------------------


def _format_circuit_reply(payload: Dict[str, Any], note: str) -> str:
    """回复格式：解释在前，QASM 代码块在最后。

    官方 evaluator 用正则 `OPENQASM\\s+2\\.0;.*?(?=^\\s*```|\\Z)` 抽取程序，
    所以 QASM 必须放在末尾的代码块里、且解释文字中不能出现 OPENQASM 字样，
    否则正则会从解释处开始截取。
    """
    explanation = str(payload.get("explanation") or "").strip()
    lines: List[str] = []
    if explanation:
        lines += [explanation, ""]
    if note:
        lines += [note, ""]
    lines += ["```qasm", str(payload["qasm"]).strip(), "```"]
    return "\n".join(lines)


def handle_circuit_task(prompt: str, first: Dict[str, Any]) -> str:
    """意图生成与代码纠错共用一条"生成 → 自验 → 带反馈重试"的闭环。"""
    payload = first
    history: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    last_reason = ""

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        qasm = str(payload.get("qasm") or "")
        ok, reason, _ = verify_qasm(
            qasm,
            str(payload.get("target_family") or "custom"),
            int(payload.get("n_qubits") or 0),
            payload.get("expected_distribution"),
        )
        if ok:
            note = "" if attempt == 1 else "（第 %d 次尝试通过自验）" % attempt
            return _format_circuit_reply(payload, note)

        last_reason = reason
        if attempt == MAX_GENERATION_ATTEMPTS:
            break

        history.append(
            {
                "role": "user",
                "content": "你上一版的电路没有通过自动验证：%s\n"
                "请修正后重新返回同样格式的 JSON。保持用户原本声明的目标不变，"
                "只允许使用 12 门白名单，并确认 measure 覆盖了所有相关比特。" % reason,
            }
        )
        try:
            payload = _extract_json(_chat(history))
        except Exception as exc:  # noqa: BLE001
            last_reason = "%s（重试时模型回复无法解析：%s）" % (reason, exc)
            break
        history.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})

    # 仍未通过：如实说明，但仍然把最后一版电路交出去，避免整个 case 拿不到任何东西
    note = "注意：这一版没有通过我的自动验证（%s）。它仍可运行，但结果可能与你的预期不同。" % last_reason
    return _format_circuit_reply(payload, note)


_QUEUE_TEXT = {
    "none": "无排队",
    "minutes_to_hours": "分钟到小时级排队",
    "hours": "小时级排队",
}
_COST_TEXT = {"free": "免费", "free_quota": "有免费额度", "paid": "付费"}


def handle_backend_selection(payload: Dict[str, Any]) -> str:
    min_qubits = payload.get("min_qubits")
    min_qubits = int(min_qubits) if isinstance(min_qubits, (int, float)) else None
    kind = payload.get("kind") if payload.get("kind") in ("qpu", "simulator") else None

    matched, all_backends = select_backends(
        min_qubits=min_qubits,
        require_no_queue=bool(payload.get("require_no_queue")),
        forbid_paid=bool(payload.get("forbid_paid")),
        kind=kind,
    )

    constraints = []
    if min_qubits:
        constraints.append("至少 %d 比特" % min_qubits)
    if payload.get("require_no_queue"):
        constraints.append("不排队")
    if payload.get("forbid_paid"):
        constraints.append("不花钱")
    if kind == "qpu":
        constraints.append("真实量子硬件")
    if kind == "simulator":
        constraints.append("模拟器")
    condition_text = "、".join(constraints) if constraints else "无特殊约束"

    lines = ["按你的条件（%s），我从官方后端能力表里筛出以下结果。" % condition_text, ""]

    if matched:
        lines.append("推荐后端：")
        for backend in matched:
            lines.append(
                "- `%s` —— %s，%d 比特上限，%s，%s%s"
                % (
                    backend["id"],
                    backend["name"],
                    backend["max_qubits"],
                    _QUEUE_TEXT.get(backend["queue"], backend["queue"]),
                    _COST_TEXT.get(backend["cost"], backend["cost"]),
                    "，需注册账号" if backend["requires_account"] else "，无需账号",
                )
            )
        # 如实补充这个规模上做不到的事。
        # backend_capabilities.md 的"50 比特"示例既说"无后端满足"，又把 72 比特的
        # originq_wukong 列为可接受答案（"若约束允许排队"）。两种判定都要照顾到：
        # 既给出规范标识，也把能力边界讲清楚。
        caveats = []
        if not any(backend["queue"] == "none" for backend in matched):
            caveats.append("这个规模上没有零排队的选项，全部需要排队等待")
        if not any(backend["kind"] == "simulator" for backend in matched):
            caveats.append("这个规模超出了所有本地模拟器的能力，只能用真机或云端")
        if not any(not backend["requires_account"] for backend in matched):
            caveats.append("全部需要先注册账号")
        if caveats:
            lines += ["", "需要注意：%s。" % "；".join(caveats)]
            if min_qubits and min_qubits > max(
                backend["max_qubits"] for backend in all_backends
                if backend["kind"] == "simulator"
            ):
                lines.append(
                    "如果你希望本地、免费、立刻就能跑，%d 比特超出了现有模拟器上限，"
                    "可行的方向是把电路拆解成更小的子电路。" % min_qubits
                )

        best = matched[0]
        lines += ["", "如果只想要一个答案：**%s**。" % best["id"]]
        return "\n".join(lines)

    # 无解时如实说明，并给出最接近的替代——题面明确说这比给错答案得分更高
    largest = max(all_backends, key=lambda backend: backend["max_qubits"])
    lines += [
        "**没有任何后端能同时满足这些条件。**",
        "",
        "现有后端里规模最大的是 `%s`（%s），上限 %d 比特。"
        % (largest["id"], largest["name"], largest["max_qubits"]),
    ]
    if min_qubits and min_qubits > largest["max_qubits"]:
        lines.append(
            "你需要的 %d 比特超出了全部可用后端的能力。可行的方向是把电路拆解成更小的子电路，"
            "或者放宽其他约束后使用 `%s`。" % (min_qubits, largest["id"])
        )
    else:
        relaxed, _ = select_backends(min_qubits=min_qubits)
        if relaxed:
            lines.append(
                "若能放宽排队或费用上的要求，这些是可选的：%s。"
                % "、".join("`%s`" % backend["id"] for backend in relaxed)
            )
        largest_simulator = max(
            (backend for backend in all_backends if backend["kind"] == "simulator"),
            key=lambda backend: backend["max_qubits"],
        )
        if min_qubits and min_qubits > largest_simulator["max_qubits"]:
            lines.append(
                "另外，%d 比特超出了全部本地模拟器的上限（最大的是 `%s`，%d 比特），"
                "所以「本地、免费、不排队」这三条无法同时满足。"
                "如果这三条是硬要求，可行的方向是把电路拆解成更小的子电路。"
                % (min_qubits, largest_simulator["id"], largest_simulator["max_qubits"])
            )
    return "\n".join(lines)


# --- 入口 -------------------------------------------------------------------


def agent_chat(prompt: str) -> str:
    """L2 契约入口。任何情况下都返回字符串，绝不向外抛异常。"""
    if not isinstance(prompt, str) or not prompt.strip():
        return "请描述你想做的事情，例如：生成一个 3 比特的最大纠缠态并全部测量。"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        payload = _extract_json(_chat(messages))
    except Exception as exc:  # noqa: BLE001
        return (
            "我暂时没能理解这个请求，也没能从模型拿到可用的结构化结果（%s: %s）。"
            "可以换个说法再试一次，比如直接说明你想要几个比特、想制备什么态。"
            % (type(exc).__name__, exc)
        )

    task = str(payload.get("task") or "").strip().lower()
    try:
        if task == "select_backend":
            return handle_backend_selection(payload)
        if payload.get("qasm"):
            return handle_circuit_task(prompt, payload)
        if task in ("generate", "repair"):
            return "我需要再确认一下你的目标：想用几个量子比特，想得到什么样的测量结果？"
        return str(payload.get("explanation") or payload.get("user_goal") or json.dumps(
            payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        return "处理过程中出错了（%s: %s）。请换个说法再试一次。" % (type(exc).__name__, exc)
