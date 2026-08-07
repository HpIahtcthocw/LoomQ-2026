# LoomQ 参赛作战计划（单人）

截止：**2026-08-25 12:00 UTC+8**（以 GitHub Issue 的 `created_at` 为准）
本机只做方案与代码，**所有安装与运行在另一台机器上进行**，见 [RUNBOOK.md](RUNBOOK.md)。

## 一、得奖策略：不冲总分，主攻专项奖

总分第一需要 L1 满分（45）+ L3（15）+ Bonus RISC-V（+8），那是多人硬核队的活，单人两周不现实。

真正的机会在 **「LoomQ 最佳包容性设计与优秀体验奖」**。它的评选标准是：

> 让一位没有任何量子背景的跨界创作者，在 5 分钟内依靠智能体引导，成功在真实量子机上完成人生第一个实验，并理解其科学原理。

这个奖比的是产品、引导和叙事，正是硬核队伍最不上心的地方，而且**不要求总分排名靠前**。评选资格是"L2 智能体交互、新手引导体验、无门槛可视化方向得分突出"。

由此确定投入分配：

| 模块 | 目标 | 分 | 理由 |
|---|---|---:|---|
| L1 通用中间层 | 入门档 → 尽量摸进阶 | 12 → 25+ | 12 分是**评奖资格线**，必须拿到 |
| L2 智能体 | 打满 | 30 | 纯 LLM 工程，无量子门槛，专项奖的主战场 |
| 工程与产品化 | 打满 | 10 | 一键跑通 + 答好"为谁而做"必答题 |
| Bonus 新手引导与视觉叙事 | 打满 | +4 | 专项奖的直接加分项 |
| L1 真机（量旋） | 打通 1 台 | +5 | **专项奖标准里写了"真实量子机"，这项不是可选** |
| L3 混合编译 | 放弃 | 0 | 单人写编译器投产比最差 |

保守 **61 分**，L1 进阶打开可到 **70+**。

## 二、里程碑

| 阶段 | 产出 | 判定标准 | 状态 |
|---|---|---|---|
| M0 环境 | Python 3.10 venv + braket/spinqit 装好 | 官方示例能跑出 counts | **已完成** |
| M1 位序标定 | 确认每个后端的原生位序 | `tools/probe_bitorder.py` 报告与大赛约定一致 | **已完成**（两后端均需反转） |
| M2 L1 入门（资格线） | spinq + braket 两个模拟器跑通公开电路 | `evaluator.py --level l1 --target spinq,braket` 全 PASS | **已完成**（4/4 PASS） |
| M3 L1 进阶 | 加 originq；跨 12 门覆盖 | `tools/run_regression.py` 六个电路保真度 ≥0.97 | 两后端已全过；originq 未装 |
| M4 L2 客观 | `agent_chat` 三类任务 | 三类任务离线自测通过 | 代码完成，**待接真实模型** |
| M5 L2 交互 | 零基础用户入口 | 找一个不懂量子的人，5 分钟内跑出第一个电路 | CLI 完成，待找真人试 |
| M6 真机 | 量旋云跑一次，存 job_id + result.json | job_id 可在控制台溯源 | **未开始**（需账号） |
| M7 提交 | 证据包 + Issue | 拿到 `submission:accepted` 标签和回执 | 证据包骨架已写 |

**M2 是生死线，已经过了。** 12 分资格线到手，意味着后面所有专项奖的评选资格都保住了。

剩下的优先级：M6（真机，专项奖标准明写"真实量子机"）> M4 接真模型 > M5 找真人试 > M7 提交 > M3 补 originq。

## 三、架构：一个关键的解耦

题面要求"真正抽象的中间层，三套硬编码分支不算通用，评委会审查架构"。同时注意 **`transpile()` 和 `run()` 的消费者不是同一个**：

- `transpile()` 的返回值由**组委会的解析器**打分（`target_ir_contract.md` 规定了规范子集）
- `run()` 的内部实现只需要让**真实 SDK** 跑出正确 counts

这两者的门名可以不同。例如 Braket 的 OpenQASM 方言用 `si`/`ti`/`ccnot`，而 stdgates.inc 用 `sdg`/`tdg`/`ccx`。所以：

```
QASM2 文本
   ↓ qasm2_parser
统一 IR（Circuit: n_qubits / n_clbits / ops[Gate|Measure]）
   ├─→ emit_qasm2   → target "spinq"    （SpinQit 原生吃 QASM 2.0，转译与执行同一份）
   ├─→ emit_qasm3   → target "braket"   （交给评测器的规范 OpenQASM 3）
   └─→ emit_originir→ target "originq"  （OriginIR 文本）
                              ↓
                       backends.run_*  （真正执行，可用各自方言或 SDK 原生 API）
                              ↓
                    counts 归一化（位序 + Schema）
```

门名映射全部集中在 `emitters.py` 的字典里，加后端只加一张表，这就是"抽象中间层"的可审查证据。

