#!/usr/bin/env python3
"""L2 Agent 离线验证：不需要 API key，不需要量子 SDK。

手法借自官方的 tests/test_l2_contract.py——起一个本地 HTTP 服务假装 OpenAI 兼容端点，
按脚本返回预设回复。这样可以把 Agent 的全部行为验证掉，包括：
  - 三类任务的路由
  - 自验闭环：模型给出错电路时能不能识别并带反馈重试
  - 造假防护：模型编一个错电路 + 配套的错期望，能不能被独立参考分布挡住
  - 选后端：答案是否来自能力表数据而非模型记忆
  - 无解情形是否如实说明
  - 环境变量缺失、模型返回垃圾时是否优雅降级而不抛异常

用法（仓库根目录）：
    python tools/selftest_agent.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "starter_kit"))

from loomq import agent  # noqa: E402

FAILURES = []
# 由每个测试设置：一个回复列表，服务按调用顺序依次返回
SCRIPT: list = []
CALL_LOG: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print("[%s] %s%s" % ("PASS" if condition else "FAIL", label,
                         ("  -> " + detail) if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class ScriptedHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        CALL_LOG.append(json.loads(self.rfile.read(length)))
        reply = SCRIPT[len(CALL_LOG) - 1] if len(CALL_LOG) <= len(SCRIPT) else SCRIPT[-1]
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}]})
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def run_agent(prompt: str, script: list) -> str:
    """在假端点下调用 agent_chat，返回回复文本。"""
    global SCRIPT, CALL_LOG
    SCRIPT, CALL_LOG = script, []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    saved = {key: os.environ.get(key) for key in
             ("LOOMQ_LLM_BASE_URL", "LOOMQ_LLM_API_KEY", "LOOMQ_LLM_MODEL", "LOOMQ_LLM_TIMEOUT_SECONDS")}
    os.environ.update({
        "LOOMQ_LLM_BASE_URL": "http://127.0.0.1:%d" % server.server_port,
        "LOOMQ_LLM_API_KEY": "offline-test-key",
        "LOOMQ_LLM_MODEL": "offline-test-model",
        "LOOMQ_LLM_TIMEOUT_SECONDS": "10",
    })
    try:
        return agent.agent_chat(prompt)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# 官方 evaluator 用这个正则抽取 QASM，回复格式必须能被它正确截取
OFFICIAL_QASM_PATTERN = re.compile(r"OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)", re.DOTALL | re.MULTILINE)


def extract_like_evaluator(text: str):
    match = OFFICIAL_QASM_PATTERN.search(text)
    return match.group(0).strip() if match else None


GHZ3_GOOD = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0],q[1];
cx q[1],q[2];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
"""

