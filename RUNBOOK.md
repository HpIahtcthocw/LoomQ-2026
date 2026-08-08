# RUNBOOK · 在另一台机器上跑起来

本手册假设：代码从这台机器搬过去，**所有安装与运行都在目标机器上进行**。
按顺序执行，每步都有明确的通过判据；某步不通过就停在那里，不要往下走。

---

## 步骤 0 · 先在 GitHub 上 fork

提交必须是**你自己账号下的 fork**，且最终提交 Issue 必须由同一账号创建。该账号的用户名就是 Team ID。

1. 打开 https://github.com/QAIDAO/LoomQ-2026，点 Fork
2. 记下你的 GitHub 用户名，后面所有 `<TEAM_ID>` 都填它

把本地代码指向你的 fork：

```bash
git remote set-url origin https://github.com/<TEAM_ID>/LoomQ-2026.git
git remote add upstream https://github.com/QAIDAO/LoomQ-2026.git
```

**通过判据**：`git remote -v` 里 origin 指向你自己的账号。

---

## 步骤 1 · Python 3.10（这一步最容易卡住）

**必须恰好是 3.10，高了低了都不行。** 这不是照抄题面推荐，是查 PyPI 实际发布情况得出的：

| 包 | 有 wheel 的 Python | 结论 |
|---|---|---|
| `spinqit` 0.2.4 | cp38 / cp39 / **cp310** | 上限 3.10，且**没有 sdist**，没法源码编译绕过 |
| `amazon-braket-sdk` 最新（1.125） | 纯 `py3`，但 `requires_python >=3.11` | 下限 3.11 |
| `pyqpanda` 3.8.5 | cp38 … cp311 | 上限 3.11 |

中间两行**互相冲突**：最新版 Braket 要 3.11 以上，而 spinqit 到 3.10 为止。
解法是把 Braket 降版本，具体钉到哪一版见步骤 2——那里还有一层更深的冲突。
所以 3.10 是唯一可行的交集。

用 3.11/3.12 的话，`pip install spinqit` 会报"找不到满足要求的版本"，
而报错信息不会告诉你根因是版本——很容易误以为是网络或镜像源问题。

**本机已装好**（2026-08-07）：`py -3.10` 指向 Python 3.10.11，与原有的 3.12 并存，
用 `winget install --id Python.Python.3.10 --scope user` 装的，没有动 3.12。

```bash
# macOS / Linux
python3.10 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
```

**通过判据**：

```bash
python --version    # 必须输出 Python 3.10.x
```

不是 3.10 就先去装 3.10，别继续。

---

## 步骤 2 · 装依赖

**直接用已经锁好的文件装，不要自己拼命令**：

```bash
pip install --upgrade pip
pip install -r starter_kit/requirements.txt
```

`starter_kit/requirements.txt` 已经填好并在干净的 3.10 环境里 dry-run 验证过能解出来。
文件头部写了每个关键版本为什么是那个值。这里只讲为什么不能自己随手装。

### 三层缠在一起的版本约束

天真的做法 `pip install amazon-braket-sdk spinqit` 会连撞三堵墙：

**第一层，Python 版本。** 最新 Braket 要 ≥3.11，spinqit 只到 3.10 → 用 3.10。

**第二层，antlr 硬钉冲突。** 这层最阴：

```
spinqit 0.2.4                          → antlr4-python3-runtime==4.9.2
amazon-braket-default-simulator ≥1.28  → antlr4-python3-runtime==4.13.2
```

两边都是 `==`，无法调和，pip 直接报 `ResolutionImpossible`。而且这不只是元数据打架——
ANTLR 4.10 改了序列化 ATN 的格式，4.9 生成的解析器在 4.13 运行时上会崩，
所以**不能靠强装新版 antlr 绕过**。
唯一出路是把 `amazon-braket-default-simulator` 停在 **1.27.0**，它是最后一个用 4.9.2 的版本。

**第三层，声明的下界不等于真实兼容。** 这层最容易踩：
`amazon-braket-sdk==1.100.0` 声明自己只要 `default-simulator>=1.27.0`，看着能配 1.27.0，
装完却在 import 时炸：

```
ImportError: cannot import name 'VerbatimBoxDelimiter'
             from 'braket.default_simulator.openqasm.interpreter'
```

