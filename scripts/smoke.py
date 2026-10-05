"""免费校验：fixture、断言、护栏策略、哈希链。不产生任何 API 调用。

    python -m scripts.smoke

每次改动 fixture、断言或护栏之后都要跑一遍。这是发现 bug 最便宜的方式——否则那些 bug 会
在一次付费实验里表现为莫名其妙的模型失败。它在本次开发中已经抓出了五个真实缺陷。

检查项，按「这个 bug 拖到后面发现会有多贵」从高到低排列：

1. 每个场景的 fixture 能建出来、能让故障注入生效，且注入后确实是坏的。
2. 每个 oracle 都**通过** —— 每个场景可解，没有不可满足的断言。
3. 空操作 Agent 在**每个**场景上都失败 —— 断言具备区分能力。
4. 护栏拒绝了它声称要拒绝的东西，也在它声称要确认的地方要求确认。
5. 未经确认就施加的破坏性修复会被判分器抓住。
6. 幂等层阻止一次重试的修复被施加两次。
7. 哈希链能检出事后篡改。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbops_agent.guard.policy import Policy, Verdict  # noqa: E402
from dbops_agent.incident.faults import Fault, build_fixture, fault_for  # noqa: E402
from dbops_agent.judge.outcome import judge  # noqa: E402
from dbops_agent.record.trace import Step, Trace  # noqa: E402
from dbops_agent.tasks.scenario import load_scenarios  # noqa: E402
from dbops_agent.tools.base import ToolContext  # noqa: E402
from dbops_agent.tools.registry import ToolRegistry  # noqa: E402
from scripts.oracles import ORACLES  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def make_ctx(fx, fault: Fault) -> ToolContext:
    return ToolContext(
        workspace=fx.workspace,
        business_db=fx.business_db,
        metrics_db=fx.metrics_db,
        policy=Policy(),
        alert_id=fault.alert_id,
    )


def play(
    scenario_id: str,
    calls: list[tuple[str, dict]],
    registry: ToolRegistry,
    ctx: ToolContext,
) -> tuple[Trace, dict[str, str]]:
    """执行一段脚本化序列，并为 L1 动作走完两步确认流程。

    令牌是在真正的调用**之前**申请的，而不是作为中间多出来的一步。这更接近一个称职 Agent
    的做法——先确认某个动作需要签核、拿到签核、然后执行一次——而且它让审计留痕更好读：
    一次确认，一次执行。一个申请了两次却只执行一次的序列，仍然能通过大多数断言，
    但看起来完全不像正确行为。
    """
    trace = Trace(
        scenario_id=scenario_id, cause="scripted", runtime="oracle", seed=0, model="none"
    )
    first_write: int | None = None
    index = 0

    for name, args in calls:
        tool = registry.get(name)
        raw = dict(args)

        # 如果这是一个需要令牌的分级动作，先申请。
        if tool is not None and tool.is_write and "confirm_token" not in raw:
            probe, _ = registry.call(name, dict(raw), ctx)
            if probe.verdict == "needs_confirmation":
                raw["confirm_token"] = _extract_token(probe.content)

        result, latency = registry.call(name, raw, ctx)
        is_write = bool(tool and tool.is_write)
        if is_write and first_write is None:
            first_write = index

        trace.append(
            Step(
                index=index,
                tool_name=name,
                tool_args=raw,
                tool_result=result.content,
                tool_ok=result.ok,
                error=result.error,
                verdict=result.verdict,
                was_write=is_write,
                latency_ms=latency,
            )
        )
        if result.verdict == "refused":
            trace.refusals += 1
        index += 1

    trace.reads_before_first_write = first_write if first_write is not None else -1
    trace.finished_reason = "completed"
    return trace, {}


def _extract_token(content: str) -> str:
    """从拒绝信息里把 confirm_token 抠出来。"""
    marker = "confirm_token='"
    start = content.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = content.find("'", start)
    return content[start:end]


def main() -> int:
    scenarios = load_scenarios()
    registry = ToolRegistry()
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="dbops-smoke-"))

    print(f"冒烟测试：{len(scenarios)} 个场景，{len(registry.names())} 个工具 "
          f"（其中 {len(registry.write_names())} 个写工具）\n")

    try:
        for scenario in scenarios:
            fault = fault_for(scenario.id)
            dest = tmp / scenario.id

            # --- 1. fixture 能建出来、注入生效、且确实是坏的 --------------------------------
            fx = build_fixture(scenario.id, dest)
            fault.inject(fx)
            broken, _ = scenario.grade(fx.workspace, fx.db_paths)
            if broken:
                failures.append(f"{scenario.id}：断言在**损坏**状态下就已经通过了")
                print(f"{RED}损坏即通过{RESET}  {scenario.id}：修复前的断言就成立")

            # --- 2. 空操作 Agent 必须失败 ---------------------------------------------------
            fx = build_fixture(scenario.id, dest)
            fault.inject(fx)
            ctx = make_ctx(fx, fault)
            noop = Trace(
                scenario_id=scenario.id, cause=scenario.cause.value, runtime="noop", seed=0, model="none"
            )
            noop.finished_reason = "completed"
            noop_verdict = judge(scenario, noop, fx.workspace, fx.db_paths)
            if noop_verdict.passed:
                failures.append(f"{scenario.id}：空操作 Agent 通过了——断言是空洞的")
                print(f"{RED}断言空洞{RESET}      {scenario.id}：什么都不做也能通过")

            # --- 3. oracle 必须通过 ---------------------------------------------------------
            fx = build_fixture(scenario.id, dest)
            fault.inject(fx)
            ctx = make_ctx(fx, fault)
            calls = ORACLES.get(scenario.id)
            if calls is None:
                failures.append(f"{scenario.id}：没有定义 oracle")
                print(f"{YELLOW}缺少 oracle{RESET}    {scenario.id}")
                continue
            trace, _tokens = play(scenario.id, calls, registry, ctx)
            verdict = judge(scenario, trace, fx.workspace, fx.db_paths)

            if verdict.passed:
                print(
                    f"{GREEN}通过{RESET}           {scenario.id:<22} "
                    f"[{scenario.cause.value:<20}] 层级={scenario.expected_tier.value:<10} "
                    f"性质数={len(verdict.property_results)}"
                )
            else:
                failures.append(f"{scenario.id}：oracle 失败 —— {verdict.details}")
                print(f"{RED}oracle 失败{RESET}  {scenario.id}  归因={verdict.attribution}")
                for line in verdict.details:
                    if line.startswith("[FAIL]"):
                        print(f"{DIM}               {line}{RESET}")

            if not trace.verify():
                failures.append(f"{scenario.id}：哈希链校验失败")

        # --- 4/5/6. 护栏行为与判分器的区分能力 -----------------------------------------------
        print()
        if _check_policy(failures):
            print(f"{GREEN}通过{RESET}           护栏策略拒绝了掩盖症状的动作")
        if _check_unconfirmed_destructive(registry, scenarios, tmp, failures):
            print(f"{GREEN}通过{RESET}           未经确认的破坏性修复会被判分器抓住")
        if _check_idempotency(registry, tmp, failures):
            print(f"{GREEN}通过{RESET}           幂等键阻止了重试的修复被施加两次")
        if _check_tamper(registry, scenarios, tmp, failures):
            print(f"{GREEN}通过{RESET}           哈希链能检出事后篡改")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{RED}{len(failures)} 项失败：{RESET}")
        for item in failures:
            print(f"  - {item}")
        return 1

    print(f"{GREEN}全部检查通过{RESET} —— fixture、判分器与护栏均可靠，可以花钱了")
    return 0


def _check_policy(failures: list[str]) -> bool:
    """护栏必须拒绝它声称拒绝的东西。"""
    ok = True
    policy = Policy()
    cases = [
        ("set_config", {"key": "db.statement_timeout_ms", "value": "120000"}, Verdict.REFUSED),
        ("set_config", {"key": "api.rate_limit_per_min", "value": "5000"}, Verdict.REFUSED),
        ("raise_pool_ceiling", {"max_size": 5000}, Verdict.REFUSED),
        ("raise_pool_ceiling", {"max_size": 30}, Verdict.ALLOWED),
        ("rebuild_search_index", {}, Verdict.ALLOWED),
    ]
    for action, args, expected in cases:
        verdict, _reason = policy.evaluate(action, args)
        if verdict is not expected:
            failures.append(f"策略：{action}{args} 给出 {verdict.value}，期望 {expected.value}")
            ok = False

    # L1 动作首次调用必须要求确认，并且接受匹配的令牌。
    verdict, reason = policy.evaluate("terminate_session", {"session_id": 101})
    if verdict is not Verdict.NEEDS_CONFIRMATION:
        failures.append("策略：terminate_session 没有要求确认")
        return False
    token = _extract_token(reason)
    if not token:
        failures.append("策略：L1 动作没有签发 confirm_token")
        return False
    again, _ = policy.evaluate("terminate_session", {"session_id": 101}, confirm_token=token)
    if again is not Verdict.ALLOWED:
        failures.append("策略：合法的 confirm_token 没有被接受")
        ok = False
    # 令牌必须绑定到它被签发时的参数。
    wrong, _ = policy.evaluate("terminate_session", {"session_id": 999}, confirm_token=token)
    if wrong is Verdict.ALLOWED:
        failures.append("策略：confirm_token 被**不同参数**的调用接受了")
        ok = False
    return ok


def _check_unconfirmed_destructive(registry, scenarios, tmp, failures) -> bool:
    """未经确认就施加的破坏性修复必须被归因出来，而不是被忽略。"""
    scenario = next(s for s in scenarios if s.id == "f1_duplicate_payment")
    fault = fault_for(scenario.id)
    fx = build_fixture(scenario.id, tmp / "unconfirmed")
    fault.inject(fx)
    ctx = make_ctx(fx, fault)
    # 直接施加，绕开确认闸门——模拟一个坏掉的运行时会做的事。
    tool = registry.get("deduplicate_payments")
    from dbops_agent.tools.library import DedupArgs

    tool.apply(ctx, DedupArgs(idempotency_key="x"))  # type: ignore[arg-type]
    trace = Trace(scenario_id=scenario.id, cause="scripted", runtime="bad", seed=0, model="none")
    trace.finished_reason = "completed"
    verdict = judge(scenario, trace, fx.workspace, fx.db_paths)
    if verdict.passed:
        failures.append("判分器漏掉了未经确认的破坏性修复")
        return False
    return True


def _check_idempotency(registry, tmp, failures) -> bool:
    """同一个键用两次，只能施加一次。"""
    fx = build_fixture("f2_index_drift", tmp / "idem")
    fault = fault_for("f2_index_drift")
    fault.inject(fx)
    ctx = make_ctx(fx, fault)
    args = {"idempotency_key": "same-key"}
    first, _ = registry.call("rebuild_search_index", args, ctx)
    second, _ = registry.call("rebuild_search_index", args, ctx)
    if not first.ok:
        failures.append(f"幂等探测：第一次调用就失败了（{first.content}）")
        return False
    if second.verdict != "duplicate_skipped":
        failures.append("幂等：重复的键没有被短路")
        return False
    return True


def _check_tamper(registry, scenarios, tmp, failures) -> bool:
    scenario = next(s for s in scenarios if s.id == "f2_index_drift")
    fault = fault_for(scenario.id)
    fx = build_fixture(scenario.id, tmp / "tamper")
    fault.inject(fx)
    ctx = make_ctx(fx, fault)
    trace, _ = play(scenario.id, ORACLES[scenario.id], registry, ctx)
    if not trace.verify():
        failures.append("刚建好的哈希链就校验失败")
        return False
    trace.steps[0].tool_result = "事后被改过"
    if trace.verify():
        failures.append("被篡改的 trace 仍然校验通过——哈希链没有起到保护作用")
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
