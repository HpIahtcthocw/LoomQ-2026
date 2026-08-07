#!/usr/bin/env python3
"""位序标定：确定每个后端的原生位序是否与大赛约定一致。

为什么必须单独做这件事：
    公开电路 bell 的理想分布是 {"00":0.5,"11":0.5}，ghz3 是 {"000":0.5,"111":0.5}，
    **两者都是对称的**——把整个位串反转过来，分布一模一样。所以位序写错时公开自测
    依然全 PASS，直到隐藏电路（Grover-3 期望 94.5% 落在 111、QFT-4 四个峰）才会翻车。

做法：
    用只翻转部分比特的非对称探针电路。大赛约定 key 的最右字符是 c[0]，
    因此 `x q[0]` 作用于 2 比特系统时，正确结果必须是 "01"（c[1]=0, c[0]=1）。
    若得到 "10"，说明该后端原生位序相反，需在中间层反转。

用法（在装好 SDK 的机器上，仓库根目录）：
    python tools/probe_bitorder.py
    python tools/probe_bitorder.py --target braket --shots 512

标定完成后，把结论写进 starter_kit/loomq/counts.py 的 NATIVE_MATCHES_CONTEST。
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq import parse  # noqa: E402
from loomq import backends  # noqa: E402
from loomq.counts import coerce_to_counts  # noqa: E402

# 探针：每个是 (说明, QASM, 大赛约定下的唯一正确 key, 位序相反时会看到的 key)
PROBES = [
    (
        "2 比特，只翻转 q[0]",
        """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[0];
measure q[0] -> c[0];
measure q[1] -> c[1];
""",
        "01",
        "10",
    ),
    (
        "3 比特，翻转 q[0] 与 q[1]",
        """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
x q[0];
x q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
""",
        "011",
        "110",
    ),
]


def probe_one(target: str, shots: int) -> None:
    print("\n=== target: %s ===" % target)
    verdicts = []

    for description, qasm, contest_key, reversed_key in PROBES:
        circuit = parse(qasm)
        try:
            # 绕过 normalize，直接看后端原生输出，否则会被已有配置干扰判断。
            raw = _raw_counts(circuit, target, shots)
        except backends.BackendUnavailable as exc:
            print("  跳过：%s" % exc)
            return
        except Exception as exc:  # noqa: BLE001
            print("  [ERROR] %s: %s" % (type(exc).__name__, exc))
            return

        dominant = max(raw, key=raw.get)
        if dominant == contest_key:
            verdict = "一致"
        elif dominant == reversed_key:
            verdict = "相反"
        else:
            verdict = "异常"
        verdicts.append(verdict)
        print("  %s -> 主峰 %r（约定应为 %r）：%s" % (description, dominant, contest_key, verdict))
        if verdict == "异常":
            print("    原生 counts: %s" % dict(sorted(raw.items(), key=lambda kv: -kv[1])[:6]))

    if not verdicts:
        return
    if all(v == "一致" for v in verdicts):
        print('  结论：NATIVE_MATCHES_CONTEST["%s"] = True' % target)
    elif all(v == "相反" for v in verdicts):
        print('  结论：NATIVE_MATCHES_CONTEST["%s"] = False  # 需要反转位串' % target)
    else:
        print("  结论：两个探针结论不一致，说明不是单纯的位序问题——先查 measure 映射与补零宽度")


def _raw_counts(circuit, target: str, shots: int) -> dict:
    """只取后端原生 counts，不做位序归一化。"""
    payload = backends.RUNNERS[target](circuit, shots)
    return coerce_to_counts(payload["counts"], shots)


def main() -> int:
    parser = argparse.ArgumentParser(description="LoomQ 后端位序标定")
    parser.add_argument("--target", default="all", help="spinq / braket / originq / all")
    parser.add_argument("--shots", type=int, default=1024)
    args = parser.parse_args()

    print("=== LoomQ 位序标定 ===")
    print("已安装的后端：%s" % backends.available())
    print("\n注意：本脚本读取的是经过 normalize 之后的 counts。第一次标定前，请确认")
    print("counts.py 的 NATIVE_MATCHES_CONTEST 三项都是 None（默认即是），否则结论会被污染。")

    targets = sorted(backends.RUNNERS) if args.target == "all" else [args.target]
    for target in targets:
        if target not in backends.RUNNERS:
            print("\n未知 target: %s" % target)
            continue
        probe_one(target, args.shots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