那个符号 1.27.0 里根本没有。**依赖声明是宽的，实际代码是紧的。**
解法是按发布日期配对：`default-simulator` 1.27.0 发布于 2025-08-13，
同日发布的 `amazon-braket-sdk==1.97.0` 才是真正配套的一版（1.28.0 在 8-18 就换成 antlr 4.13.2 了）。

最终可用组合：

```
Python 3.10.11
amazon-braket-sdk==1.97.0
amazon-braket-default-simulator==1.27.0
antlr4-python3-runtime==4.9.2
spinqit==0.2.4
```

### Windows 还要装 VC++ 运行库

`spinqit` → `igraph` 的 C 扩展依赖 `MSVCP140.dll`，这个 DLL 不随 Python 附带：

```powershell
winget install --id Microsoft.VCRedist.2015+.x64
```

不装的话报错长这样，而且**极具误导性**：

```
ImportError: DLL load failed while importing _igraph: 找不到指定的模块。
```

它说的是 `_igraph`，但 `_igraph.pyd` 明明就在那儿——缺的是它依赖的 `MSVCP140.dll`。
更迷惑的是：先 `import torch` 再 `import igraph` 就能成功，因为 torch 自带一份
`msvcp140.dll` 并把自己的 lib 目录加进了 DLL 搜索路径。于是同一个 `import igraph`
时好时坏，全看导入顺序。别去猜顺序，装运行库。

Linux 容器没这个问题，官方 Dockerfile 用的 `python:3.10-slim` 自带 glibc 与 libstdc++。

### 通过判据

不要先 import torch，就按最朴素的顺序试，能过才算真的好：

```bash
python -c "import spinqit; import braket; from braket.devices import LocalSimulator; print('ok')"
```

**已锁定的 `requirements.txt`** 是全量 freeze（65 个包 + 显式钉住的 `setuptools`），
已逐包核对过 PyPI：全部在 linux/cp310 上都有现成 wheel，只有 `antlr4-python3-runtime`
和 `python-constraint` 需从 sdist 构建，二者都是纯 Python，不需要编译器，slim 镜像足够。
改动这个文件后务必重跑一次可解性验证：

```bash
py -3.10 -m venv _verify && _verify/Scripts/python -m pip install --dry-run -r starter_kit/requirements.txt
```

`pyqpanda`（本源，L1 进阶项）**先不要装**。它不影响资格线，而且会引入新的版本约束
（它只有 cp38–cp311 的 wheel，虽然涵盖 3.10，但依赖树可能与现有锁定打架）。
等步骤 6 全过了再回来处理，装之前先备份 `requirements.txt`。

---

## 步骤 3 · 官方示例先跑通

在自己的代码之前，先确认 SDK 本身是好的：

```bash
cd starter_kit
python examples/run_braket.py
python examples/run_spinq.py
cd ..
```

**通过判据**：两个都打印出 counts，且 spinq 那个**没有**出现 `[Warning] 未检测到 spinqit 模块`。
如果出现那句警告，说明 spinqit 没装成功，回步骤 2。

---

## 步骤 4 · 离线自测（不需要 SDK，不需要 API key，纯标准库）

六条命令，加起来约 10 秒：

```bash
python tools/selftest_transpile.py     # 转译层，37 项
python tools/selftest_decompose.py     # 门分解数值验证，22 项
python tools/selftest_agent.py         # L2 Agent，42 项，用本地假端点
python tools/selftest_explain.py       # 结果解释文案，35 项
python tools/gen_circuits.py           # 重新生成隐藏电路回归集 + 灵敏度检验
python tools/run_regression.py         # 回归集跑一遍参考模拟器
```

**通过判据**：第 1、2、3、4、6 条最后一行都输出 `全部通过`，无任何 `[FAIL]`；
第 5 条把 6 个电路与理想分布写进 `regression/`，且末尾报告
「相位错误探测器 4 个，位序错误探测器 4 个」——两个数都不能是 0。

这五条在本机（Windows、无 SDK、无 key）已全部通过。在目标机器上重跑是为了确认
代码搬运完整、没有文件缺失、没有换行符或编码损坏。

顺手确认零基础入口也是好的：

