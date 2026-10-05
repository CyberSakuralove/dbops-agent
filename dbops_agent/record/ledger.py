"""Token 账本：精确记账 + 硬性预算熔断。

两件普通的 token 计数器不会做的事：

1. **把「缓存命中输入」「缓存未命中输入」「输出」三项分开记。** 在官方价目表上这三者
   单价最多差 200 倍。混在一起记会把整个成本结构藏起来，也让"把提示词压小到底省不省钱"
   这个问题变得无法回答——而本项目必须能回答它，因为诚实的答案是"省不下"。

2. **带熔断。** 一个不停重试撞墙的 Agent 正是本项目要抓的失败，所以账本拒绝继续花钱。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import PRICING, price_multiplier

USD_TO_CNY = 7.1  # 近似值；当真需要精确汇率时再更新


class BudgetExceeded(RuntimeError):
    """超出预算时抛出。大声中止，而不是悄悄超支。"""


@dataclass
class CostEntry:
    label: str
    model: str
    input_cache_hit: int = 0
    input_cache_miss: int = 0
    output: int = 0
    peak: bool = False
    usd: float = 0.0
    cny: float = 0.0


@dataclass
class Ledger:
    budget_cny: float
    model: str = "deepseek-flash"
    entries: list[CostEntry] = field(default_factory=list)
    _spent_cny: float = 0.0

    @property
    def spent_cny(self) -> float:
        return round(self._spent_cny, 6)

    @property
    def remaining_cny(self) -> float:
        return round(self.budget_cny - self._spent_cny, 6)

    def rate(self, key: str) -> float:
        try:
            return PRICING[self.model][key]
        except KeyError as exc:
            known = ", ".join(sorted(PRICING))
            raise KeyError(f"模型 {self.model!r} 没有定价（已知：{known}）") from exc

    def record(
        self,
        label: str,
        *,
        input_cache_hit: int = 0,
        input_cache_miss: int = 0,
        output: int = 0,
        at: datetime | None = None,
    ) -> CostEntry:
        at = at or datetime.now(UTC)
        mult = price_multiplier(at)
        usd = (
            input_cache_hit / 1_000_000 * self.rate("input_cache_hit")
            + input_cache_miss / 1_000_000 * self.rate("input_cache_miss")
            + output / 1_000_000 * self.rate("output")
        ) * mult
        cny = usd * USD_TO_CNY
        entry = CostEntry(
            label=label,
            model=self.model,
            input_cache_hit=input_cache_hit,
            input_cache_miss=input_cache_miss,
            output=output,
            peak=mult == 1.0,
            usd=round(usd, 8),
            cny=round(cny, 8),
        )
        self.entries.append(entry)
        self._spent_cny += cny
        if self._spent_cny > self.budget_cny:
            raise BudgetExceeded(
                f"{label!r} 之后超出预算："
                f"已花 {self.spent_cny:.4f} 元 > 预算 {self.budget_cny:.2f} 元"
            )
        return entry

    def totals(self) -> dict[str, float | int]:
        return {
            "calls": len(self.entries),
            "input_cache_hit": sum(e.input_cache_hit for e in self.entries),
            "input_cache_miss": sum(e.input_cache_miss for e in self.entries),
            "output": sum(e.output for e in self.entries),
            "usd": round(sum(e.usd for e in self.entries), 8),
            "cny": self.spent_cny,
            "budget_cny": self.budget_cny,
        }

    def cost_breakdown(self) -> dict[str, float]:
        """钱到底花在了哪。按次运行报告，绝不合并。"""
        hit = (
            sum(e.input_cache_hit for e in self.entries) / 1_000_000 * self.rate("input_cache_hit")
        )
        miss = (
            sum(e.input_cache_miss for e in self.entries)
            / 1_000_000
            * self.rate("input_cache_miss")
        )
        out = sum(e.output for e in self.entries) / 1_000_000 * self.rate("output")
        total = hit + miss + out or 1e-9
        return {
            "input_cache_hit_usd": round(hit, 8),
            "input_cache_miss_usd": round(miss, 8),
            "output_usd": round(out, 8),
            "output_share": round(out / total, 4),
        }

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "totals": self.totals(),
                    "breakdown": self.cost_breakdown(),
                    "entries": [asdict(e) for e in self.entries],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
