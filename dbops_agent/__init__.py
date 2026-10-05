"""dbops-agent：一个数据库/服务故障的自愈 Agent。

各层按依赖顺序排列：

  contract/   机器可验证的状态断言 —— 判分的事实基础
  incident/   故障注入 + 双库 schema + fixture
  tools/      Agent 可调用的确定性工具集
  guard/      分级动作授权（L0 自动 / L1 需确认 / L2 拒绝）
  record/     哈希链 trace、回放缓存、成本账本
  judge/      结果验证、失败归因、报告
  runtimes/   被测的 Agent 循环（先是自写控制组）
  tasks/      场景定义与 fixture

其余一切都遵循的唯一设计规则是：**只判结果，绝不判文字。**
Agent 声称"重复行已清理"是一句话；行数才是事实。
"""

__version__ = "0.1.0"
