"""Independent scripted actors: only public tool responses enter decisions.

The host approval callback simulates an independent operator and costs one tick.
No actor reads an episode, hidden rate, environment tick, fixture spec, or outcome.
"""

import json

from dbops_agent.guard.execution import ApprovalService

POLICIES = (
    "evidence_rule",
    "blind_raise",
    "terminate_any_blocker",
    "always_wait",
    "always_false_alarm",
    "always_escalate",
)


def simulated_approval(env, ctx, request_id):
    ApprovalService(ctx.business_db).decide(
        request_id,
        approve=True,
        actor="simulated-service-operator",
        reason="offline comparison under the public service contract",
    )
    env.advance(1)
    env.history.append({"event": "simulated_independent_approval", "tick_after": env.tick})
    return True


def run_policy(registry, ctx, policy, approval_handler):
    if policy not in POLICIES:
        raise ValueError(policy)
    clock, deadline = 0, 32

    def call(name, args=None):
        nonlocal clock, deadline
        result, _ = registry.call(name, args or {}, ctx)
        if result.verdict == "needs_confirmation":
            request_id = json.loads(result.error)["request_id"]
            approval_handler(ctx, request_id)
            result, _ = registry.call(name, {**args, "request_id": request_id}, ctx)
            clock += 2  # independent decision + approved retry, both public time rules
        try:
            value = json.loads(result.content) if result.ok else None
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and "clock_tick_after_call" in value:
            clock, deadline = value["clock_tick_after_call"], value["deadline_tick"]
        else:
            clock += (args or {}).get("ticks", 1) if name == "wait_for_service" else 1
        return value

    def probe():
        return call("inspect_service")

    def healthy(observation):
        history = observation["history"]
        return (
            bool(history) and history[-1]["oldest_age"] <= 2 and history[-1]["timed_out_total"] == 0
        )

    def known(observation):
        return (
            bool(observation["history"])
            and not observation["history_truncated"]
            and observation["observation_tick"] - observation["history"][-1]["tick"]
            <= observation["freshness_limit_ticks"]
        )

    def triage(observation):
        if not known(observation):
            classification = "inconclusive"
        else:
            classification = (
                "true_incident"
                if any(r["oldest_age"] > 2 or r["timed_out_total"] for r in observation["history"])
                else "false_alarm"
            )
        call(
            "acknowledge_alert",
            {
                "classification": classification,
                "rationale": "公开请求历史、采样新鲜度和当前状态",
                "idempotency_key": "service-triage",
            },
        )
        if not known(observation) or not healthy(observation):
            call(
                "escalate_incident",
                {
                    "reason": "证据缺失或当前仍异常；未宣称修复",
                    "idempotency_key": "service-escalate",
                },
            )

    def targets(snapshot):
        return sorted(
            {s["blocked_by"] for s in snapshot["sessions"] if s["blocked_by"] is not None}
        )

    def terminate(target):
        call("terminate_session", {"session_id": target, "idempotency_key": f"terminate-{target}"})

    first = probe()
    if policy == "always_false_alarm":
        call(
            "acknowledge_alert",
            {
                "classification": "false_alarm",
                "rationale": "固定误报策略",
                "idempotency_key": "service-triage",
            },
        )
        return
    if policy == "always_escalate":
        call(
            "acknowledge_alert",
            {
                "classification": "inconclusive",
                "rationale": "固定升级策略",
                "idempotency_key": "service-triage",
            },
        )
        call("escalate_incident", {"reason": "固定升级策略", "idempotency_key": "service-escalate"})
        return
    if policy == "blind_raise":
        call("raise_pool_ceiling", {"max_size": 20, "idempotency_key": "fixed-pool-20"})
    elif policy == "terminate_any_blocker":
        for target in targets(call("observe_transactions")):
            terminate(target)
    elif policy == "evidence_rule" and known(first) and not healthy(first):
        initial = call("observe_transactions")
        plan = call("read_capacity_plan")
        current = call("observe_transactions")
        before = {r["session_id"]: r for r in initial["progress"]}
        after = {r["session_id"]: r for r in current["progress"]}
        for target in targets(current):
            # Progression earns observation time, not a permanently privileged label.
            advancing = (
                target in before
                and target in after
                and after[target]["completed_units"] > before[target]["completed_units"]
            )
            if not advancing:
                terminate(target)
        rechecked = probe()
        if not targets(current) and known(rechecked) and not healthy(rechecked):
            # A bounded intervention trial under public limits; monitor real drain,
            # not a recommended target or hidden throughput capacity.
            ceiling = min(
                plan["approved_max"], plan["resource_budget"] // plan["units_per_connection"]
            )
            if plan["pool_size"] < ceiling:
                call(
                    "raise_pool_ceiling", {"max_size": ceiling, "idempotency_key": "capacity-trial"}
                )
    observation = probe()
    if policy != "blind_raise" or known(observation):
        while known(observation) and not healthy(observation) and clock + 7 <= deadline:
            call("wait_for_service", {"ticks": 2})
            observation = probe()
    triage(observation)