```bash
cd starter_kit && python -m loomq.cli --demo && cd ..
```

**通过判据**：依次打印三个任务（贝尔态 / GHZ-3 / Grover-3）的电路图、分布与解释，
退出码 0。Grover 的 `111` 应占约 78%。若终端出现乱码，说明字符集降级没生效，
把终端编码设为 UTF-8（Windows：`chcp 65001`）后重试。

---

## 步骤 5 · 位序标定（最关键的一步，别跳）

```bash
python tools/probe_bitorder.py
```

脚本会用非对称探针电路（只翻转 `q[0]`，2 比特下正确结果必须是 `"01"`）判断每个后端的原生位序。

**为什么必须单独做**：公开电路 bell 是 `{"00","11"}`、ghz3 是 `{"000","111"}`，
整串反转后分布完全不变。位序写错时步骤 6 的公开自测**依然会全 PASS**。

而且不能指望"标志性电路"替你抓到它——**GHZ-5 和 Grover-3 对位序错误同样免疫**
（实测反转后保真度均为 1.0000：GHZ 原因同 bell；Grover 的目标态 `111` 回文，
其余七态概率相等，反转只是把它们互相置换）。整个回归集里只有 QFT-4 和三个随机电路
能抓住位序错误。所以位序要**先标定、再回归**，顺序不能反。

**通过判据**：每个后端都给出明确结论。然后按结论修改
`starter_kit/loomq/counts.py` 里的 `NATIVE_MATCHES_CONTEST`：

```python
NATIVE_MATCHES_CONTEST = {
    "spinq": True,      # 按脚本结论填
    "braket": False,    # Braket 大概率是 False（原生以 q[0] 为最左字符）
    "originq": None,
}
```

改完**重跑一次探针**，这次三个后端都应报"一致"。

如果脚本报"两个探针结论不一致"，那不是位序问题，去查 `measure` 映射和补零宽度。

---

## 步骤 6 · 公开自测：拿下 L1 入门档（评奖资格线）

```bash
cd starter_kit
python evaluator.py --level l1 --target spinq,braket --json-out report.json
cd ..
```

**通过判据**：4 个 case（bell/ghz3 × spinq/braket）全部 PASS，退出码 0，
`summary` 显示 `"failed": 0`。

**到这里你已经跨过评奖资格线（12 分）。** 立刻 commit 并 push，先把这个状态存下来：

```bash
git add -A
git commit -m "L1 入门档：spinq + braket 双后端跑通公开电路"
git push origin main
```

### 可能踩到的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `counts total must equal shots exactly` | SDK 返回的是概率不是计数 | `coerce_to_counts` 已处理；若仍报错，打印原始返回值贴给我 |
| Braket 报未知门 | Braket 方言不认 stdgates 门名 | 改 `emitters.py` 的 `BRAKET_DIALECT_NAMES`，**不要动** `STDGATES_NAMES`（那份是交给评测器的） |
| `bit_order must be little` | 结果构造绕过了 `build_result` | 所有结果必须走 `counts.build_result` |
| fidelity 落在 0.5 左右 | 门被翻错或位序问题 | 先跑步骤 5 的探针 |

---

## 步骤 7 · L1 进阶（可选，12 → 35 分）

两件事，按顺序：

**7a. 加第三个平台 originq**

```bash
pip install pyqpanda
python tools/probe_bitorder.py --target originq
cd starter_kit && python evaluator.py --level l1 --target spinq,braket,originq && cd ..
```

`backends.run_originq` 里的 pyqpanda 调用**尚未实测**，报错把完整堆栈贴给我。装不上就跳过。

**7b. 隐藏电路回归**

评测的 8 个电路里只有 bell/ghz3 公开，其余（GHZ-5、QFT-4、Grover-3、Random×3）
要自己造来验证。**这部分本机已经做完了**：`tools/gen_circuits.py` 生成电路，
理想分布由 `loomq/refsim.py`（纯标准库参考态矢模拟器）精确算出，存在 `regression/`。
在目标机器上要做的是把这些电路喂给真实后端：

```bash
python tools/run_regression.py --target spinq,braket
```

**通过判据**：每个组合保真度 ≥ 0.97，最后一行 `全部通过`，退出码 0。
退出码 2 表示参考模拟器过了但真实后端一个都没跑起来，那不算完成。