# 缺了第二个 cx，得到的是 (|000>+|110>)/√2，不是 GHZ
GHZ3_BAD = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
h q[0];
cx q[0],q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
measure q[2] -> c[2];
"""


def payload(qasm: str, family: str = "ghz", n: int = 3, expected=None, explanation="三个比特会永远同时是 0 或同时是 1。") -> str:
    body = {
        "task": "generate",
        "qasm": qasm,
        "target_family": family,
        "n_qubits": n,
        "expected_distribution": expected or {"000": 0.5, "111": 0.5},
        "explanation": explanation,
    }
    return json.dumps(body, ensure_ascii=False)


def test_generation_happy_path() -> None:
    reply = run_agent("生成一个 3 比特的最大纠缠态并进行全测量", [payload(GHZ3_GOOD)])
    extracted = extract_like_evaluator(reply)
    check("生成:官方正则能抽出 QASM", extracted is not None)
    check("生成:抽出的是完整程序且不含解释文字",
          bool(extracted) and extracted.startswith("OPENQASM 2.0;") and "measure" in extracted,
          repr(extracted))
    check("生成:抽出的内容能通过自验",
          agent.verify_qasm(extracted or "", "ghz", 3, None)[0])
    check("生成:一次通过时只调用模型 1 次", len(CALL_LOG) == 1, "实际 %d 次" % len(CALL_LOG))
    check("生成:回复里带了给零基础用户的解释", "永远同时" in reply)


def test_self_verify_retry() -> None:
    """模型第一版给错电路，Agent 必须识别、带反馈重试，第二版通过。"""
    reply = run_agent("帮我做一个三比特的 GHZ 态", [payload(GHZ3_BAD), payload(GHZ3_GOOD)])
    check("自验:错电路触发了重试", len(CALL_LOG) == 2, "实际调用 %d 次" % len(CALL_LOG))
    check("自验:重试的 prompt 里带了具体失败原因",
          len(CALL_LOG) > 1 and "保真度" in json.dumps(CALL_LOG[1], ensure_ascii=False))
    check("自验:最终交付的是正确电路",
          agent.verify_qasm(extract_like_evaluator(reply) or "", "ghz", 3, None)[0])
    check("自验:回复注明了是第 2 次通过", "第 2 次尝试" in reply, reply[:120])


def test_self_consistent_forgery_is_caught() -> None:
    """模型编一个错电路，又编一个与之**真正**自洽的错期望——必须被族名推导挡住。

    注意 expected 必须按小端序写（key = c[2]c[1]c[0]）。GHZ3_BAD 少了第二个 cx，得到
    (|q0q1q2=000> + |q0q1q2=110>)/√2，对应位串是 000 与 011，不是 110。早先这里误写成
    {000,110}，那个"假期望"其实和错电路对不上，于是这条断言是靠巧合通过的，真正的
    自洽造假路径从未被测到。
    """
    forged = payload(GHZ3_BAD, family="ghz", n=3,
                     expected={"000": 0.5, "011": 0.5})  # 与错电路真正自洽的假期望
    reply = run_agent("生成 3 比特 GHZ 态", [forged, forged, forged])
    # 真自洽的造假现在会命中 LABEL_CONFLICT，于是每轮多一次裁决调用：3 轮 × (生成 + 裁决) = 6。
    # 这里的假端点对裁决请求也只会回吐那个 forged payload，它没有 matches_user_request
    # 字段，裁决按 False 处理并退回重试——正是我们要的保守行为。
    check("防伪:自洽的假期望没能骗过验证", len(CALL_LOG) == 6, "实际调用 %d 次" % len(CALL_LOG))
    check("防伪:裁决被触发且未放行", "没有通过我的自动验证" in reply, reply[:160])
    check("防伪:三次都失败后如实告知用户", "没有通过我的自动验证" in reply, reply[:160])
    check("防伪:仍然把电路交了出去，没有空手而归",
          extract_like_evaluator(reply) is not None)
    check("防伪:交付的不是被标为已验证的结果", "第 2 次尝试" not in reply and
          "已按你的原始要求确认" not in reply, reply[:160])


# 正确的"01/10 各一半"两比特纠缠态：h, cx 之后翻转 q[1]
PSI_PLUS = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0],q[1];
x q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
"""


def test_label_conflict_does_not_destroy_correct_answer() -> None:
    """族名贴错但电路正确时，裁决必须救回它，而不是把它改坏。

    这是实测踩到的真实事故：模型被要求做"测量只能是 01 或 10"的两比特纠缠态，电路
    (h, cx, x) 和声明分布 {01,10} 都对，但 target_family 标成了 "bell"。旧实现把 bell
    硬解释成 00/11，据此否决正确电路，再通过重试把模型"纠正"成真错的贝尔态。
    """
    mislabeled = json.dumps({
        "task": "generate",
        "qasm": PSI_PLUS,
        "target_family": "bell",           # 贴错了：这是 Ψ+，不是 Φ+
        "n_qubits": 2,
        "expected_distribution": {"01": 0.5, "10": 0.5},   # 这个是对的
        "explanation": "两个比特测出来一定一个 0 一个 1。",
    }, ensure_ascii=False)
    adjudication = json.dumps({"matches_user_request": True, "why": "分布正是 01 与 10 各一半"})
    reply = run_agent("做一个两比特电路，测量结果只可能是 01 或 10，各占一半",
                      [mislabeled, adjudication])
    check("标签冲突:只多花了一次裁决调用", len(CALL_LOG) == 2, "实际调用 %d 次" % len(CALL_LOG))
    delivered = extract_like_evaluator(reply)
    check("标签冲突:交付的电路没有被改动",
          bool(delivered) and "x q[1];" in delivered, repr(delivered))
    check("标签冲突:交付的电路分布确实是 01/10",
          agent.verify_qasm(delivered or "", "custom", 2, {"01": 0.5, "10": 0.5})[0])
    check("标签冲突:裁决 prompt 里带了实际分布而不是标签",
          len(CALL_LOG) > 1 and "01:0.5" in json.dumps(CALL_LOG[1], ensure_ascii=False),
          json.dumps(CALL_LOG[-1], ensure_ascii=False)[:200])


