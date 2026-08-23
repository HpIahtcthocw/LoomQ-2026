# 正式环境复现说明

这份说明把正式评测环境要跑的命令和目前已有的记录放在一起。带“待复现”的项目。

## 归档对象

| 项目 | 值 |
|---|---|
| Fork | `https://github.com/HpIahtcthocw/LoomQ-2026` |
| 提交版本 | 最终提交 Issue 中填写 `prepare_submission.py` 输出的完整 commit SHA |
| 合同版本 | `1.0` |
| Starter Kit | `1.1.0` |
| 目标运行时 | Linux / Python 3.10 |
| 依赖入口 | `starter_kit/requirements.txt` |

## 评测环境命令

在干净的 Python 3.10 环境中执行：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r starter_kit/requirements.txt

python starter_kit/prepare_submission.py --team-id HpIahtcthocw
python starter_kit/evaluator.py --level l1 --target spinq,braket,originq --json-out /tmp/l1.json
python starter_kit/evaluator.py --level l3 --json-out /tmp/l3.json
```

L2 需要由评测环境注入 `LOOMQ_LLM_BASE_URL`、`LOOMQ_LLM_API_KEY`、`LOOMQ_LLM_MODEL`，再执行：

```bash
python starter_kit/evaluator.py --level l2 --json-out /tmp/l2.json
```

## 目前能对上的记录

| 检查项 | 当前状态 | 证据 |
|---|---|---|
| 提交预检 | 已通过 | `prepare_submission.py` 的输出 |
| L1 三平台公开电路 | 已在 Python 3.10.11 环境记录为 6/6 PASS | `evidence/README.md`、`RUNBOOK.md`；公开电路位于 `starter_kit/circuits/` |
| L3 公开分支 | 已通过 | `python starter_kit/evaluator.py --level l3` |
| L3 随机差分 | 已通过仓库自测 | `tools/selftest_hybrid.py`，记录的 800 组随机程序结果 |
| RISC-V Bonus | 已通过仓库自测 | `tools/selftest_quantum_ext.py`、`docs/quantum-riscv-extension.md` |
| L2 正式模型 | 待在正式注入环境复现 | `starter_kit/l2_policy.json`、`tools/selftest_l2_live.py` |
| 真机证据 | 已归档 | `evidence/files/spinq-cloud-raw-payloads.json`、各平台 result/circuit 文件 |

## 需要注意

- 当前 Windows 工作区已用 Python 3.10.11 重建 `.venv` 并装齐依赖；正式评测仍以干净 Linux / Python 3.10 环境为准。
- `evaluator.py` 的公开结果不是正式隐藏分数。隐藏电路、私有 L2 prompt 和随机 L3 用例会由组委会重新生成。
- 不提交任何 API Key、Token、Cookie 或个人身份信息。
- 本文件随证据补充而更新；最终提交前重新运行 `prepare_submission.py`，在 Issue 中填写它输出的最新完整 commit SHA。