默认 32768 shots 不是随手定的。保真度自带采样噪声，态数越多噪声越大，
`random_c` 有 32 个态——**8192 shots 下正确的实现也会偶发跌破 0.97**
（30 个随机种子实测最低 0.9725，距阈值仅 0.0025）。32768 下最差余量约 0.016。
假警报比没测更糟，它会把你送上错误的排查方向。

这一步真正的风险是**受控相位门的语义**。本机已数值验证：

> 把 `cu1` 的 5 门分解里三个 `u1` 换成 `rz` 是**安全的**（只差全局相位，标量与一切算子交换）。
> 真正致命的是把 `cu1` 实现成**受控 rz**（`crz`）——相位挂到受控分支上变成相对相位，
> QFT-4 保真度会掉到 **0.7241**。而 bell/ghz3 不含 `cu1`，前面所有步骤都不会暴露它。

所以接后端时要认准语义是 `diag(1,1,1,e^{iθ})`：Braket 的 `cphaseshift`、
OpenQASM 3 的 `cp` 都对；OriginIR 契约里写的 `CR` **是否为 cu1 语义需要实测确认**，
方法是把只含一个 `cu1(pi/2)` 的电路跑一遍，与 `refsim` 的理想分布对比。

失败模式与含义（脚本失败时也会打印这张表）：

| 现象 | 几乎必定是 |
|---|---|
| 只有 `qft4` 挂 | 受控相位门语义（`cu1` 被当成 `crz`） |
| `qft4` 和 `random_*` 挂，`ghz5`/`grover3` 过 | **位序**——后两者分布回文对称，对位序错误免疫 |
| 全挂且保真度接近 0 | `measure` 映射或补零宽度 |

---

## 步骤 7.5 · 量旋云真机（+10 分，本机已做完）

**这一步本机已经全部完成，两个平台的证据都已入库。** 换机后除非要重跑，
否则只需确认 `starter_kit/evidence/files/` 里的四个文件在，以及
`python tools/check_hardware_evidence.py` 输出三条标准全达标。

要在新机器上重跑的话：

```bash
python tools/make_spinq_key.py            # 生成 PEM 私钥，打印公钥
# 把打印出的 ssh-rsa 那一整行贴到 cloud.spinq.cn 账号设置的 SSH 公钥处
setx LOOMQ_SPINQ_USERNAME 20260808
setx LOOMQ_SPINQ_KEYFILE  %USERPROFILE%\.spinq\spinq_cloud_rsa
python tools/run_spinq_cloud.py platforms   # 看哪些真机在线，不提交任务
python tools/run_spinq_cloud.py calibrate --qubits 2   # 标定位序，必做
python tools/run_spinq_cloud.py run --qasm starter_kit/circuits/bell.qasm --platform gemini_vp
```

**密钥格式是唯一的硬坑。** 量旋 SDK 用 `RSA.importKey` 读私钥，只认 PEM
（`-----BEGIN RSA PRIVATE KEY-----`）。而 OpenSSH 7.8 之后 `ssh-keygen` 默认输出
自有格式（`-----BEGIN OPENSSH PRIVATE KEY-----`），`importKey` 会直接抛异常。
`make_spinq_key.py` 用 PyCryptodome 直接生成以绕开这件事，并会跑一遍签名自检；
真要用 `ssh-keygen` 必须显式加 `-m PEM`。私钥写在 `~/.spinq/`，**刻意放在仓库外**。

**判定标准与直觉相反，别用错。** 真机这 10 分**只查主峰命中，不查保真度**，
题面原话是「真机允许噪声，只查主峰命中」。0.97 那个阈值属于 L1 语义等价的 35 分，
跑在无噪声模拟器上。我们真机实测保真度只有 0.7743（Bell）与 0.6233（GHZ-3），
但 Top-2 主导态完全正确、分离度 5.3 / 5.7 倍，三条标准全过。
**把真机保真度往上优化一分不得**，别在这上面花时间。

真正的风险是第 3 条：评委会抽样登录控制台复核 `job_id`，
所以**任务页截图必须补进 `evidence/files/`**，这一步只能人工做。

接真机需要改中间层的三处（都从 spinqit 0.2.4 源码确认，已实现在 `backends.py`）：