def test_label_conflict_rejected_falls_back_to_retry() -> None:
    """裁决说"不满足用户要求"时，必须退回正常重试，不能放宽标准。"""
    mislabeled = json.dumps({
        "task": "generate",
        "qasm": PSI_PLUS,
        "target_family": "bell",
        "n_qubits": 2,
        "expected_distribution": {"01": 0.5, "10": 0.5},
        "explanation": "两个比特测出来一定一个 0 一个 1。",
    }, ensure_ascii=False)
    refuse = json.dumps({"matches_user_request": False, "why": "用户要的是 00 与 11"})
    bell_good = json.dumps({
        "task": "generate",
        "qasm": """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0],q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
""",
        "target_family": "bell",
        "n_qubits": 2,
        "expected_distribution": {"00": 0.5, "11": 0.5},
        "explanation": "两个比特要么都是 0 要么都是 1。",
    }, ensure_ascii=False)
    reply = run_agent("我想要一个贝尔态", [mislabeled, refuse, bell_good])
    check("标签冲突:裁决否决后继续重试", len(CALL_LOG) == 3, "实际调用 %d 次" % len(CALL_LOG))
    check("标签冲突:最终交付的是 00/11 的贝尔态",
          agent.verify_qasm(extract_like_evaluator(reply) or "", "bell", 2, None)[0])


def test_custom_family_falls_back_to_declared() -> None:
    """非标准态族时才采信模型声明的分布。"""
    uniform2 = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
