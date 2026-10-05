"""结果验证、失败归因与报告。"""

from .outcome import Verdict, judge, read_audit
from .report import (
    ATTRIBUTION_BUCKETS,
    BUCKET_CN,
    GroupStats,
    bootstrap_ci,
    group_traces,
    pass_at_k,
    pass_pow_k,
    render,
)

__all__ = [
    "ATTRIBUTION_BUCKETS",
    "BUCKET_CN",
    "GroupStats",
    "Verdict",
    "bootstrap_ci",
    "group_traces",
    "judge",
    "pass_at_k",
    "pass_pow_k",
    "read_audit",
    "render",
]
