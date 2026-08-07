# LoomQ 人工评分证据

这份文件是人工评分材料的统一入口。请直接编辑它，只填写要申报的项目。截图、原始结果或图表统一放在 `starter_kit/evidence/files/`，也可以引用 `starter_kit/` 中已有的代码和文档。

证据包是可选的。没有申报某项人工分时，留空即可，不影响自动评分。

## 提交前填写

把要申报项目的方框改成 `[x]`，并填写对应内容：

- [ ] L1 真机 —— **待完成**：本地模拟器已全部打通，还需注册量旋云跑一次真机
- [x] L2 交互体验
- [x] 工程与产品化
- [ ] 自定义量子 RISC-V Bonus —— 本次不参加 L3，不申报
- [x] 新手引导与视觉叙事 Bonus

## L1 真机

每个有效真机平台计 5 分，最多两个平台。模拟器不计真机分。每个平台复制并填写一次下面的信息：

```text
平台名称：量旋云 SpinQ Cloud（待打通）
平台 job ID：[待填写]
运行时间：[待填写，带时区]
shots：[待填写]
实际执行的 QASM：[待填写仓库内路径]
平台返回的原始结果：[待填写仓库内路径]
任务页截图：[待填写仓库内路径]
```

> 打通真机后，把 QASM 与原始结果保存为
> `evidence/files/spinq-circuit.qasm` 与 `evidence/files/spinq-result.json`，
> 并把上面的方框改成 `[x]`。真机是加分项而非资格线，但**专项奖标准里写了"在真实量子机上"**，
> 所以这一项对我们不是可选的。

## L2 交互体验

```text
启动界面或 CLI 的命令：
  cd starter_kit && python -m loomq.cli
  （零依赖，不装任何量子 SDK 也能完整跑通；加 --demo 可自动依次跑完下面三个任务）

测试入口或页面地址：无（命令行入口，题面允许 CLI）

适合现场体验的 3 个用户任务：
1. 启动后直接输入 1 —— 做出两比特贝尔态。观察结果只有 00 和 11 各一半，
   中间的 01、10 一次都不出现；界面会解释这就是「纠缠」。
2. 输入 3 —— 用 Grover 搜索在八个抽屉里找出 111。观察正确答案的概率
   从 12.5% 被放大到约 78%，界面会说明这是干涉在起作用。
3. 用自然语言输入「让四个比特全都纠缠起来」—— 智能体生成电路、
   自我验证、给出电路图与结果解释。若模型服务未配置，界面会明确
   提示缺少哪个环境变量并引导改用内置示例，不会报错退出。

截图或演示视频：[待补充录屏，路径 evidence/files/cli-demo.mp4]
```

交互设计上有三点是刻意为之，便于现场核验：

1. **永远能跑起来。** 未安装量子 SDK 时自动回落到内置参考模拟器（纯标准库精确模拟），
   未配置模型服务时回落到内置示例库，流程完整不中断。
2. **先看懂再动手。** 首屏用三句话讲清量子比特、测量与分布，不堆术语。
3. **错误可恢复。** 所有失败路径都给出下一步动作，不打印堆栈；
   `--backend refsim` 是任何环境下都成立的兜底方案。

## 工程与产品化

