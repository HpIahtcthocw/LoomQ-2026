"""LoomQ 网页入口：把零基础引导从终端搬进浏览器。

为什么要有这个文件，而不是让 CLI 顶上：

专项奖的判据是"零背景的跨界创作者五分钟内完成人生第一个实验"。可是让一个
不懂技术的人先装 Python、再在 PowerShell 里敲 `python -m loomq.cli`，
这件事本身就跟"零门槛"的主张相冲突——终端本身就是门槛。真跑过一次就知道，
Windows 控制台连中文都要先切代码页才不乱码（见 cli._ensure_utf8_output）。

三个刻意选择：

**一、不引入任何新依赖。** 只用标准库 http.server + json。requirements.txt 里
braket / spinqit / pyqpanda 三家的版本约束已经互相咬得很紧，为了一个界面去引入
Flask 或 FastAPI，风险远大于收益。代价是要自己写路由和任务表，一共不到两百行。

**二、真机走异步任务 + 轮询。** 真机排队约两分钟，同步请求必然超时，而且用户
盯着一个转圈什么也看不到。任务表把每一步进度记下来，前端一秒拉一次，
排队、提交、取结果都实时显示——等待过程本身就是"这是真机不是模拟器"的证据。

**三、电路布局在后端算。** 前端只负责画。布局算法（哪个门排在第几列、
多比特门的竖线跨度）已经在 visualize.circuit_diagram 里验证过，
再用 JavaScript 重写一遍等于把一个已经踩平的坑重新挖开。

用法：
    python -m loomq.web                  # 起服务并自动打开浏览器
    python -m loomq.web --port 8900
    python -m loomq.web --no-browser
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import backends as backend_module
from .cli import BUILTIN_EXAMPLES, hardware_ready
from .ir import Circuit, Gate, Measure, format_param
from .qasm2_parser import QasmError, parse
from .refsim import ideal_distribution, sample
from .visualize import bitstring_legend, explain_distribution, explain_hardware_noise

WEBUI = Path(__file__).parent / "webui"
DEFAULT_SHOTS = 1024
# 界面上能选的运行环境，与 _state() 里给出的那一份保持一致。
KNOWN_BACKENDS = ("refsim", "spinq", "braket", "originq", "spinq_cloud")

# 门在界面上的显示名与配色分类。IR 里 12 个白名单门加一个只由分解产生的 u1。
GATE_LABEL = {
    "h": "H", "x": "X", "s": "S", "sdg": "S†", "t": "T", "tdg": "T†",
    "rz": "RZ", "ry": "RY", "u1": "U1", "cx": "⊕", "cu1": "CU1",
    "swap": "×", "ccx": "⊕",
}
# 每个门一句人话。新手看到 RZ 不知道那是什么，光画个方块等于没画。
GATE_HINT = {
    "h": "让这个比特进入「既是 0 又是 1」的状态",
    "x": "把 0 和 1 翻过来，相当于经典的非门",
    "s": "给状态加四分之一圈的相位",
    "sdg": "S 的逆操作，转回去",
    "t": "给状态加八分之一圈的相位",
    "tdg": "T 的逆操作，转回去",
    "rz": "绕 Z 轴转一个指定的角度",
    "ry": "绕 Y 轴转一个指定的角度，能调出任意比例的 0 和 1",
    "u1": "只改相位，不改测出 0 还是 1 的概率",
    "cx": "受控非门：控制位是 1 时才翻转目标位，纠缠就是这么来的",
    "cu1": "受控相位门：两个比特都是 1 时才加相位",
    "swap": "把两个比特的状态整个对调",
    "ccx": "两个控制位同时为 1 时才翻转目标位",
}


# --- 电路布局 ---------------------------------------------------------------


def layout_circuit(circuit: Circuit) -> Dict[str, Any]:
    """把电路排成列，供前端画 SVG。

    与 visualize.circuit_diagram 同一套规则：一个操作放进最早那个"它涉及的比特
    跨度都空闲"的列；跨度指的是首尾比特之间的全部行，因为竖线会穿过中间那些行，
    别的门不能排在那里，否则连线会从门里穿过去。
    """
    frontier = [0] * max(circuit.n_qubits, 1)
    columns: List[List[Dict[str, Any]]] = []

    for op in circuit.ops:
        if isinstance(op, Measure):
            targets = (op.qubit,)
            item = {
                "kind": "measure",
                "label": "M",
                "qubits": [op.qubit],
                "clbit": op.clbit,
                "hint": "测量：把量子状态读成一个确定的 0 或 1，读完叠加就塌掉了",
            }
        else:
            targets = op.qubits
            item = {
                "kind": "gate",
                "name": op.name,
                "label": GATE_LABEL.get(op.name, op.name.upper()),
                "qubits": list(op.qubits),
                "params": [format_param(value) for value in op.params],
                "hint": GATE_HINT.get(op.name, ""),
            }

        span = range(min(targets), max(targets) + 1)
        column = max(frontier[q] for q in span)
        for q in span:
            frontier[q] = column + 1
        while len(columns) <= column:
            columns.append([])
        columns[column].append(item)

    return {
        "n_qubits": circuit.n_qubits,
        "n_clbits": circuit.n_clbits,
        "depth": circuit.depth_estimate(),
        "n_gates": len(circuit.gates()),
        # 图上画了几个方块要对得上文字，否则用户数一遍发现对不上，
        # 只会怀疑是不是自己看错了。测量画成方块，就得单独报出来。
        "n_measures": sum(1 for op in circuit.ops if isinstance(op, Measure)),
        "columns": columns,
    }


# --- 任务表 -----------------------------------------------------------------

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "progress": [], "result": None, "error": None}
    return job_id


def _note(job_id: str, text: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job["progress"].append(text)


def _finish(job_id: str, result: Dict[str, Any]) -> None:
    with JOBS_LOCK:
        JOBS[job_id].update(status="done", result=result)


def _fail(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id].update(status="error", error=message)


# --- 执行 -------------------------------------------------------------------


def _execute(
    circuit: Circuit, target: str, shots: int, job_id: str
) -> Tuple[Dict[str, int], str, bool, Optional[Dict[str, str]]]:
    """跑一次电路，返回 (counts, 后端说明, 是否真机, 回退说明)。

    与 cli.run_circuit 同样的兜底策略：任何一步失败都回落到参考模拟器，
    绝不把用户卡在半路——把人卡住的引导不叫引导。区别只在于进度写进任务表
    而不是打印到终端。

    回退说明是第四个返回值：用户是自己点了"送到真机上跑"才走到这里的，
    如果只把原因写进进度日志，跑完日志一消失，他看到的就只是一个模拟器
    结果，会以为是自己操作错了。谁主动要过真机，谁就有权知道为什么没跑成。
    """
    if target == "refsim":
        # 这行字直接印在结果页最显眼的位置，是零基础用户第一眼看到的东西。
        # "参考模拟器""兜底"都是行话，换成他一眼知道是什么的说法。
        return sample(circuit, shots), "普通电脑模拟出来的结果", False, None

    if target == "spinq_cloud":
        _note(job_id, "正在连接真实的量子计算机…")
        try:
            payload = backend_module.run_spinq_cloud(
                circuit, shots, progress=lambda text: _note(job_id, text)
            )
        except backend_module.SpinQCloudPending as exc:
            _note(job_id, "%s 任务编号 %s 已存下，先用模拟器把流程走完。" % (exc, exc.task_code))
            return sample(circuit, shots), "普通电脑模拟（真机还在排队）", False, {
                "title": "真机还在排队，先给你看模拟器的结果",
                "detail": "%s 任务编号 %s 已经存下来了，排到之后可以在 cloud.spinq.cn 用这个编号查。"
                          % (exc, exc.task_code),
                "hint": "下面这张图是模拟器算的理想结果，和真机跑的是同一个电路。",
            }
        except backend_module.BackendUnavailable as exc:
            _note(job_id, "连不上真机：%s" % exc)
            reason = str(exc)
            # 三种原因给的是三句不同的话。以前不管为什么连不上，都统一告诉用户
            # "真机常常一台都不在线"——账号没配的时候这句话是错的，机器可能好好开着，
            # 是这台电脑压根没去敲门。把错的理由讲得很体贴，比不讲更糟。
            if "凭据" in reason or "LOOMQ_SPINQ" in reason:
                hint = ("这台电脑还没配量旋云的账号，所以根本没连上去，谈不上机器在不在线。"
                        "配好账号之后，同一个按钮就会把这个电路送上真机。")
            elif circuit.n_qubits > 3:
                _note(job_id, "在线的真机装不下这个电路。这是真机的规模限制，不是你的电路有问题。")
                hint = ("在线的真机最多只有几个比特，装不下这个电路。"
                        "这是今天真机的规模限制，不是你的电路有问题。")
            else:
                hint = ("真机只有 2 到 8 个比特，还要维护、校准，随时可能一台都不在线。"
                        "这不是你操作错了，也不是程序坏了——真实量子计算机现在就是这么稀缺。")
            return sample(circuit, shots), "普通电脑模拟（真机没连上，用它顶上）", False, {
                "title": "这次没能跑到真机上，结果来自模拟器",
                "detail": reason,
                "hint": hint,
            }
        except Exception as exc:  # noqa: BLE001
            _note(job_id, "真机运行出了状况：%s: %s" % (type(exc).__name__, exc))
            return sample(circuit, shots), "普通电脑模拟（真机中途出错，用它顶上）", False, {
                "title": "真机中途出了状况，结果来自模拟器",
                "detail": "%s: %s" % (type(exc).__name__, exc),
                "hint": "电路本身没问题，模拟器用同一份电路把结果算了出来。过一会儿可以再试一次真机。",
            }
        return (
            payload["counts"],
            "真实量子计算机 %s（任务编号 %s，可在 cloud.spinq.cn 查到）"
            % (payload["backend"], payload["job_id"]),
            True,
            None,
        )

    try:
        payload = backend_module.execute(circuit, target, shots)
        return payload["counts"], "%s（任务 %s）" % (payload["backend"], payload["job_id"]), False, None
    except backend_module.BackendUnavailable as exc:
        _note(job_id, "%s 已自动改用内置模拟器。" % exc)
        return sample(circuit, shots), "普通电脑模拟（选的那个环境没装上，用它顶上）", False, {
            "title": "选的那个后端没装上，结果来自内置模拟器",
            "detail": str(exc),
            "hint": "内置模拟器不需要装任何东西，算的是同一个电路。",
        }


def _run_job(job_id: str, title: str, qasm: str, explanation: str, target: str,
             shots: int, term: str = "") -> None:
    try:
        circuit = parse(qasm)
    except (QasmError, ValueError) as exc:
        _fail(job_id, "这段电路没能通过检查：%s" % exc)
        return

    try:
        counts, backend_note, on_hardware, fallback = _execute(circuit, target, shots, job_id)
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, "运行失败：%s: %s" % (type(exc).__name__, exc))
        return

    total = sum(counts.values()) or 1
    observed = {key: value / total for key, value in counts.items()}
    width = len(next(iter(counts))) if counts else circuit.n_clbits

    result: Dict[str, Any] = {
        "title": title,
        "term": term,
        "qasm": qasm,
        "explanation": explanation,
        "circuit": layout_circuit(circuit),
        "counts": counts,
        "distribution": observed,
        "shots": total,
        "backend_note": backend_note,
        "on_hardware": on_hardware,
        "legend": bitstring_legend(width),
    }
    # 只在用户主动要过真机的时候解释回退。默认走模拟器的人没提过这个要求，
    # 给他弹一条"真机不可用"只会凭空制造一个他本来没有的问题。
    if fallback and target == "spinq_cloud":
        result["fallback"] = fallback

    if on_hardware:
        # 真机结果被噪声糊过，光看它讲不清电路的意图。拿 refsim 精确算出的理想分布
        # 当基准：结构解释讲电路本该做什么，偏差解释讲真机差在哪。
        ideal = ideal_distribution(circuit)
        result["ideal"] = ideal
        result["explain"] = explain_distribution(ideal)
        result["noise"] = explain_hardware_noise(observed, ideal)
    else:
        result["explain"] = explain_distribution(observed, total)

    _finish(job_id, result)


def _run_ask(job_id: str, question: str, target: str, shots: int) -> None:
    """自然语言路径。模型没配或者没吐出电路时，报错要说人话。"""
    try:
        from . import agent as agent_module

        reply = agent_module.agent_chat(question)
    except Exception as exc:  # noqa: BLE001
        _fail(
            job_id,
            "没能连上模型服务（%s）。可以先点上面三个现成的例子，流程完全一样。" % exc,
        )
        return

    match = re.search(r"OPENQASM\s+2\.0;.*?(?=^\s*```|\Z)", reply, re.DOTALL | re.MULTILINE)
    if not match:
        # 模型没配、答非所问、或者吐了一段话但没有电路，都会落到这里。
        # 无论哪种，用户下一步能做什么必须写在同一句里。
        _fail(
            job_id,
            (reply.strip() or "我暂时没能把这句话变成一个电路。")
            + "\n\n可以换个说法再试一次，比如直接说清要几个比特、想制备什么态；"
            "或者回去点三个现成的例子，流程完全一样。",
        )
        return

    _run_job(job_id, question, match.group(0).strip(), reply.split("```")[0].strip(), target, shots)


# --- HTTP -------------------------------------------------------------------


def _state() -> Dict[str, Any]:
    installed = backend_module.available()
    return {
        "hardware": hardware_ready(),
        "llm": bool(os.environ.get("LOOMQ_LLM_API_KEY")),
        "backends": [
            {"id": "refsim", "name": "普通电脑模拟（内置，不用装任何东西）", "ready": True,
             "note": "精确计算，立刻出结果"},
            {"id": "spinq", "name": "量旋 SpinQit 模拟器（也是普通电脑算）",
             "ready": bool(installed.get("spinq")), "note": ""},
            {"id": "braket", "name": "AWS Braket 模拟器（也是普通电脑算）",
             "ready": bool(installed.get("braket")), "note": ""},
            {"id": "originq", "name": "本源 pyqpanda 模拟器（也是普通电脑算）",
             "ready": bool(installed.get("originq")), "note": ""},
            {"id": "spinq_cloud", "name": "真的量子计算机（量旋云上的实机）", "ready": hardware_ready(),
             "note": "要排队，大约两分钟"},
        ],
        # 顺带把电路结构一起给出去，让卡片上先画一张缩略线路图。
        # 「电路」对没接触过的人是个空词，先看见再点，比点完才第一次看见要好。
        "examples": [
            {
                "key": key,
                "title": value[0],
                "term": value[3],
                "explanation": value[2],
                "circuit": layout_circuit(parse(value[1])),
            }
            for key, value in BUILTIN_EXAMPLES.items()
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "LoomQ"

    def log_message(self, *_args) -> None:  # 静音：终端要留给启动提示
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        if path == "/api/state":
            self._json(_state())
            return

        if path == "/api/job":
            job_id = ""
            if "?" in self.path:
                for chunk in self.path.split("?", 1)[1].split("&"):
                    if chunk.startswith("id="):
                        job_id = chunk[3:]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                payload = dict(job) if job else None
            if payload is None:
                self._json({"error": "没有这个任务"}, 404)
                return
            self._json(payload)
            return

        name = "index.html" if path == "/" else path.lstrip("/")
        target = (WEBUI / name).resolve()
        if not str(target).startswith(str(WEBUI.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if kind.startswith("text/") or kind == "application/javascript":
            kind += "; charset=utf-8"
        self._send(200, target.read_bytes(), kind)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/run":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "请求格式不对"}, 400)
            return

        target = body.get("backend") or "refsim"
        # 后端名要在这里挡住。往下走的话，未知名字会在 backends.execute 里抛
        # ValueError，被 _run_job 的兜底翻译成一句「运行失败」——那是把一个
        # 参数错误说成了运行错误，排查的人会去查错方向。
        if target not in KNOWN_BACKENDS:
            self._json({"error": "没有这个运行环境：%s" % target}, 400)
            return

        # 不能写 `body.get("shots") or DEFAULT_SHOTS`——0 是假值，会被当成"没传"
        # 而悄悄变成 1024，非法输入反倒跑出了结果。
        shots = body.get("shots")
        if shots is None:
            shots = DEFAULT_SHOTS
        if isinstance(shots, bool) or not isinstance(shots, int) or not 1 <= shots <= 20000:
            self._json({"error": "重复测量次数要在 1 到 20000 之间"}, 400)
            return

        job_id = _new_job()

        if body.get("example"):
            entry = BUILTIN_EXAMPLES.get(str(body["example"]))
            if not entry:
                self._json({"error": "没有这个例子"}, 400)
                return
            args = (job_id, entry[0], entry[1], entry[2], target, shots, entry[3])
            threading.Thread(target=_run_job, args=args, daemon=True).start()
        elif body.get("question"):
            question = str(body["question"]).strip()[:300]
            threading.Thread(target=_run_ask, args=(job_id, question, target, shots), daemon=True).start()
        else:
            self._json({"error": "没说要跑什么"}, 400)
            return

        self._json({"job": job_id})


def serve(port: int, open_browser: bool) -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("LoomQ 已经跑起来了：%s" % url)
    print("在浏览器里打开这个地址就能用。按 Ctrl+C 退出。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")
    finally:
        httpd.server_close()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="LoomQ 网页入口：不懂量子也能跑量子程序")
    parser.add_argument("--port", type=int, default=8899, help="监听端口，默认 8899；被占用时自动换一个")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args(argv)

    try:
        return serve(args.port, not args.no_browser)
    except OSError:
        # 端口被占是最常见的启动失败，不该让用户自己去查怎么办。
        print("端口 %d 被占用了，换一个空闲端口重试。" % args.port)
        return serve(0, not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