完整模块清单（都在 `starter_kit/loomq/`）：

| 模块 | 职责 | 依赖 |
|---|---|---|
| `ir.py` | `Circuit`/`Gate`/`Measure`，唯一的内部数据结构；输入白名单 12 门与 IR 门集（含 `u1`）严格分开 | 无 |
| `qasm2_parser.py` | OpenQASM 2.0 → 统一 IR；多寄存器展平、整寄存器广播、白名单外的门直接拒绝 | 无 |
| `emitters.py` | 统一 IR → 各后端原生表示，门名映射集中于此 | 无 |
| `decompose.py` | `gate_identities.md` 的门分解，落点为 `h`/`x`/`cx`/`rz`/`u1` | 无 |
| `refsim.py` | 纯标准库参考态矢模拟器，供离线验证与 Agent 自验 | 无 |
| `counts.py` | 位序与结果 Schema 归一化；`coerce_to_counts` 处理 SDK 返概率而非计数的情况 | 无 |
| `backends.py` | 真实 SDK 执行层，**惰性导入**——缺哪个 SDK 只影响那一个 target | 各 SDK |
| `agent.py` | L2 智能体：意图生成 / 代码纠错 / 选后端，含自验重试闭环 | LLM 服务 |
| `visualize.py` | 电路图 / 直方图 / 人话解释，字符集自动降级 | 无 |
| `cli.py` | 零基础用户入口 | 无 |

只有 `backends.py` 与 `agent.py` 需要外部依赖，其余全部纯标准库。这是"永远能跑起来"的结构性保证：本机无 SDK 无 key，仍能完整验证除真机执行外的一切。

## 四、三个必须重视的技术陷阱

以下结论**全部经 `tools/selftest_decompose.py` 用参考模拟器数值验证**，不是照抄文档。

### 1. 受控相位门认错语义会让 QFT-4 静默失败

`gate_identities.md` 说"cu1 分解必须用 u1，作为受控门分解的组成部分时 rz 不可互换"。实测结论比这更精确，而且要点不在 rz 上：

**把 cu1 那个 5 门分解里的三个 `u1` 全部换成 `rz`，结果只差一个全局相位，完全等价。** 因为单比特 `rz(φ) = e^{-iφ/2}·u1(φ)`，这个因子是纯标量；标量与一切算子交换，三个标量相乘仍是标量，任何测量都观测不到。

真正会错的是把 `cu1` 实现成**受控 rz**（crz）：

```
crz(θ) = diag(1, 1, e^{-iθ/2}, e^{iθ/2})
cu1(θ) = diag(1, 1, 1,        e^{iθ})
```

此时相位挂在受控分支上，成了**相对**相位，可观测。实测在 QFT 风格的两比特电路上保真度只有 **0.8154**，在完整 QFT-4 上只有 **0.7241**，都远低于 0.97 阈值。而 Bell 和 GHZ 不含 `cu1`，完全看不出问题。

落到实操：各平台的受控相位门要认准语义是 `diag(1,1,1,e^{iθ})`。Braket 用 `cphaseshift`（正确），OpenQASM 3 stdgates 用 `cp`（正确），OriginIR 契约写的是 `CU1/CR`——**`CR` 是否为 cu1 语义需要在接本源时实测确认**。永远不要用"受控 rz"去顶替 `cu1`。

### 2. 位序错误对大多数"标志性"电路完全隐形

`bell.qasm` 的理想分布是 `{"00":0.5,"11":0.5}`，`ghz3` 是 `{"000":0.5,"111":0.5}` —— **两者都是回文对称的**，把整个位串反转过来分布一模一样。位序写错时公开自测依然全 PASS。

更值得警惕的是：**GHZ-5 和 Grover-3 也测不出位序错误**（`gen_circuits.py` 的灵敏度检验实测保真度均为 **1.0000**）。GHZ-5 的原因同 bell；Grover-3 的原因是目标态 `111` 本身回文，而其余七个态概率相等，反转只是把它们互相置换。所以"Grover 命中率 94.5%"看着很有辨识度，其实对位序毫无约束力。

真正能抓住位序错误的是 **QFT-4（反转后保真度 0.1340）和三个随机电路（0.38 / 0.57 / 0.60）**——它们的分布不对称。

不过回归集只是兜底。位序应当**先标定、再回归**：`tools/probe_bitorder.py` 用非对称探针电路（只翻转 `q[0]`）直接读出后端的原生约定。大赛约定 key 的最右字符是 `c[0]`，所以 2 比特下正确结果必须是 `"01"`，若得到 `"10"` 说明该后端原生位序相反，需在 `counts.py` 里反转。

### 3. 对基态做 QFT 是无效的回归测试

QFT 作用在单个基态 `|x⟩` 上时，输出的 16 个振幅**模长全部相同**，测量分布是均匀的 6.25%，与 x 取值无关，也与相位是否算错无关。用这种电路做回归，任何相位错误都测不出来（实测确认）。

