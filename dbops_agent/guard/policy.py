"""Static capability and parameter policy; approval authority lives in execution.py.

The Agent can request approval, but cannot approve its own destructive operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tier(StrEnum):
    L0_AUTO = "l0_auto"
    L1_CONFIRM = "l1_confirm"
    L2_REFUSE = "l2_refuse"


class Verdict(StrEnum):
    ALLOWED = "allowed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    REFUSED = "refused"
    BAD_TOKEN = "bad_token"


# --- 动作登记表 ---------------------------------------------------------------------
#
# 集中声明，这样整个策略一屏就能审完，而不是散落在各个工具实现里。
# `forbidden_reason` 表达的是关于**调用方式**而非「是哪个动作」的规则：
# 调高连接池上限是常规操作，但关掉一个安全限流不是。

ACTION_TIERS: dict[str, Tier] = {
    "rebuild_search_index": Tier.L0_AUTO,
    "raise_pool_ceiling": Tier.L0_AUTO,
    "acknowledge_alert": Tier.L0_AUTO,
    "write_incident_report": Tier.L0_AUTO,
    "deduplicate_payments": Tier.L1_CONFIRM,
    "terminate_session": Tier.L1_CONFIRM,
    "set_config": Tier.L1_CONFIRM,
}


@dataclass
class Policy:
    """决定动作级别；不签发或验证可由 Agent 自授权的令牌。"""

    def tier_of(self, action: str) -> Tier:
        return ACTION_TIERS.get(action, Tier.L2_REFUSE)

    # --- 禁止的参数组合 ---------------------------------------------------------------

    @staticmethod
    def forbidden_reason(action: str, args: dict) -> str | None:
        """关于参数的规则，也就是「低危动作被用成高危」的那些情形。"""
        if action == "set_config":
            key = str(args.get("key", ""))
            value = str(args.get("value", ""))
            limits = {
                "db.statement_timeout_ms": 30_000,
                "api.rate_limit_per_min": 600,
                "db.pool.max_size": 200,
            }
            if key not in limits:
                return f"配置项 {key!r} 不在可变更白名单中"
            try:
                number = int(value)
                if not 1 <= number <= limits[key]:
                    return f"{key} 必须在 1 至 {limits[key]} 之间"
            except (TypeError, ValueError):
                return f"配置值必须是整数: {value!r}"
        if action == "raise_pool_ceiling":
            try:
                if not 1 <= int(args.get("max_size", 0)) <= 200:
                    return "连接池上限必须在 1 至 200 之间"
            except (TypeError, ValueError):
                return "连接池上限不是数字"
        if action == "terminate_session":
            try:
                if int(args.get("session_id", 0)) <= 0:
                    return "拒绝终止一个未指明的会话"
            except (TypeError, ValueError):
                return "会话 id 不是数字"
        return None

    # --- 决策 -------------------------------------------------------------------------

    def evaluate(
        self, action: str, args: dict, *, confirm_token: str | None = None
    ) -> tuple[Verdict, str]:
        """返回 (裁决, 人类可读的原因)。"""
        reason = self.forbidden_reason(action, args)
        if reason is not None:
            return Verdict.REFUSED, reason

        tier = self.tier_of(action)
        if tier is Tier.L2_REFUSE:
            return Verdict.REFUSED, f"{action} 超出 Agent 的权限范围"

        if tier is Tier.L0_AUTO:
            return Verdict.ALLOWED, "低风险且可逆"

        if confirm_token or args.get("confirm_token"):
            return Verdict.BAD_TOKEN, "旧版 confirm_token 不能授权执行；需要独立审批"
        return Verdict.NEEDS_CONFIRMATION, "需要独立操作者批准"
