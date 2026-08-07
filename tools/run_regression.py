#!/usr/bin/env python3
"""把隐藏电路回归集喂给真实后端，与理想分布比保真度。

理想分布由 loomq.refsim 精确计算（见 gen_circuits.py），不是采样，可直接当判定基准。

**关于 shots 的选择**：官方阈值是 0.97，但保真度本身有采样噪声，态数越多噪声越大。
用参考模拟器对每个电路跑 30 个不同随机种子实测的最低保真度（即"正确实现能有多倒霉"）：

    电路        态数   8192 shots   32768 shots   65536 shots
    grover3     8      0.9858       0.9908        0.9941
    random_b    16     0.9794       0.9911        0.9932
    random_c    32     0.9725       0.9864        0.9908

8192 shots 下 random_c 距阈值只剩 0.0025——**正确的实现也会偶发 FAIL**，那种假警报会
把人送上错误的排查方向。所以默认取 32768，此时最差余量约 0.016。

这不与题面矛盾。题面说 0.97 阈值已为 8192 shots 留出余量（完美实现约 0.99+），那是针对
官方那批电路说的；而 random_c 是我们自造的 5 比特随机电路，概率摊在 32 个态上，
同样 shots 下涨落显然更大。官方怎么判我们管不着，自己的回归要自己留足余量。

用法（仓库根目录）：
    python tools/run_regression.py                      # 默认 refsim，任何环境都能跑
    python tools/run_regression.py --target spinq,braket
    python tools/run_regression.py --target braket --shots 65536

refsim 目标不经过 SDK，用参考模拟器自己采样再和自己的理想分布比。它验证的是
"采样—归一化—比对"这条链路和 shots 数够不够，不验证后端；但它保证这个脚本在
没装任何 SDK 的机器上也能先跑通，排除脚本本身的问题。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq import parse  # noqa: E402
from loomq.refsim import hellinger_fidelity, sample  # noqa: E402

REGRESSION_DIR = os.path.join(ROOT, "regression")
CIRCUIT_DIR = os.path.join(REGRESSION_DIR, "circuits")
THRESHOLD = 0.97

ORDER = ["ghz5", "qft4", "grover3", "random_a", "random_b", "random_c"]


def load_ideals():
    path = os.path.join(REGRESSION_DIR, "ideal_distributions.json")
    if not os.path.exists(path):
        raise SystemExit("找不到 %s，先运行：python tools/gen_circuits.py" % path)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def run_one(circuit, target, shots):
    """返回 counts。refsim 走参考模拟器采样，其余走真实 SDK。"""
    if target == "refsim":
        return sample(circuit, shots, seed=20260807)
    from loomq import backends

    return backends.execute(circuit, target, shots)["counts"]


def to_probabilities(counts):
    """counts → 概率。hellinger_fidelity 吃的是概率，直接喂计数会得到 0。"""
    total = sum(counts.values())
    if total <= 0:
        raise ValueError("counts 总和为 0")
    return {key: value / total for key, value in counts.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="隐藏电路回归")
    parser.add_argument("--target", default="refsim",
                       help="逗号分隔，可选 refsim,spinq,braket,originq")
    parser.add_argument("--shots", type=int, default=32768,
                       help="默认 32768。低于 16384 时 random_c 会有假警报，见模块开头的实测表")
    args = parser.parse_args()

    targets = [item.strip() for item in args.target.split(",") if item.strip()]
    ideals = load_ideals()

    print("=== 隐藏电路回归（shots=%d，阈值 %.2f）===\n" % (args.shots, THRESHOLD))
    if args.shots < 16384:
        print("提示：shots=%d 偏低，random_c 可能出现假警报（正确实现也会偶发 FAIL）。\n"
              % args.shots)

    failures = []
    skipped = []
    for name in ORDER:
        path = os.path.join(CIRCUIT_DIR, name + ".qasm")
        if not os.path.exists(path):
            raise SystemExit("找不到 %s，先运行：python tools/gen_circuits.py" % path)
        with open(path, encoding="utf-8") as handle:
            circuit = parse(handle.read())
        ideal = ideals[name]

        for target in targets:
            try:
                counts = run_one(circuit, target, args.shots)
            except Exception as exc:  # 后端不可用不应中断整轮回归
                skipped.append((name, target, exc))
                print("%-10s %-8s SKIP  %s" % (name, target, type(exc).__name__))
                continue

            fidelity = hellinger_fidelity(to_probabilities(counts), ideal)
            passed = fidelity >= THRESHOLD
            if not passed:
                failures.append((name, target, fidelity))
            print("%-10s %-8s fidelity=%.4f  %s"
                  % (name, target, fidelity, "PASS" if passed else "FAIL"))

    print()
    if skipped:
        # 同一个后端会对每个电路各报一次，按后端去重，否则同一句话要刷六遍
        reasons = {}
        for _, target, exc in skipped:
            reasons.setdefault(target, str(exc))
        print("跳过 %d 项，涉及 %d 个不可用后端：" % (len(skipped), len(reasons)))
        for target, reason in sorted(reasons.items()):
            print("  %-8s %s" % (target, reason))
        print()

    if failures:
        print("%d 项未达阈值：" % len(failures))
        for name, target, fidelity in failures:
            print("  %s/%s  %.4f" % (name, target, fidelity))
        print()
        print("排查顺序：")
        print("  1. 先跑 python tools/probe_bitorder.py —— 位序错误最常见")
        print("     但注意 ghz5 和 grover3 对位序错误免疫（分布回文对称），")
        print("     所以若只有 qft4 和 random_* 挂了，几乎必定是位序问题")
        print("  2. 若只有 qft4 挂了，查受控相位门语义：cu1 是 diag(1,1,1,exp(i*theta))，")
        print("     不是受控 rz。Braket 用 cphaseshift，OpenQASM 3 用 cp")
        print("  3. 若全挂且保真度接近 0，查 measure 映射与补零宽度")
        return 1

    real_targets = [target for target in targets if target != "refsim"]
    if real_targets and all(target in {item[1] for item in skipped} for target in real_targets):
        print("参考模拟器通过，但没有任何真实后端跑起来——这一步还不算完成。")
        print("装好 SDK 后重跑：python tools/run_regression.py --target %s"
              % ",".join(real_targets))
        return 2

    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