1. **云端拒绝显式 measure。** `assemble` 见到 MEASURE 节点直接抛
   `CircuitOperationValidationError`，测量由平台在末尾自动完成。
   `_measure_free_qasm` 在 IR 层摘掉全部 Measure，但**保留 creg 声明**——
   编译器要靠它确定 `ir.dag['cnum']`。
2. **结果位宽按 `n_qubits` 而非 `n_clbits`。** 部分测量只在 `sqc_25_vp` 与
   `simulator` 上支持，其余平台一律返回全部比特。
3. **官方 `execute()` 会无限期阻塞**（内部 `hanging=True, timeout=None`，每 5 秒轮询）。
   改为自己 `submit_task` + `get_task_result`，能设超时，且**超时也保留 task_code**
   ——真机证据要的就是这个可溯源编号，任务还在排队时不该丢掉它。

位序**必须为真机独立标定，不能沿用模拟器结论**：云端自动测量是另一条代码路径。
2026-08-08 实测（`gemini_vp` 任务 `G-260808-0001`、`triangulum_vp` 任务 `S-260808-0001`）：
非对称探针 `x q[0]` 在 2 比特主峰落 `10`、3 比特主峰落 `100`，即原生位串以 `q[0]` 为
最左字符，与大赛约定相反，`NATIVE_MATCHES_CONTEST["spinq_cloud"] = False`。
两个位宽结论一致，排除了「自动测量整体错位」这个竞争解释。

零基础用户也能用真机，这是专项奖标准的硬要求（「在真实量子机上完成人生第一个实验」）：

```bash
cd starter_kit && python -m loomq.cli          # 配好凭据后开场会提示可用真机
# 交互中输入 real 切到真机，输入 1 跑贝尔态，再输入 sim 切回模拟器对照
```

---

## 步骤 8 · L2（30 分，专项奖主战场）

**已接真实模型跑通**（`deepseek-v4-flash`，对标组 24/24）。`submission.yaml` 里的
`l2: true` 与 `network.required_for_l2: true` 已经改好，不用再动。

先明确这一级的分值结构，它比看上去要紧：客观分 20（12 个隐藏 case，通过率 × 20），
交互体验分 10，而**交互分只在客观分 ≥ 12 时才计入**。也就是 12 个 case 至少要过 8 个，
否则做得再好的 CLI 也一分不得。专项奖的资格因此直接压在这里。

配环境变量。自备 DeepSeek key，**组委会赛前不提供任何额度**。
`l2_policy.json` 写明正式评分用 `deepseek-v4-flash`，而 `llm_client.py` 只在模型名
**恰好等于**这个字符串时才发送 `thinking: {"type":"disabled"}`——用 `deepseek-chat`
之类的别名跑出来的结果不能代表正式环境：

```bash
# macOS / Linux
export LOOMQ_LLM_BASE_URL=https://api.deepseek.com/v1
export LOOMQ_LLM_API_KEY=<你自己的 key>
export LOOMQ_LLM_MODEL=deepseek-v4-flash
export LOOMQ_LLM_TIMEOUT_SECONDS=120
```

```powershell
# Windows PowerShell
$env:LOOMQ_LLM_BASE_URL = "https://api.deepseek.com/v1"
$env:LOOMQ_LLM_API_KEY  = "<你自己的 key>"
$env:LOOMQ_LLM_MODEL    = "deepseek-v4-flash"
$env:LOOMQ_LLM_TIMEOUT_SECONDS = "120"
```

**key 只走环境变量，绝不写进文件。** 仓库是公开的，key 一旦提交就会被 GitHub 的
secret scanning 抓到并可能被盗用；`.gitignore` 里已经兜底屏蔽了 `.env` / `*.key` /
`secrets.json`，但最稳妥的还是压根不落盘。

三条命令，从轻到重：

```bash
cd starter_kit
python -m loomq.cli --ask "让三个比特全都纠缠起来"   # 看链路通不通
python evaluator.py --level l2                        # 官方契约自测
cd ..
python tools/selftest_l2_live.py                      # 真实通过率压测
```

**通过判据**：CLI 能生成电路并显示自验通过；`evaluator.py --level l2` PASS；
`selftest_l2_live.py` 对标组 24/24、折算客观分 ≈ 20/20。

