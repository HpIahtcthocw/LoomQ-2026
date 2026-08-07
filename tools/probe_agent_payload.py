#!/usr/bin/env python3
"""看模型对某个 prompt 返回的原始结构化 payload，用来诊断自验回路的判定依据。"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "starter_kit")
for path in (KIT, ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

from loomq.agent import SYSTEM_PROMPT, _chat, _extract_json, reference_distribution, verify_qasm

prompt = sys.argv[1] if len(sys.argv) > 1 else "做一个两比特电路，测量结果只可能是 01 或 10，各占一半"
print("prompt:", prompt)
print()

raw = _chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}])
payload = _extract_json(raw)
print("--- 模型首轮 payload ---")
print(json.dumps(payload, ensure_ascii=False, indent=2))
print()

family = str(payload.get("target_family") or "custom")
n_qubits = int(payload.get("n_qubits") or 0)
declared = payload.get("expected_distribution")
reference, source = reference_distribution(family, n_qubits, declared)
print("--- 自验依据 ---")
print("family 标签   :", family)
print("模型声明分布  :", declared)
print("实际采用的基准:", reference)
print("基准来源      :", source)
print()

ok, reason, fidelity = verify_qasm(str(payload.get("qasm") or ""), family, n_qubits, declared)
print("首轮自验结果  :", "通过" if ok else "不通过")
print("理由          :", reason)
