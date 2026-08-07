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

位串与比特的对应关系（这是最容易出错的地方，务必逐位核对）：
位串写作 c[n-1]…c[1]c[0]，**最右边的字符是 c[0]，最左边的字符是 c[n-1]**。
举例，4 比特要让测量结果确定等于 "1100"，先逐位拆开：
    c[3]=1, c[2]=1, c[1]=0, c[0]=0
在默认的 measure q[i] -> c[i] 连线下，需要翻转的是 q[2] 和 q[3]，即 x q[2]; x q[3];
写成 x q[0]; x q[1]; 会得到 "0011"，方向正好相反。

用户用序数指代比特时（"第一个比特"、"第 1 位"）指的是 q[0]，它对应 c[0]，
落在位串**最右边**那一位；"第二个比特"是 q[1]，落在右边第二位，依此类推。
所以对某个指定比特施加的约束，写进位串时要从右往左数，不是从左往右数。

用户明确列举了允许的测量结果时，必须严格按他给的位串设计电路，不要替换成你更熟悉的
标准态。例如用户要"只能是 000 或 011"，那就不能给 000/111 的 GHZ 态——它们不是一回事。

target_family 是给系统做自动校验用的标签，它必须精确对应测量分布，不是态的俗称。按下面
的**分布**来填，不要按名字来填：

- "bell"：**只**用于测量结果为 00 和 11 各一半的两比特态（即 |Φ+>）。
- "ghz"：**只**用于测量结果为 全 0 和 全 1 各一半的 n 比特态，n_qubits 填 n。
- "uniform"：**只**用于全部 2^n 种结果等概率的态。
- "custom"：以上之外的一切情况，此时 expected_distribution 必须准确填写。

特别注意：贝尔态有四个，GHZ 也有局部翻转与相位的变体。如果用户要的是 01 与 10 各一半、
或带负号相位、或任何其他分布，即使它在物理上也叫"贝尔态"或"最大纠缠态"，target_family
也必须填 "custom" 并写出真实的 expected_distribution。标签填错会让系统用错误的基准否决
你正确的电路。

无论填哪个族，只要你给了 expected_distribution，它就必须与你的电路真实测量分布一致。

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


def canonical_from_family(family: str, n_qubits: int) -> Optional[Dict[str, float]]:
    """由态族名推出规范分布。名字只能定到"族"，不能定到唯一成员，见下方注释。"""
    family = (family or "").strip().lower()
    if family == "bell":
        return {"00": 0.5, "11": 0.5}
    if family == "ghz" and 2 <= n_qubits <= 16:
        return {"0" * n_qubits: 0.5, "1" * n_qubits: 0.5}
    if family == "uniform" and 1 <= n_qubits <= 16:
        size = 2**n_qubits
        weight = 1.0 / size
        return {format(index, "0%db" % n_qubits): weight for index in range(size)}
    return None


