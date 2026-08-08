"""counts 归一化：位序与统一结果 Schema。

大赛约定（Qiskit 风格）：counts 的 key 是 c[n-1]…c[1]c[0]，**最右侧字符是 c[0]**，
`bit_order` 固定为 "little"。各平台原生位序可能相反，归一化是中间层的职责。

警告：公开电路 bell/ghz3 的理想分布是对称的（00/11、000/111），整串反转后分布不变，
所以位序写错时公开自测依然会全 PASS。必须用非对称探针电路标定，见 tools/probe_bitorder.py。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Mapping, Optional

# 各后端原生位序是否与大赛约定一致。None 表示尚未在真机/模拟器上标定。
# 用 tools/probe_bitorder.py 确认后把值改成 True/False，不要凭猜测填写。
#
# 2026-08-07 实测标定（Python 3.10 + amazon-braket-sdk 1.97.0 + spinqit 0.2.4，Windows）：
# 两个后端的原生位串都以 q[0] 为**最左**字符，与大赛约定（最右为 c[0]）相反，因此都要反转。
# 2 比特与 3 比特两个非对称探针结论一致，排除了 measure 映射错位的干扰项。
NATIVE_MATCHES_CONTEST: Dict[str, Optional[bool]] = {
    "spinq": False,
    "braket": False,
    # 2026-08-08 实测标定（pyqpanda 3.8.5）：originq 原生位序**与约定一致**，
    # 与 spinq/braket 相反。这正是必须逐个标定、不能沿用别家结论的例子。
    # 两个非对称探针（2 比特翻 q[0] 得 "01"、3 比特翻 q[0]+q[1] 得 "011"）结论一致。
    "originq": True,
    # 真机与本地模拟器是两条独立代码路径，位序不能想当然沿用 spinq 的结论：
    # 云端拒绝显式 measure、自行在末尾全测量，映射关系由平台决定而非我们的 measure 语句。
    # 2026-08-08 真机实测标定（gemini_vp 任务 G-260808-0001，triangulum_vp 任务
    # S-260808-0001）：非对称探针 x q[0] 在 2 比特返回主峰 "10"（1023/1024）、
    # 3 比特返回主峰 "100"（492/1024，NMR 真机噪声可观但主峰无歧义），
    # 即原生位串以 q[0] 为最左字符，与大赛约定（最右为 c[0]）相反，需要反转。
    # 两个宽度、两个平台结论一致，排除了"自动测量整体错位"这个干扰项。
    "spinq_cloud": False,
}


def reverse_keys(counts: Mapping[str, int]) -> Dict[str, int]:
    """反转每个位串。用于把 big-endian 的原生结果转成大赛约定。"""
    flipped: Dict[str, int] = {}
    for key, value in counts.items():
        flipped[key[::-1]] = flipped.get(key[::-1], 0) + int(value)
    return flipped


def pad_keys(counts: Mapping[str, int], width: int) -> Dict[str, int]:
    """把位串左侧补零到统一宽度，避免不同后端省略高位零导致的 key 不齐。"""
    padded: Dict[str, int] = {}
    for key, value in counts.items():
        normalized = key.zfill(width)
        padded[normalized] = padded.get(normalized, 0) + int(value)
    return padded


def normalize(
    counts: Mapping[str, int],
    width: int,
    target: str,
    native_matches_contest: Optional[bool] = None,
) -> Dict[str, int]:
    """统一位宽与位序。

    native_matches_contest 为 None 时回退到 NATIVE_MATCHES_CONTEST 表；若表中也是 None，
    则原样返回并由调用方在标定阶段人工核对——绝不静默猜测。
    """
    if native_matches_contest is None:
        native_matches_contest = NATIVE_MATCHES_CONTEST.get(target)
    result = pad_keys(counts, width)
    if native_matches_contest is False:
        result = reverse_keys(result)
    return result


def coerce_to_counts(raw: Mapping[str, object], shots: int) -> Dict[str, int]:
    """把后端返回的原始结果统一成整数计数，且总和严格等于 shots。

    有些 SDK 版本返回概率而不是计数。判定要求 counts 总和必须**精确等于** shots，
    所以按最大余数法分配，避免四舍五入导致总和差 1 而整条 case 被判 schema 非法。
    """
    values = {str(key): float(value) for key, value in raw.items()}  # type: ignore[arg-type]
    if not values:
        raise ValueError("后端返回的 counts 为空")

    total = sum(values.values())
    looks_like_counts = all(float(value).is_integer() for value in values.values()) and total > 1.5
    if looks_like_counts and int(round(total)) == shots:
        return {key: int(round(value)) for key, value in values.items()}

    if total <= 0:
        raise ValueError("后端返回的分布总和为 %r，无法归一化" % total)

    scaled = {key: value / total * shots for key, value in values.items()}
    floored = {key: int(value) for key, value in scaled.items()}
    remainder = shots - sum(floored.values())
    order = sorted(scaled, key=lambda key: scaled[key] - floored[key], reverse=True)
    for index in range(remainder):
        floored[order[index % len(order)]] += 1
    return floored


def build_result(
    backend: str,
    job_id: str,
    shots: int,
    counts: Mapping[str, int],
    transpiled_gates: int,
    depth: int,
    timestamp: Optional[str] = None,
) -> Dict[str, object]:
    """构造统一结果 Schema。字段与判定要求见题面第四节。

    evaluator.validate_schema 会检查：必填字段齐全、shots 为正整数、
    counts 为非空二进制串字典、counts 总和严格等于 shots、bit_order == "little"、
    meta 中不得出现 is_mock。
    """
    total = sum(int(value) for value in counts.values())
    if total != shots:
        raise ValueError("counts 总和 %d 与 shots %d 不一致，后端返回需先修正" % (total, shots))
    return {
        "backend": backend,
        "job_id": job_id,
        "shots": int(shots),
        "counts": {str(key): int(value) for key, value in counts.items()},
        "bit_order": "little",
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "meta": {"transpiled_gates": int(transpiled_gates), "depth": int(depth)},
    }
