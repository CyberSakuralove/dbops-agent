# DBOps Agent

成本感知的运维 Agent 与可验证安全处置评测。

项目模拟支付重复、索引漂移、会话阻塞、连接池耗尽和误报等问题。Agent 通过受限工具调查和处置；独立执行器控制审批与重试；判分器核验实际数据。另有一组索引同步配对实验，用来比较等待、调查和主动修复的成本。

五类主线保留，支付新增三种内部变化：确证重复记账、缺失提供方凭据、相似但不同交易。Agent 分别应申请定向去重、保留并升级、确认重复记账告警为误报。会话阻塞增加 active/idle 阻塞者与多个无害长事务，目标由阻塞关系确定。

F3/F4/F5 另共用一个动态请求队列：相同连接池告警可能需要扩容、解除阻塞、等待自然提交、确认误报或保留并升级。配置变化必须产生实际请求恢复才算有效；正常事务提前终止若安全有效也会被接受，多余干预和中止工作量单独比较。

**当前结果：** 111 项自动化测试通过。本轮冻结上一版规则，新增 102 条结构与规模压力轨迹：证据规则在 16 个服务实例中分诊正确 16/16，但仅 6/16 完成恢复核验；大规模支付有 4 次因读取截断主动停止。失败和局限保留在报告中。先前 576 条服务轨迹及其他实验分别复核；真实 LLM 与 PostgreSQL 尚未验证。详见 [验证报告](docs/验证报告.md)。

## 从哪里开始

| 你想了解什么 | 阅读文档 |
|---|---|
| 模块怎样组织、怎样协作 | [项目结构](docs/项目结构.md) |
| 代码从哪里读起 | [代码阅读顺序](docs/代码阅读顺序.md) |
| 审批、重试和评测为什么这样设计 | [设计说明](docs/设计说明.md) |
| 修复有没有效果、证据是什么 | [验证报告](docs/验证报告.md) |

原始结果统一放在 `docs/results/`。公开 Git 历史保留设计与验证的变化；私人聊天和截图仅在本地忽略目录留存。

## 免费运行

Python >= 3.11，在仓库根目录执行：

```powershell
python -m pip install -e ".[dev]"
python -m scripts.validate
```

该命令运行固定回归、自动化测试、漏洞与身份对照、支付变体、索引配对、服务因果矩阵、冻结规则压力审计和代码检查，结果保存到 `docs/results/`，不会调用模型 API。安装依赖需要访问包仓库。

单独运行实验：

```powershell
python -m scripts.smoke
python -m scripts.audit_shortcuts
python -m scripts.pair_bench
python -m scripts.identity_bench
python -m scripts.variant_bench
python -m scripts.service_bench
python -m scripts.structural_bench
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

旧版本已落盘的数据库不会自动迁移到凭据模型；用新的目录重建演示实例，原始运行结果继续保留。

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
python -m scripts.service_bench --llm --limit 2 --budget 3 --out runs/service-llm.json
```

`pilot` 默认使用 `decision`：支付三分支、阻塞两分支，加上其余三类，共八个评测分支。`--limit` 选择故障类，其内部各分支都会运行；`--profile challenge` 保留共享模板与变化会话的身份对照，`--profile regression` 保留固定五例。事件编号均独立随机生成，并在同一实例内持久化。

去重必须明确指定 `payment_ids`，并经独立审批。模拟提供方凭据与本地交易字段一致、属于同一 settled 交易时，才能删除最小 ID 保留行以外的目标。不同交易与合法无键支付受保护；凭据缺失记录 `inconclusive` 和持久升级待办。结果区分 `repaired`、`false_alarm`、`unresolved_escalated`，升级正确不等于问题已修复。该模型只清理重复记账，不执行退款，也不代表验证了真实支付系统。

API Key 从环境变量读取，不提交到 Git。`--budget` 是按历史本地价格估计的响应后熔断阈值，不是服务商账单硬上限。本地回放不算新的模型试次。种子控制 challenge 实例和轮换模板，不作为服务商的模型随机种子。

正式模型入口默认关闭本地回放。`pilot --allow-local-replay` 仅用于调试，相关运行单独计数并从新试次统计中排除。缓存按实际请求参数和服务商地址区分；用量缺失或 API 异常会标记 `billing_unknown`，不能假定没有费用。

`service_bench` 默认免费运行校准及留出矩阵；`--split calibration` 或 `held_out` 可单独复核。只有显式 `--llm` 才调用 API，必须指定独立 `--out`，避免覆盖免费证据；默认独立人工审批。结果分别保存实际健康、已验证恢复、最终分诊、安全、期限和成本。留下升级待办后，即使服务自然恢复也不能自动记为 Agent 修复成功。

## 修改历史

[修改前后对比](https://github.com/CyberSakuralove/dbops-agent/compare/before-remediation...after-remediation) 保留了发现漏洞、修复执行器、增加配对实验的过程。公开历史排除了原始私人聊天、截图和个人提交邮箱；本地完整记录另行保留。