```text
干净环境中的构建和启动命令：
  Python 3.10 环境下：
    pip install -r starter_kit/requirements.txt
    cd starter_kit && python evaluator.py --level l1 --target spinq,braket
  已实测通过：4/4 case PASS。requirements.txt 为全量精确锁定（66 行 ==），
  并在干净的 3.10 venv 里 dry-run 验证过依赖可解。
  完整步骤见仓库根目录 RUNBOOK.md（九个步骤，每步都有明确通过判据）
  最小验证（无需任何第三方依赖，也不需要 API key，全部约 10 秒）：
    python tools/selftest_transpile.py     # 转译层，37 项断言
    python tools/selftest_decompose.py     # 门分解数值验证，22 项断言
    python tools/selftest_agent.py         # L2 Agent，36 项断言，用本地假端点
    python tools/gen_circuits.py           # 生成隐藏电路回归集 + 灵敏度检验
    python tools/run_regression.py         # 回归集比对理想分布
    cd starter_kit && python -m loomq.cli --demo

架构说明：见 PLAN.md 第三节。分层为
  qasm2_parser  OpenQASM 2.0 → 统一 IR
  ir            Circuit / Gate / Measure，唯一的内部数据结构
  emitters      统一 IR → 各后端原生表示（门名映射集中在此，加后端只加一张表）
  decompose     gate_identities.md 的门分解
  backends      真实 SDK 执行层，惰性导入
  counts        位序与统一结果 Schema 归一化
  refsim        纯标准库参考态矢模拟器，供离线验证与 Agent 自验
  agent         L2 智能体
  visualize     电路图 / 直方图 / 人话解释
  cli           零基础用户入口

  十个模块里只有 backends 和 agent 需要外部依赖，其余全部纯标准库。
  这是"永远能跑起来"的结构性保证，也让上面那六条验证命令在任何机器上都成立。

跨平台一致性的实测证据：
  自造的 6 个隐藏电路（GHZ-5 / QFT-4 / Grover-3 / Random×3，覆盖全部 12 门）
  在 spinq 与 braket 两个后端上，与参考模拟器精确算出的理想分布相比，
  保真度全部 >= 0.989（官方阈值 0.97），命令：
    python tools/run_regression.py --target spinq,braket
  这比只跑公开的 bell/ghz3 强得多——后两者分布回文对称，位序写反了也照样 PASS。

目标用户和使用场景：
  有明确问题意识、具备基本计算机使用能力，但没有量子物理背景的人——
  尤其是跨界创作者、教育工作者、以及被"黑话高墙"挡在门外的女性开发者。
  场景是：想验证一个想法值不值得深入，但不愿先花三个月学量子力学。

完整使用流程：RUNBOOK.md + `python -m loomq.cli --demo` 的输出即是完整演示
```

**必答题：你的工具让哪一类原本进不来的人，第一次能用上量子计算？**

答：**能把想法说清楚、但不掌握任何量子平台方言的人。**

今天的门槛不在于智力，而在于"入场费"：想跑一个量子电路，你得先选平台、注册账号、
学它专属的 SDK 与指令集，而三家平台的方言互不相通。这笔入场费筛掉的不是不聪明的人，
是没有科班背景、没有实验室资源、没有师兄带路的人。

LoomQ 把这笔入场费拆成两半各自消灭：中间层消灭"必须学会某家方言"，
智能体消灭"必须先会写 QASM"。剩下的只需要用中文说出你想做什么。

## 自定义量子 RISC-V Bonus

本次不参加 L3，不申报此项。

## 新手引导与视觉叙事 Bonus

```text
零基础首次运行指南：
  starter_kit/loomq/cli.py 的首屏引导（三句话讲清量子比特、测量与分布，
  并给出三个可直接输入 1/2/3 运行的例子）
  以及仓库根目录 RUNBOOK.md

量子概念解释：
  starter_kit/loomq/visualize.py 的 explain_distribution()
  ——完全由数据驱动，按结果的集中度与结构自动生成人话解释，
  例如两比特只出现 00/11 时会指出「中间那些结果一次都没出现，这就是纠缠」。
  弧度也会被翻译成 pi/2 这种可读写法（_pretty_angle）。

结果可视化：
  starter_kit/loomq/visualize.py
  - circuit_diagram()：文本线路图，按逐比特前沿列排布以保证时序正确，
    多比特门用竖线连接
  - histogram()：结果分布条形图
  - bitstring_legend()：位串读法说明（最右一位是 c[0]，新手最容易搞错的地方）
  字符集按终端编码自动降级为纯 ASCII，任何环境下都不会出现乱码。

错误恢复或无障碍引导：
  - 后端不可用时自动回落到内置参考模拟器并说明原因，流程不中断
  - 模型服务未配置时明确指出缺少哪个环境变量，并引导改用内置示例
  - 电路解析失败时给出具体原因和下一步动作，不打印堆栈
  - Agent 自验未通过时如实告知，但仍交付电路，不让用户空手而归
  - 长句按显示宽度折行（中文按 2 计），中英混排表格按显示宽度对齐
```

## 提交规则

- 所有材料都要在截止前进入最终提交的 commit，工作人员不接受截止后补交。
- 外部视频可以用稳定只读链接，源码、原始结果和复现命令应保存在仓库中。
- 整个 fork commit 的归档包不得超过 100 MiB。
- 不要提交 API Key、Token、Cookie、个人身份信息或平台账户隐私。
- 如申报 L1 真机分，在最终提交 Issue 的 `Hardware evidence` 中填写 `starter_kit/evidence/README.md`。
