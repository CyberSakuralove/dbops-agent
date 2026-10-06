"""Counterfactual cases; templates are shared across faults, never keyed by labels."""

from .faults import build_fixture, fault_for

ALERTS = (
    "数据库服务报告异常，请核对业务与监控状态，安全处理并验证结果。",
    "值班系统发现数据库告警，原因尚不明确；请排查，健康时记录误报。",
    "收到业务运行异常通知，请确定是否存在故障并按权限处置。",
)


def build_case(scenario, dest, *, variant_seed: int, template: int = 0, variant: str | None = None):
    case = scenario.model_copy(deep=True)
    case.alert = ALERTS[template]
    if variant is not None and case.id in {"f1_duplicate_payment", "f3_lock_contention"}:
        from .variants import build_variant

        return build_variant(case, dest, seed=variant_seed, variant=variant)
    if variant is not None:
        raise ValueError("This family has no decision variants")
    fx = build_fixture(case.id, dest, variant_seed=variant_seed)
    fault_for(case.id).inject(fx)
    return case, fx