注意 `evaluator.py --level l2` **只有 1 个公开用例，而且只校验"回复里能抽出可解析的
QASM"，不校验电路对不对**——它过了完全不代表能得分。真正能估通过率的是
`selftest_l2_live.py`：24 个对标用例覆盖题面三类任务的措辞、比特数、目标态变体，
判定口径与官方一致（同一个抽取正则、refsim 无噪声模拟、Hellinger 阈值 0.97、
选后端答案集由 `backend_capabilities.json` 按约束算出而非写死）。另有 12 个进阶用例
（加 `--stretch`）刻意超出题面难度，只用来找边界，不参与折算。

Agent 内部是"生成 → 自验 → 不对就重试"的闭环，最多三次，偶发抽风不会直接变成失败；
三次都不过会如实告知但仍把最好的一版交出来，不让用户空手而归。这里有个**反直觉的坑**：
自验基准取错时，这个闭环会把正确答案改成错的（详见 `PLAN.md` 第四节陷阱 4）。所以
族名标签与模型声明的分布冲突时，代码不擅自判决，而是带着用户原话做一次定向裁决。

调不通时按这个顺序查：

| 现象 | 处理 |
|---|---|
| 报缺少环境变量 | 变量名拼写，注意是 `LOOMQ_LLM_*` 前缀 |
| HTTP 404 / 模型不存在 | base URL 少了 `/v1`，或该 key 下没开通这个模型；跑 `python tools/probe_agent_payload.py` 看原始返回 |
| 超时 | 把 `LOOMQ_LLM_TIMEOUT_SECONDS` 调到 180 |
| 模型返回的 QASM 抽不出来 | 看 `agent.py` 的 `_extract_json`；官方正则要求完整程序，不能夹解释文字 |
| 某个用例总是失败 | `python tools/probe_agent_payload.py "<那句 prompt>"` 会打印模型原始 payload 与自验依据，能直接看出是模型错了还是我们的基准错了 |
| 自验总是三次都失败 | 大概率是模型太弱，换更强的 model；`selftest_agent.py` 能证明是模型问题不是代码问题 |

---

## 步骤 9 · 提交（截止 2026-08-25 12:00 UTC+8）

```bash
python starter_kit/prepare_submission.py --team-id <TEAM_ID>
```

预检会确认工作区干净、HEAD 已 push、fork 所有者与 Team ID 一致，并输出 fork 地址和 40 位 commit SHA。

然后在 **上游** `QAIDAO/LoomQ-2026` 创建「LoomQ 最终提交」Issue，填入预检输出的内容。

**通过判据**：Issue 拿到 `submission:accepted` 标签，并出现包含 commit、归档 SHA-256
和 Artifact ID 的自动回执。**只创建 Issue 或只通过本地预检都不算提交成功。**

要更新提交就改代码、push、**新建一个 Issue**，不要编辑旧的。截止前最后一次通过校验的提交生效。

几条硬规则：

- 截止时间以 GitHub 服务器记录的 Issue `created_at` 为准，不看 commit 时间也不看你的本地时间
- 归档不得超过 100 MiB，大视频用稳定只读链接
- 不要提交任何 API Key、Token、Cookie
- 申报真机、L2 交互、工程产品化或 Bonus，填 `starter_kit/evidence/README.md`，附件放 `evidence/files/`

**别卡最后一小时。** 8/24 就该完成一次完整的有效提交，8/25 只做增量替换。

---

## 附一：当前代码状态

本机（Windows）现在已装好 Python 3.10.11 + braket + spinqit，环境就在 `.venv/`。
下面"已实测"指用真实 SDK 跑过，"已验证"指纯离线自测。