h q[1];
measure q[0] -> c[0];
measure q[1] -> c[1];
"""
    body = payload(uniform2, family="custom", n=2,
                   expected={"00": 0.25, "01": 0.25, "10": 0.25, "11": 0.25},
                   explanation="两个比特各自独立地五五开。")
    reply = run_agent("让两个比特各自处于均匀叠加并测量", [body])
    check("custom:采信声明分布并通过", extract_like_evaluator(reply) is not None)
    check("custom:一次通过", len(CALL_LOG) == 1, "实际 %d 次" % len(CALL_LOG))

    ok, reason, _ = agent.verify_qasm(uniform2, "uniform", 2, None)
    check("uniform:独立推导也应通过", ok, reason)


def test_backend_selection_from_table() -> None:
    body = json.dumps({
        "task": "select_backend", "min_qubits": 15, "require_no_queue": True,
        "forbid_paid": False, "kind": None, "user_goal": "15 比特且零排队",
    })
    reply = run_agent("我需要运行一个 15 比特电路，且零排队等待，选哪个平台？", [body])
    # backend_capabilities.md 给出的正确答案集
    expected_ids = {"spinq_taurus_simulator", "originq_local_simulator", "braket_local_simulator"}
    present = {backend_id for backend_id in expected_ids if backend_id in reply}
    check("选后端:命中官方正确答案集", present == expected_ids,
          "缺少 %s" % (expected_ids - present))
    check("选后端:排除了 8 比特的量旋真机", "spinq_cloud_qpu" not in reply)
    check("选后端:排除了付费云端", "braket_cloud" not in reply)
    check("选后端:给出了唯一推荐", "如果只想要一个答案" in reply)


def test_backend_selection_real_hardware_free() -> None:
    body = json.dumps({
        "task": "select_backend", "min_qubits": 5, "require_no_queue": False,
        "forbid_paid": True, "kind": "qpu", "user_goal": "真机 5 比特不花钱",
    })
    reply = run_agent("在真实量子硬件上跑一个 5 比特电路，不想花钱", [body])
    check("选后端:真机免费场景命中量旋真机", "spinq_cloud_qpu" in reply)
    check("选后端:真机免费场景命中悟空", "originq_wukong" in reply)
    check("选后端:排除了模拟器", "braket_local_simulator" not in reply)


def test_backend_selection_large_scale() -> None:
    """50 比特：能力表里 originq_wukong 上限 72，确实满足，所以不是"无解"。

    但 backend_capabilities.md 的示例同时要求"如实说明超出所有可用后端能力"。
    两种判定都要覆盖：既给出规范标识，也讲清能力边界。
    """
    body = json.dumps({
        "task": "select_backend", "min_qubits": 50, "require_no_queue": False,
        "forbid_paid": False, "kind": None, "user_goal": "50 比特",
    })
    reply = run_agent("我要跑一个 50 比特的电路", [body])
    check("大规模:给出了 72 比特的悟空真机", "originq_wukong" in reply)
    check("大规模:说明了超出所有模拟器能力", "超出了所有本地模拟器" in reply, reply)
    check("大规模:建议拆解电路", "拆解" in reply, reply)
    check("大规模:说明了需要排队", "排队" in reply)


def test_backend_selection_impossible() -> None:
    """真正无解：50 比特 + 零排队 + 模拟器。最大的零排队模拟器只有 30 比特。"""
    body = json.dumps({
        "task": "select_backend", "min_qubits": 50, "require_no_queue": True,
        "forbid_paid": True, "kind": "simulator", "user_goal": "50 比特本地免费不排队",
    })
    reply = run_agent("我想在本地免费跑一个 50 比特电路，不想等", [body])
    check("无解:如实说明没有后端满足", "没有任何后端能同时满足" in reply, reply[:200])
    check("无解:给出最接近的替代", "originq_wukong" in reply)
    check("无解:建议拆解电路", "拆解" in reply, reply[:300])


def test_graceful_degradation() -> None:
    reply = run_agent("随便说点什么", ["这不是 JSON，就是一段废话"])
    check("降级:模型返回垃圾时不抛异常且给出可读提示",
          isinstance(reply, str) and "换个说法" in reply, reply[:120])

    check("降级:空输入有引导", "请描述你想做的事情" in agent.agent_chat(""))

    saved = {key: os.environ.pop(key, None) for key in
             ("LOOMQ_LLM_BASE_URL", "LOOMQ_LLM_API_KEY", "LOOMQ_LLM_MODEL")}
    try:
        reply = agent.agent_chat("生成一个贝尔态")
        check("降级:环境变量缺失时不抛异常", isinstance(reply, str) and len(reply) > 0)
        check("降级:错误信息里不泄露任何 key", "offline-test-key" not in reply)
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def test_no_hardcoded_secrets() -> None:
    source = open(os.path.join(ROOT, "starter_kit", "loomq", "agent.py"), encoding="utf-8").read()
    check("合规:没有硬编码 API 地址", "https://api." not in source)
    check("合规:没有硬编码模型名", "deepseek" not in source.lower())
    check("合规:通过 llm_client 读环境变量", "llm_client" in source)


def main() -> int:
    print("=== L2 Agent 离线验证（无需 API key / 量子 SDK）===\n")
    for section, test in (
        ("意图生成", test_generation_happy_path),
        ("自验重试", test_self_verify_retry),
        ("防伪造", test_self_consistent_forgery_is_caught),
        ("标签冲突·救回", test_label_conflict_does_not_destroy_correct_answer),
        ("标签冲突·否决", test_label_conflict_rejected_falls_back_to_retry),
        ("custom 回退", test_custom_family_falls_back_to_declared),
        ("选后端·基本", test_backend_selection_from_table),
        ("选后端·真机免费", test_backend_selection_real_hardware_free),
        ("选后端·大规模", test_backend_selection_large_scale),
        ("选后端·真无解", test_backend_selection_impossible),
        ("优雅降级", test_graceful_degradation),
        ("合规检查", test_no_hardcoded_secrets),
    ):
        print("--- %s ---" % section)
        test()
        print()
    print("全部通过" if not FAILURES else "失败 %d 项: %s" % (len(FAILURES), FAILURES))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