def _normalized(declared: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not declared:
        return None
    try:
        total = sum(float(value) for value in declared.values())
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return {str(key): float(value) / total for key, value in declared.items()}


def reference_distribution(
    family: str, n_qubits: int, declared: Optional[Dict[str, float]]
) -> Tuple[Optional[Dict[str, float]], str]:
    """给出首选判定基准：族名能推导就用族名，否则回退到模型声明。

    族名优先是有意的——它是我们唯一不依赖模型自我陈述的信号，能挡住"编个错电路再编个
    配套的错期望"这种自洽性造假。但族名天生欠定（贝尔态有四个，GHZ 有局部翻转变体），
    所以族名与声明冲突时不能直接判错，见 classify_verification。
    """
    canonical = canonical_from_family(family, n_qubits)
    if canonical is not None:
        label = (family or "").strip().lower()
        if label == "ghz":
            return canonical, "独立推导（GHZ-%d）" % n_qubits
        if label == "uniform":
            return canonical, "独立推导（%d 比特均匀叠加）" % n_qubits
        return canonical, "独立推导（贝尔态 Φ+）"
    normalized = _normalized(declared)
    if normalized is not None:
        return normalized, "采信模型声明（custom）"
    return None, "无参考分布，仅做可解析性与结构校验"


PASS = "pass"
LABEL_CONFLICT = "label_conflict"
FAIL = "fail"


def _describe(distribution: Dict[str, float], limit: int = 8) -> str:
    """描述一个分布。会被送进裁决 prompt，所以截断时必须说明，否则均匀分布看起来会像不均匀。"""
    ordered = sorted(distribution.items(), key=lambda kv: -kv[1])
    shown = "、".join("%s:%.4f" % (key, value) for key, value in ordered[:limit])
    if len(ordered) <= limit:
        return "共 %d 个可能结果 —— %s" % (len(ordered), shown)
    return "共 %d 个可能结果，概率最高的 %d 个是 %s（其余 %d 个概率更低或相同）" % (
        len(ordered), limit, shown, len(ordered) - limit,
    )


def classify_verification(
    qasm: str, family: str, n_qubits: int, declared: Optional[Dict[str, float]]
) -> Tuple[str, str, Optional[float]]:
    """三态自验。返回 (状态, 说明, 保真度)。

    为什么需要第三种状态而不是简单的通过/不通过：族名与显式声明冲突时，有两种可能，
    而它们在内部**结构上完全同形**，光靠代码分不开：

      甲、族名是对的，电路错了，模型又编了一个与错电路自洽的假分布（真造假，该重试）。
      乙、电路和分布都对，只是族名贴错了（例如 01/10 的纠缠态被标成 bell）。这时
          否决它就会把正确答案改坏——这是实测踩到过的真实事故。

    两者的唯一区分依据是**用户原话**，那不在这个函数的视野里。所以这里只负责如实报出
    "冲突"，把裁决交给上层带着用户原始请求去做一次定向确认。
    """
    try:
        circuit = parse(qasm)
    except Exception as exc:  # noqa: BLE001
        return FAIL, "无法解析：%s: %s" % (type(exc).__name__, exc), None

    if not circuit.measures():
        return FAIL, "电路里没有 measure 语句，测不出任何结果", None

    width = n_qubits or circuit.n_qubits
    canonical = canonical_from_family(family, width)
    normalized = _normalized(declared)
    actual = ideal_distribution(circuit)

    if canonical is not None:
        fidelity = hellinger_fidelity(actual, canonical)
        if fidelity >= FIDELITY_THRESHOLD:
            return PASS, "保真度 %.4f，基准由族名独立推导" % fidelity, fidelity
        if normalized is not None:
            declared_fidelity = hellinger_fidelity(actual, normalized)
            if declared_fidelity >= FIDELITY_THRESHOLD:
                return (
                    LABEL_CONFLICT,
                    "电路与模型自己声明的分布一致（保真度 %.4f，%s），"
                    "但与族名 \"%s\" 推导出的基准只有 %.4f（%s）。族名和声明有一个是错的。"
                    % (declared_fidelity, _describe(normalized), family, fidelity, _describe(canonical)),
                    declared_fidelity,
                )
        return (
            FAIL,
            "保真度只有 %.4f（阈值 %.2f）。按族名 \"%s\" 应主要落在 %s，"
            "但这个电路实际主要落在 %s"
            % (fidelity, FIDELITY_THRESHOLD, family,
               sorted(canonical, key=lambda key: -canonical[key])[:4],
               [key for key, _ in sorted(actual.items(), key=lambda kv: -kv[1])[:4]]),
            fidelity,
        )

    if normalized is not None:
        fidelity = hellinger_fidelity(actual, normalized)
        if fidelity >= FIDELITY_THRESHOLD:
            return PASS, "保真度 %.4f，基准来自模型声明（custom）" % fidelity, fidelity
        return (
            FAIL,
            "保真度只有 %.4f（阈值 %.2f）。声明期望落在 %s，但电路实际落在 %s"
            % (fidelity, FIDELITY_THRESHOLD,
               sorted(normalized, key=lambda key: -normalized[key])[:4],
               [key for key, _ in sorted(actual.items(), key=lambda kv: -kv[1])[:4]]),
            fidelity,
        )

    return PASS, "结构校验通过（无参考分布，仅校验可解析性与 measure）", None


def verify_qasm(
    qasm: str, family: str, n_qubits: int, declared: Optional[Dict[str, float]]
) -> Tuple[bool, str, Optional[float]]:
    """布尔版自验。冲突按不通过处理，交由上层裁决。"""
    status, reason, fidelity = classify_verification(qasm, family, n_qubits, declared)
    return status == PASS, reason, fidelity


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


ADJUDICATION_PROMPT = """判断下面这个电路的测量分布是否就是用户想要的。

用户的原始请求：
%s

这个电路实际的测量分布（由无噪声模拟精确算出，位串最右边是 c[0]）：
%s

只返回一个 JSON 对象，不要解释：
{"matches_user_request": true 或 false, "why": "一句话理由"}

只看分布本身是否满足用户的要求，不要在意用户或别人给这个态起的名字叫什么。"""


def adjudicate_label_conflict(prompt: str, payload: Dict[str, Any]) -> bool:
    """族名与声明分布冲突时的裁决：拿电路的**真实**分布去问用户原始请求是否被满足。

    关键是这里问的不是"你贴的标签对不对"，而是把 refsim 精确算出的实际分布摆出来，问它
    是否满足用户原话。分布是我们独立算的，模型无从粉饰；同时"用户原话"这个唯一的外部
    基准也被带进来了，正好补上 classify_verification 缺的那块信息。

    任何异常都返回 False，退回正常重试路径——裁决只用来救回正确答案，不用来放宽标准。
    """
    try:
        circuit = parse(str(payload.get("qasm") or ""))
        actual = ideal_distribution(circuit)
    except Exception:  # noqa: BLE001
        return False
    try:
        verdict = _extract_json(
            _chat(
                [
                    {
                        "role": "user",
                        "content": ADJUDICATION_PROMPT % (prompt.strip(), _describe(actual)),
                    }
                ]
            )
        )
    except Exception:  # noqa: BLE001
        return False
    return verdict.get("matches_user_request") is True


def handle_circuit_task(prompt: str, first: Dict[str, Any]) -> str:
    """意图生成与代码纠错共用一条"生成 → 自验 → 带反馈重试"的闭环。"""
    payload = first
    history: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    last_reason = ""
    # 重试有可能越改越差，所以留住历史最优版本，最后交出去的是它而不是最后一版。
    best_payload = payload
    best_fidelity = -1.0

    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        qasm = str(payload.get("qasm") or "")
        status, reason, fidelity = classify_verification(
            qasm,
            str(payload.get("target_family") or "custom"),
            int(payload.get("n_qubits") or 0),
            payload.get("expected_distribution"),
        )
        if status == PASS:
            note = "" if attempt == 1 else "（第 %d 次尝试通过自验）" % attempt
            return _format_circuit_reply(payload, note)

        if status == LABEL_CONFLICT and adjudicate_label_conflict(prompt, payload):
            note = "（族名标签与分布不一致，已按你的原始要求确认电路正确）"
            return _format_circuit_reply(payload, note)

        if fidelity is not None and fidelity > best_fidelity:
            best_fidelity, best_payload = fidelity, payload
        last_reason = reason
        if attempt == MAX_GENERATION_ATTEMPTS:
            break

        history.append(
            {
                "role": "user",
                "content": "你上一版的电路没有通过自动验证：%s\n"
                "请修正后重新返回同样格式的 JSON。保持用户原本声明的目标不变，"
                "只允许使用 12 门白名单，并确认 measure 覆盖了所有相关比特。\n"
                "如果你认为上一版电路其实是对的，只是 target_family 这个标签不准确"
                "（例如把 01/10 的纠缠态标成了 bell），那就保留电路不动，"
                "改成 target_family=\"custom\" 并把 expected_distribution 填成真实分布。" % reason,
            }
        )
        try:
            payload = _extract_json(_chat(history))
        except Exception as exc:  # noqa: BLE001
            last_reason = "%s（重试时模型回复无法解析：%s）" % (reason, exc)
            break
        history.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})

    # 仍未通过：如实说明，但仍然把最好的一版交出去，避免整个 case 拿不到任何东西
    note = "注意：这一版没有通过我的自动验证（%s）。它仍可运行，但结果可能与你的预期不同。" % last_reason
    return _format_circuit_reply(best_payload, note)


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