`tools/gen_circuits.py` 因此用 `ry(pi/2)` 制备输入态——不用 `h`，因为 QFT 自身也对每个比特施加 `h`，用 `h` 制备会与之相互抵消，实测会把分布搅成 11 个杂峰。制备在 `q[0]`、`q[1]` 上，输出恰好是 4 个 25% 的峰，与题面对 QFT-4 的描述一致。

同样的道理也否掉了随手取的随机电路种子：原先 `random_b`/`random_c` 的种子生成的分布接近均匀（`random_c` 是 32 个态各 3.125%），**对相位错误和位序错误的保真度都是 1.0000**，等于白测。现在的三个种子是筛出来的，要求两类错误都能检出。`gen_circuits.py` 末尾的灵敏度检验会把每个电路对两类错误的保真度列成表，任何一类没有探测器就报警。

## 五、风险与对策

| 风险 | 对策 |
|---|---|
| spinqit 与 Braket 有三层版本冲突（Python 版本、antlr 硬钉、声明下界不等于真实兼容） | 已解决并锁进 `requirements.txt`：3.10.11 + `braket-sdk==1.97.0` + `default-simulator==1.27.0` + `antlr==4.9.2`。详见 RUNBOOK 步骤 2 |
| Windows 上 `import igraph` 时好时坏 | 缺 `MSVCP140.dll`。装 VC++ 运行库；先 import torch 会"碰巧"成功，别依赖那个 |
| Windows 上 pyqpanda 安装困难 | originq 是进阶项，装不上就先跳过，不影响资格线 |
| 评测在 Linux 容器，本地是 Windows | 提交前用仓库自带 Dockerfile 验一遍 |
| 依赖版本 | `requirements.txt` 必须写 `==`，写 `>=` 会被拒 |
| L2 无 key 可调试 | 自备 DeepSeek key，组委会赛前不提供任何额度 |
| 提交无效 | 建 Issue 不等于提交成功，必须拿到 `submission:accepted` 标签 + 归档回执 |
| Braket 门名方言 | `transpile` 输出与 `run` 内部解耦（见第三节），先用探针脚本确认 |

## 六、当前进度

本机（无量子 SDK、无 API key）能验证的部分全部完成并通过：

- [x] 赛题与 Starter Kit 通读
- [x] 架构设计
- [x] QASM2 解析器 + 统一 IR + 三个发射器 —— `selftest_transpile.py` 37 项断言通过
- [x] 参考态矢模拟器（纯标准库）—— 对上官方公开分布
- [x] 门分解 + 数值验证 —— `selftest_decompose.py` 22 项断言通过
- [x] 隐藏电路回归集 GHZ-5 / QFT-4 / Grover-3 / Random×3 + 理想分布 + 灵敏度检验
- [x] 位序探针脚本
- [x] L2 Agent（三类任务 + 自验重试闭环）—— `selftest_agent.py` 36 项断言通过
- [x] 电路图 / 直方图 / 人话解释
- [x] 零基础用户 CLI 入口 —— `python -m loomq.cli --demo` 端到端跑通
- [x] 证据包与 `submission.yaml`

真实 SDK 环境（Python 3.10.11 + braket 1.97.0 + spinqit 0.2.4）也已装好并跑通：

- [x] 后端执行层实测 —— `backends.py` 三处 `[待实测]` 全部关闭
- [x] 位序标定并写回 `counts.NATIVE_MATCHES_CONTEST` —— 两后端均需反转
- [x] `evaluator.py --level l1 --target spinq,braket` 全 PASS（**评奖资格线已到手**）
- [x] `requirements.txt` 精确锁版本 —— 干净环境 dry-run 验证可解
- [x] 隐藏电路回归跑真实后端 —— 6 电路 × 2 后端保真度 ≥0.989

还差的（都需要外部账号或人，不是代码问题）：

- [ ] L2 在真实 DeepSeek 下的通过率调优（需自备 API key）
- [ ] 量旋云真机 + job_id 证据（需注册账号，**专项奖标准要求**）
- [ ] 找一个不懂量子的人实测 5 分钟引导
- [ ] 录制 CLI 演示视频
- [ ] originq / pyqpanda（L1 进阶，可选）

### 各模块自测命令一览

```bash
python tools/selftest_transpile.py     # 转译层 37 项，纯标准库
python tools/selftest_decompose.py     # 门分解数值验证 22 项，纯标准库
python tools/selftest_agent.py         # L2 Agent 36 项，本地假端点，无需 API key
python tools/gen_circuits.py           # 重新生成回归集并做灵敏度检验
python tools/probe_bitorder.py         # 位序标定（需装 SDK）
cd starter_kit && python -m loomq.cli --demo
```

四条纯标准库的命令加起来约 10 秒，任何一条报 FAIL 都不要往下走。
