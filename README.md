# DBOps Agent

成本感知的运维 Agent 与可验证安全处置评测。

项目模拟支付重复、索引漂移、会话阻塞、连接池耗尽和误报等问题。Agent 通过受限工具调查和处置；独立执行器控制审批与重试；判分器核验实际数据。另有一组索引同步配对实验，用来比较等待、调查和主动修复的成本。

**当前结果：** 32 项自动化测试通过；审批、幂等和判分漏洞已有修复证据。真实 LLM 对照与 PostgreSQL 验证尚未完成。完整结果见 [验证报告](docs/验证报告.md)。

## 从哪里开始

| 你想了解什么 | 阅读文档 |
|---|---|
| 模块怎样组织、怎样协作 | [项目结构](docs/项目结构.md) |
| 代码从哪里读起 | [代码阅读顺序](docs/代码阅读顺序.md) |
| 审批、重试和评测为什么这样设计 | [设计说明](docs/设计说明.md) |
| 修复有没有效果、证据是什么 | [验证报告](docs/验证报告.md) |

原始结果统一放在 `docs/results/`。历史讨论保留在 Git 历史，当前文档只描述最终实现与已验证结果。

## 免费运行

Python >= 3.11，在仓库根目录执行：

```powershell
python -m pip install -e ".[dev]"
python -m scripts.validate
```

该命令运行五场景回归、自动化测试、漏洞对照、配对规则实验和代码检查，结果保存到 `docs/results/`，不会调用模型 API。安装依赖需要访问包仓库。

单独运行实验：

```powershell
python -m scripts.smoke
python -m scripts.audit_shortcuts
python -m scripts.pair_bench
```

## 独立审批演示

先创建一个支付重复场景并申请去重：

```powershell
python -m scripts.demo init
python -m scripts.demo request
python -m scripts.approve --db runs/demo/business.db
```

申请只返回 `request_id`，不会删除支付。操作者核对后批准，再执行和重试：

```powershell
python -m scripts.approve --db runs/demo/business.db --request <request_id> --decision approve --actor engineer --reason "已核对重复支付行"
python -m scripts.demo execute --request <request_id>
python -m scripts.demo execute --request <request_id>
python -m scripts.demo judge
```

两次执行返回同一结果，只施加一次变更。将 `approve` 换成 `deny` 可以演示拒绝；审批有效期为 300 秒。目录已存在时选择新的 `--dir`，每条演示命令使用同一个目录，避免覆盖旧证据。

## 项目结构

```text
dbops_agent/    场景、工具、执行器、判分和模型循环
scripts/        演示与实验入口
tests/          安全、运行时与配对测试
docs/           四份说明与 results 原始证据
```

模型只能调用注册的工具，不能批准自己的变更。数据库效果、审批消费和操作结果在同一个 SQLite 事务中提交；报告文件和外部 API 不在这项保证内。

## 真实模型实验

阅读 [设计说明](docs/设计说明.md) 中的实验边界，并核对当前 API、价格和服务商额度后，可运行：

```powershell
python -m scripts.pilot --limit 2 --seeds 1 --budget 3 --approval-mode manual
python -m scripts.pair_bench --llm --budget 3 --out runs/pair-llm.json
```

API Key 从环境变量读取，不提交到 Git。`--budget` 是按历史本地价格估计的响应后熔断阈值，不是服务商账单硬上限。本地回放不算新的模型试次；种子目前只是试次标签。

## 修改历史

[修改前后对比](https://github.com/CyberSakuralove/dbops-agent/compare/before-remediation...after-remediation) 保留了发现漏洞、修复执行器、增加配对实验的过程。公开历史排除了原始私人聊天、截图和个人提交邮箱；本地完整记录另行保留。
