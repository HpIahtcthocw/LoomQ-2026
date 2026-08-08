#!/usr/bin/env python3
"""结果解释文案的行为验证：纯标准库，不需要任何量子 SDK 或模型服务。

为什么这些文案值得测：专项奖标准要求用户"理解其科学原理"，而解释文案就是这条标准的
唯一交付物。它又是纯字符串逻辑，改动时最容易在不知不觉中退化。

本文件最重要的一组测试是 test_entanglement_survives_hardware_noise：
真机总会在本该为零的位置漏出几个百分点，若解释逻辑按"出现过几种结果"判断，
同一个贝尔态在模拟器上认得出纠缠、在真机上就认不出——恰恰在最该讲清原理的
那次运行上失语。这个退化不会让任何其他测试变红，所以必须在这里钉住。

用法（仓库根目录）：
    python tools/selftest_explain.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq.visualize import (  # noqa: E402
    bitstring_legend,
    explain_distribution,
    explain_hardware_noise,
)

FAILURES = []

# 真机实测数据，不是编的：gemini_vp 任务 G-260808-0003 与 triangulum_vp 任务 S-260808-0002
REAL_BELL = {"00": 0.4521, "11": 0.4385, "10": 0.0615, "01": 0.0479}
REAL_GHZ3 = {
    "000": 0.3701, "111": 0.3662, "010": 0.0645, "101": 0.0605,
    "100": 0.0518, "011": 0.0488, "001": 0.0195, "110": 0.0186,
}
IDEAL_BELL = {"00": 0.5, "11": 0.5}
IDEAL_GHZ3 = {"000": 0.5, "111": 0.5}


def check(label: str, condition: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         ("  -> " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def test_entanglement_survives_hardware_noise() -> None:
    """噪声不该冲掉纠缠的解释，但也不该让它无根据地出现。"""
    for name, distribution in (("真机贝尔态", REAL_BELL), ("真机 GHZ-3", REAL_GHZ3)):
        text = explain_distribution(distribution)
        check("%s:讲出了纠缠" % name, "纠缠" in text, text)
        check("%s:没谎称中间结果为零" % name, "一次都没出现" not in text, text)
    for name, distribution in (("理想贝尔态", IDEAL_BELL), ("理想 GHZ-3", IDEAL_GHZ3)):
        text = explain_distribution(distribution)
        check("%s:讲出了纠缠" % name, "纠缠" in text, text)
        check("%s:如实说中间结果一次未现" % name, "一次都没出现" in text, text)

    # 真机实测的坏批次（任务 G-260808-0004）：11 只有 28.5%，与最大杂峰 01 的 11.7%
    # 仅差 2.4 倍。这时数据本身分不清 11 是信号还是噪声，就**不该**断言纠缠。
    # 早先"占最高峰一半以上算主峰"的实现正是死在这组数据上，钉住它防止回退。
    bad_batch = {"00": 0.5947, "11": 0.2852, "01": 0.1172, "10": 0.0029}
    text = explain_distribution(bad_batch)
    check("真机坏批次:证据不足时不硬说纠缠", "纠缠" not in text, text)
    check("真机坏批次:仍给出可读描述", "00" in text, text)


def test_uniform_and_concentrated_unchanged() -> None:
    """噪声容忍不能把均匀叠加与高集中度这两类误判成纠缠。"""
    uniform = {"00": 0.25, "01": 0.25, "10": 0.25, "11": 0.25}
    text = explain_distribution(uniform)
    check("均匀叠加:认出均匀叠加", "均匀叠加" in text, text)
    check("均匀叠加:不误报纠缠", "纠缠" not in text, text)

    grover = {"111": 0.78, "000": 0.031, "001": 0.031, "010": 0.031,
              "011": 0.031, "100": 0.031, "101": 0.031, "110": 0.034}
    text = explain_distribution(grover)
    check("Grover:指出主峰是 111", "111" in text, text)
    check("Grover:不误报纠缠", "纠缠" not in text, text)

    certain = {"101": 0.97, "001": 0.03}
    text = explain_distribution(certain)
    check("高集中度:说成确定答案", "确定的答案" in text, text)

    # 全 0 / 全 1 之外的两峰不是纠缠，别乱贴标签
    two_peak = {"01": 0.49, "10": 0.48, "00": 0.02, "11": 0.01}
    text = explain_distribution(two_peak)
    check("反相关双峰:不误报纠缠", "纠缠" not in text, text)


def test_hardware_noise_explanation() -> None:
    """真机偏差解释必须对着理想分布讲，且两类偏差都要覆盖。"""
    text = explain_hardware_noise(REAL_BELL, IDEAL_BELL)
    check("真机解释:点明是真机", "真实的量子计算机" in text, text)
    check("真机解释:给出漏出比例", "10.9%" in text, text)
    check("真机解释:列出该出现的结果", "00、11" in text, text)
    check("真机解释:讲退相干", "退相干" in text, text)
    check("真机解释:落到量子纠错", "量子纠错" in text, text)

    # 坏批次：漏出 12.0%，且 00/11 严重失衡（59.5% vs 28.5%）。
    # 失衡这类偏差比漏出更常见，早先的实现完全没提，等于漏讲一半现象。
    bad_batch = {"00": 0.5947, "11": 0.2852, "01": 0.1172, "10": 0.0029}
    text = explain_hardware_noise(bad_batch, IDEAL_BELL)
    check("坏批次:漏出比例按理想支撑集算", "12.0%" in text, text)
    check("坏批次:不误报 11 为杂峰", "00、11" in text, text)
    check("坏批次:点出两峰失衡", "一高一低" in text, text)
    check("坏批次:给出两峰实际占比", "59.5%" in text and "28.5%" in text, text)

    clean = explain_hardware_noise(IDEAL_BELL, IDEAL_BELL)
    check("真机解释:干净时说清无杂散", "相当干净" in clean, clean)
    check("真机解释:干净时不编造失衡", "一高一低" not in clean, clean)

    check("真机解释:空输入不炸", explain_hardware_noise({}, IDEAL_BELL) != "")


def test_bitstring_legend() -> None:
    """位串读法是新手最容易搞错的地方，最右一位必须点明。"""
    for width in (2, 3, 5):
        text = bitstring_legend(width)
        check("位序说明 %d 比特:点明最右是 c[0]" % width, "最右" in text and "c[0]" in text, text)


def test_no_markdown_in_terminal_text() -> None:
    """终端文案里不能出现 markdown 标记——它不会被渲染，只会变成字面量。

    连同 CLI 的静态文案一起扫。写的时候顺手敲出 **加粗** 太容易了，
    而这种错只有真在终端里跑一次才看得见，靠人工复核挡不住。
    """
    from loomq import cli

    samples = {
        "真机分布解释": explain_distribution(REAL_BELL),
        "真机偏差解释": explain_hardware_noise(REAL_BELL, IDEAL_BELL),
        "理想分布解释": explain_distribution(IDEAL_GHZ3),
        "位序说明": bitstring_legend(3),
        "开场引导": cli.WELCOME,
        "真机提示": cli.WELCOME_HARDWARE,
        "帮助": cli.HELP,
    }
    for name, text in samples.items():
        check("%s:无 markdown 粗体" % name, "**" not in text, text)


def main() -> int:
    print("=== 结果解释文案验证（无需量子 SDK 与模型服务）===\n")
    test_entanglement_survives_hardware_noise()
    print()
    test_uniform_and_concentrated_unchanged()
    print()
    test_hardware_noise_explanation()
    print()
    test_bitstring_legend()
    print()
    test_no_markdown_in_terminal_text()
    print("\n" + ("全部通过" if not FAILURES else "失败 %d 项: %s" % (len(FAILURES), FAILURES)))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