| 模块 | 状态 |
|---|---|
| `loomq/ir.py` `qasm2_parser.py` `emitters.py` | 已验证，`selftest_transpile.py` 37 项 |
| `loomq/refsim.py` | 已验证，理想分布对上官方公开值 |
| `loomq/decompose.py` | 已验证，`selftest_decompose.py` 22 项（含 crz 陷阱的数值反例） |
| `loomq/visualize.py` `loomq/cli.py` | 已验证，`--demo` 端到端跑通，字符集自动降级；`selftest_explain.py` 35 项 |
| `loomq/agent.py` | **已接真实模型实测**，`selftest_agent.py` 42 项 + `selftest_l2_live.py` 对标组 24/24 |
| `loomq/counts.py` | **已实测标定**：spinq/braket/真机原生位序都与约定相反，均需反转 |
| `loomq/backends.py` braket / spinq | **已实测**，三处 `[待实测]` 全部关闭 |
| `loomq/backends.py` spinq_cloud（真机） | **已实测**，两个平台各跑通，CLI `--backend spinq_cloud` 端到端验证 |
| `loomq/backends.py` originq | 已写，pyqpanda 未装，仍未验证，优先级低 |
| `regression/` 回归集 | **已实测**，6 电路 × 2 后端保真度 ≥0.989 全 PASS |
| `adapter.py` transpile / run | 已接通，L1 公开自测四个 case 全 PASS |
| `adapter.py` agent_chat（L2） | 已接通，委托 `loomq.agent` |
| `adapter.py` compile_hybrid（L3） | 放弃 |
| `starter_kit/requirements.txt` | **已锁定**，65 包全量 freeze，干净环境 dry-run 验证可解 |

**L1 入门档（12 分资格线）已经拿到**：`evaluator.py --level l1 --target spinq,braket`
四个 case 全 PASS，退出码 0。

**L2 客观分已实测达标**：对标组 24/24，折算 ≈ 20/20，远超"交互体验分计入"所需的 12 分线。

**真机 10 分已拿满**：`gemini_vp` 与 `triangulum_vp` 两个平台，
`check_hardware_evidence.py` 按官方三条标准核验全达标。

剩下的：控制台任务页截图（只能人工，护住已得的 10 分）、找真人试 CLI、
录演示视频、pyqpanda（L1 进阶，12 → 35 的唯一途径，也是剩余最大的分数杠杆）。

## 附二：命令速查

本机的 3.10 环境在 `.venv/`。Windows 上先激活，否则 `python` 会指到 3.12：

```powershell
.venv\Scripts\Activate.ps1
$env:PYTHONIOENCODING = "utf-8"   # 否则中文输出在重定向时会乱码
```

不需要任何第三方依赖，用 3.12 也能跑：

```bash
python tools/selftest_transpile.py                  # 转译层 37 项
python tools/selftest_decompose.py                  # 门分解 22 项
python tools/selftest_agent.py                      # L2 Agent 42 项
python tools/selftest_explain.py                    # 结果解释文案 35 项
python tools/gen_circuits.py                        # 重新生成回归集 + 灵敏度检验
python tools/run_regression.py                      # 回归集（参考模拟器）
python tools/check_hardware_evidence.py             # 真机证据按官方三条标准核验
cd starter_kit && python -m loomq.cli --demo         # 零基础入口演示
```

必须在 `.venv`（3.10 + SDK）里跑：

```bash
python tools/probe_bitorder.py                             # 位序标定
python tools/run_regression.py --target spinq,braket       # 回归集（真实后端）
cd starter_kit && python evaluator.py --level l1 --target spinq,braket
cd starter_kit && python -m loomq.cli --backend braket --demo
```

还需要 `LOOMQ_SPINQ_USERNAME` / `LOOMQ_SPINQ_KEYFILE`（真机，每次约两分钟）：

```bash
python tools/make_spinq_key.py                             # 生成密钥并打印公钥
python tools/run_spinq_cloud.py platforms                  # 查在线真机，不提交任务
python tools/run_spinq_cloud.py calibrate --qubits 2       # 位序标定，必做
python tools/run_spinq_cloud.py run --qasm starter_kit/circuits/bell.qasm --platform gemini_vp
cd starter_kit && python -m loomq.cli --backend spinq_cloud
```

还需要 `LOOMQ_LLM_*` 环境变量：

```bash
cd starter_kit && python evaluator.py --level l2
cd starter_kit && python -m loomq.cli --ask "让三个比特纠缠起来"
```

### PowerShell 的两个坑

`>` 重定向写出的是 **UTF-16**，直接看会以为程序输出乱码。要留存输出就用 Python 写文件，
或者 `| Out-File -Encoding utf8`。另外 PowerShell 5.1 **不支持 `&&`**，串联命令用 `;`。
