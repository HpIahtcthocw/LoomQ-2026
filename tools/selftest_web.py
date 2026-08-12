"""网页入口的离线自测。

不依赖网络、不依赖模型服务、不依赖真机——评委在一台裸机上也应该能验证
这个入口是活的。全部走 refsim，真机与自然语言两条路径只测"失败时是否
优雅降级"，不测它们能不能连上。

跑法：
    cd starter_kit && python ../tools/selftest_web.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "starter_kit"))

from loomq import web  # noqa: E402
from loomq.ir import Circuit, Gate, Measure  # noqa: E402
from loomq.qasm2_parser import parse  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print("[PASS] %s" % name)
    else:
        FAILED += 1
        print("[FAIL] %s%s" % (name, ("  — " + detail) if detail else ""))


def section(title: str) -> None:
    print("\n=== %s ===\n" % title)


# --- 电路布局 ---------------------------------------------------------------


def test_layout() -> None:
    section("电路布局")

    bell = parse(web.BUILTIN_EXAMPLES["1"][1])
    layout = web.layout_circuit(bell)
    check("贝尔态 2 比特", layout["n_qubits"] == 2)
    labels = [[item["label"] for item in col] for col in layout["columns"]]
    check("三列：H / CX / 两个 M", labels == [["H"], ["⊕"], ["M", "M"]], str(labels))
    check("CX 占两个比特", layout["columns"][1][0]["qubits"] == [0, 1])
    check("每个门都带一句人话", all(
        item["hint"] for col in layout["columns"] for item in col))

    ghz = parse(web.BUILTIN_EXAMPLES["2"][1])
    g = web.layout_circuit(ghz)
    check("GHZ 3 比特", g["n_qubits"] == 3)
    check("GHZ 的两个 CX 不挤在同一列",
          not any(len([i for i in col if i.get("name") == "cx"]) > 1 for col in g["columns"]))

    # 跨度占用：q0-q2 的门跨过 q1，q1 上的门不能排进同一列，否则连线穿过门
    spanning = Circuit(n_qubits=3, n_clbits=0, ops=[
        Gate("ccx", (0, 1, 2)), Gate("h", (1,)),
    ])
    s = web.layout_circuit(spanning)
    check("跨度中间的比特被占用", len(s["columns"]) == 2, str(s["columns"]))

    # 同一比特上的先后顺序不能被打乱
    seq = Circuit(n_qubits=2, n_clbits=0, ops=[Gate("h", (0,)), Gate("x", (0,)), Gate("h", (1,))])
    q = web.layout_circuit(seq)
    check("同比特的门保持先后", q["columns"][0][0]["label"] == "H" and q["columns"][1][0]["label"] == "X")
    check("无关比特可以并到第一列", len(q["columns"][0]) == 2, str(q["columns"][0]))

    empty = web.layout_circuit(Circuit())
    check("空电路不炸", empty["n_qubits"] == 0 and empty["columns"] == [])

    params = Circuit(n_qubits=1, n_clbits=1, ops=[Gate("ry", (0,), (1.5707963,)), Measure(0, 0)])
    p = web.layout_circuit(params)
    check("参数被格式化成字符串", p["columns"][0][0]["params"] == ["1.5707963"],
          str(p["columns"][0][0]["params"]))
    check("测量带经典位下标", p["columns"][1][0]["clbit"] == 0)


# --- 状态 -------------------------------------------------------------------


def test_state() -> None:
    section("运行环境")
    state = web._state()
    check("refsim 永远可用", any(b["id"] == "refsim" and b["ready"] for b in state["backends"]))
    check("列出五个后端", len(state["backends"]) == 5)
    check("三个内置示例", len(state["examples"]) == 3)
    check("示例都带标题与解释",
          all(e["title"] and e["explanation"] for e in state["examples"]))
    # 卡片上那张缩略线路图全靠这个字段，掉了的话页面不报错，只是安静地少一张图
    check("示例都带电路结构（卡片缩略图要用）",
          all(e.get("circuit", {}).get("columns") for e in state["examples"]))
    check("hardware/llm 是布尔", isinstance(state["hardware"], bool) and isinstance(state["llm"], bool))


def test_fallback() -> None:
    """真机没跑成时必须当面交代原因。

    这条判据是从实测反馈里长出来的：云上一台在线的机器都没有时，
    回退到模拟器本身是对的，但如果只把原因写进进度日志，用户看到的
    就是一个和普通模拟器结果一模一样的页面，只会以为是自己按错了。
    """
    section("真机回退的交代")
    bell = parse(web.BUILTIN_EXAMPLES["1"][1])
    job_id = web._new_job()
    counts, note, on_hw, fallback = web._execute(bell, "refsim", 64, job_id)
    check("refsim 正常出结果", sum(counts.values()) == 64 and not on_hw)
    check("没要过真机就不谈回退", fallback is None)

    # 把 execute 换成必定失败的桩，才能在不装任何第三方 SDK 的机器上
    # 稳定地走一遍回退分支——这条路平时只有在别人机器上才会被踩到。
    original = web.backend_module.execute

    def unavailable(*_args, **_kwargs):
        raise web.backend_module.BackendUnavailable("自测桩：这个后端没装")

    web.backend_module.execute = unavailable
    try:
        counts, note, on_hw, fallback = web._execute(bell, "braket", 64, job_id)
    finally:
        web.backend_module.execute = original
    check("后端没装也能兜住", sum(counts.values()) == 64 and not on_hw)
    check("兜底时说明自己是模拟器", "模拟器" in note)
    check("兜底带上原因", bool(fallback and fallback["title"] and fallback["hint"]))

    html = (web.WEBUI / "index.html").read_text(encoding="utf-8")
    for node in ("r-fallback", "r-fallback-title", "r-fallback-hint", "r-fallback-detail"):
        check("页面留了 %s 的位置" % node, 'id="%s"' % node in html)

    check("界面上的后端都在白名单里",
          all(b["id"] in web.KNOWN_BACKENDS for b in web._state()["backends"]))


# --- HTTP -------------------------------------------------------------------


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=20) as response:
        return response.status, response.read(), response.headers.get("Content-Type", "")


def post(base: str, payload: dict):
    request = urllib.request.Request(
        base + "/api/run", json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def poll(base: str, job_id: str, tries: int = 80) -> dict:
    import time

    for _ in range(tries):
        _, body, _ = get(base, "/api/job?id=" + job_id)
        payload = json.loads(body)
        if payload["status"] != "running":
            return payload
        time.sleep(0.25)
    return {"status": "timeout"}


def test_http() -> None:
    section("HTTP 接口")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    try:
        status, body, kind = get(base, "/")
        check("首页可访问", status == 200 and b"<title>" in body)
        check("首页声明 UTF-8", "charset=utf-8" in kind, kind)

        for asset in ("/app.js", "/styles.css"):
            status, body, _ = get(base, asset)
            check("静态资源 %s" % asset, status == 200 and len(body) > 500)

        status, body, _ = get(base, "/api/state")
        check("状态接口返回 JSON", status == 200 and json.loads(body)["backends"])

        # 目录穿越：webui 之外的任何文件都不能被读到
        leaked = False
        for probe in ("/../web.py", "/..%2fweb.py", "/../../requirements.txt"):
            try:
                code, payload, _ = get(base, probe)
                if code == 200 and b"import" in payload:
                    leaked = True
            except urllib.error.HTTPError:
                pass
        check("拦住目录穿越", not leaked)

        status, payload = post(base, {"example": "1", "backend": "refsim", "shots": 512})
        check("提交内置示例", status == 200 and "job" in payload, str(payload))
        result = poll(base, payload["job"])
        check("任务跑完", result["status"] == "done", str(result)[:160])

        data = result.get("result") or {}
        for field in ("title", "qasm", "circuit", "counts", "distribution",
                      "shots", "backend_note", "on_hardware", "legend", "explain"):
            check("结果含 %s" % field, field in data)
        check("shots 与请求一致", data.get("shots") == 512, str(data.get("shots")))
        check("counts 总和等于 shots", sum(data["counts"].values()) == 512)
        check("贝尔态只有 00 和 11", set(data["counts"]) <= {"00", "11"}, str(data["counts"]))
        check("模拟器不标真机", data["on_hardware"] is False)
        check("模拟器不给噪声解释", "noise" not in data)
        check("解释是中文且成句", len(data["explain"]) > 20 and "。" in data["explain"])
        check("解释里没有 markdown 记号", "**" not in data["explain"])

        status, payload = post(base, {"example": "9"})
        check("拒绝不存在的示例", status == 400, str(payload))
        status, payload = post(base, {"example": "1", "shots": 0})
        check("拒绝非法 shots", status == 400, str(payload))
        status, payload = post(base, {"example": "1", "shots": 999999})
        check("拒绝超大 shots", status == 400, str(payload))
        status, payload = post(base, {})
        check("拒绝空请求", status == 400, str(payload))
        status, payload = post(base, {"example": "1", "backend": "no_such_backend"})
        check("拒绝不存在的运行环境", status == 400, str(payload))

        try:
            get(base, "/api/job?id=nosuchjob")
            check("未知任务返回 404", False)
        except urllib.error.HTTPError as exc:
            check("未知任务返回 404", exc.code == 404)

        # 没配模型时自然语言必须给人话，而不是抛栈
        saved = os.environ.pop("LOOMQ_LLM_API_KEY", None)
        try:
            status, payload = post(base, {"question": "让两个比特纠缠起来"})
            result = poll(base, payload["job"])
            check("缺模型时任务以错误收场", result["status"] == "error")
            check("缺模型时给出下一步", "例子" in (result.get("error") or ""),
                  (result.get("error") or "")[:120])
            check("缺模型时不吐堆栈", "Traceback" not in (result.get("error") or ""))
        finally:
            if saved is not None:
                os.environ["LOOMQ_LLM_API_KEY"] = saved

        # 后端装不上时要回落到 refsim，而不是把用户卡死
        status, payload = post(base, {"example": "1", "backend": "braket", "shots": 128})
        result = poll(base, payload["job"])
        check("后端不可用时仍能出结果", result["status"] == "done", str(result)[:160])
        check("回落后 counts 仍完整", sum(result["result"]["counts"].values()) == 128)
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- 前端资源 ---------------------------------------------------------------


def test_assets() -> None:
    section("前端资源")
    ui = web.WEBUI
    check("webui 目录存在", ui.is_dir())
    for name in ("index.html", "app.js", "styles.css"):
        check("有 %s" % name, (ui / name).is_file())

    html = (ui / "index.html").read_text(encoding="utf-8")
    js = (ui / "app.js").read_text(encoding="utf-8")

    check("没有外链 CDN", "http://" not in html.replace("http://www.w3.org", "")
          and "https://" not in html)
    check("离线可跑：不引外部脚本", 'src="app.js"' in html)

    # JS 里 $('x') 引用的每个 id 都必须在 HTML 里存在，否则运行时静默变成 null
    import re

    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r"\$\('([^']+)'\)", js))
    missing = sorted(used - ids)
    check("JS 引用的 id 都在 HTML 里", not missing, "缺少 " + ", ".join(missing))

    asks = re.findall(r"^\s+ask: '", js, re.MULTILINE)
    check("三道验收题", len(asks) == 3, "找到 %d 道" % len(asks))
    check("每道题都有正确答案", len(re.findall(r"^\s+right: \d", js, re.MULTILINE)) == 3)
    check("每道题都写了解析", len(re.findall(r"^\s+why: '", js, re.MULTILINE)) == 3)

    # 收尾那段话曾经写死"它被送进一台真的量子计算机"。真机会整片下线，那天所有人
    # 都会在最后读到一段自己没做过的事。全篇的收尾陈词上撒一个能被当场戳穿的谎，
    # 前面攒的信任一次赔光——所以这两段必须留空、由 paintCoda 按实际经历填。
    for node in ("coda-did", "coda-blunt"):
        check("%s 在 HTML 里是空的" % node, 'id="%s"></p>' % node in html)
    coda = re.search(r"function paintCoda\(\)\s*\{(.*?)\n\}", js, re.S)
    check("有 paintCoda", bool(coda))
    if coda:
        body = coda.group(1)
        check("收尾按是否真跑过真机分开写", "state.ranHardware" in body)
        check("收尾按是否自己提过需求分开写", "state.askedOwn" in body)
        check("没跑真机时不说送进了真机", "没走成" in body)


def main() -> int:
    print("=== LoomQ 网页入口离线自测（不联网、不用模型、不碰真机）===")
    test_layout()
    test_state()
    test_fallback()
    test_http()
    test_assets()
    print("\n%d 通过，%d 失败" % (PASSED, FAILED))
    if FAILED:
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    from loomq.cli import _ensure_utf8_output

    _ensure_utf8_output()
    raise SystemExit(main())
