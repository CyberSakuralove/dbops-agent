"""配置。所有开关集中在这里，保证实验可复现。

定价常量已对照官方价目表核实（https://api-docs.deepseek.com/quick_start/pricing/，
2026-10-03 拉取，美元 / 百万 token）。**信任报告里任何成本数字之前请重新核对一遍——
价格会变。**
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- 定价（美元 / 百万 token）-------------------------------------------------------
#
# 这里真正要记住的不对称性：输出 token 的单价是缓存命中输入的 200 倍。任何关于
# "把提示词压小就能省钱"的说法，必须先过这道算术。
PRICING: dict[str, dict[str, float]] = {
    "deepseek-flash": {
        "input_cache_hit": 0.003,
        "input_cache_miss": 0.15,
        "output": 0.6,
    },
    "deepseek-v4-pro": {
        "input_cache_hit": 0.022,
        "input_cache_miss": 0.66,
        "output": 1.98,
    },
}

# 高峰时段（UTC）：01:00-04:00、06:00-10:00，周一至周五。
# 其余时间（含周末与中国法定节假日）为低峰，半价——正好覆盖国内的晚上。
_PEAK_WINDOWS_UTC = ((1, 4), (6, 10))


def is_peak(dt: datetime | None = None) -> bool:
    """判断给定时刻是否落在高峰计费窗口内（按 UTC 计算）。"""
    dt = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if dt.weekday() >= 5:
        return False
    return any(start <= dt.hour < end for start, end in _PEAK_WINDOWS_UTC)


def price_multiplier(dt: datetime | None = None) -> float:
    """低峰时段是半价。"""
    return 1.0 if is_peak(dt) else 0.5


# --- 路径 ---------------------------------------------------------------------------


@dataclass
class Paths:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    runs: Path = field(init=False)
    cache: Path = field(init=False)

    def __post_init__(self) -> None:
        self.runs = self.root / "runs"
        self.cache = self.root / "cache"

    def ensure(self) -> None:
        self.runs.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)


PATHS = Paths()


# --- 运行时配置 ---------------------------------------------------------------------


@dataclass
class Config:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))

    # 硬性预算上限。账本会触发熔断并中止整个运行，而不是悄悄超支。
    budget_cny: float = 30.0

    # 单次故障的步数与 token 上限。**这是成功判据的一部分，不只是安全护栏**：
    # 一个跑了 40 次工具调用才"解决"的事故不叫解决，叫乱撞。
    max_steps: int = 14
    max_tokens_per_incident: int = 80_000

    # 熔断器：同一工具以相同参数连续失败多少次之后中止该次运行。
    repeat_failure_threshold: int = 3

    # 修复动作在判定失败之前允许经过幂等层重试多少次。
    max_repair_attempts: int = 3

    temperature: float = 0.0
    seed: int = 0

    def require_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY 未设置。\n"
                "PowerShell:  $env:DEEPSEEK_API_KEY = 'sk-...'"
            )
        return self.api_key


CONFIG = Config()
