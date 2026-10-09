"""Replan after one action. Only public graph, progress, probes and limits matter."""

from .access import ObservationLimit


def known(probe):
    return (
        bool(probe and probe.get("history"))
        and not probe["history_truncated"]
        and (
            0
            <= probe["observation_tick"] - probe["history"][-1]["tick"]
            <= probe["freshness_limit_ticks"]
        )
    )


def healthy(probe):
    return (
        known(probe)
        and probe["history"][-1]["oldest_age"] <= 2
        and (probe["history"][-1]["timed_out_total"] == 0)
    )


def roots(graph):
    if not graph:
        return set()
    waiting = {s["blocked_by"] for s in graph["sessions"] if s["blocked_by"] is not None}
    return {s["id"] for s in graph["sessions"] if s["blocked_by"] is None} & waiting


def stalled(previous, current):
    if not previous or not current or previous["observation_tick"] >= current["observation_tick"]:
        return []
    before = {p["session_id"]: p for p in previous["progress"]}
    after = {p["session_id"]: p for p in current["progress"]}
    # Paused descendants must first become roots; their old pause is not a diagnosis.
    return sorted(
        t
        for t in roots(previous) & roots(current)
        if t in before
        and t in after
        and before[t]["status"] == after[t]["status"] == "active"
        and before[t]["completed_units"] == after[t]["completed_units"]
    )


def finish(access, probe):
    classification = "inconclusive"
    if known(probe):
        classification = (
            "true_incident"
            if any(p["oldest_age"] > 2 or p["timed_out_total"] for p in probe["history"])
            else "false_alarm"
        )
    ack, _ = access.call(
        "acknowledge_alert",
        {
            "classification": classification,
            "rationale": "V2 public history and fresh probe",
            "idempotency_key": "v2-service-triage",
        },
    )
    if not healthy(probe):
        escalation, _ = access.call(
            "escalate_incident",
            {
                "reason": "Evidence incomplete or service still unhealthy; preserve and escalate",
                "idempotency_key": "v2-service-escalate",
            },
        )
        return ack.ok and escalation.ok
    return ack.ok


def run(access):
    _, probe = access.call("inspect_service")
    attempts, capacity_trial = {}, False
    status = "completed"
    previous = None
    try:
        if known(probe) and not healthy(probe):
            _, plan = access.call("read_capacity_plan")
            _, previous = access.call("observe_transactions")
            while known(probe) and not healthy(probe) and access.available(8, 9):
                _, current = access.call("observe_transactions")
                _, probe = access.call("inspect_service")
                if not known(probe) or healthy(probe):
                    break
                candidates = [t for t in stalled(previous, current) if attempts.get(t, 0) < 2]
                previous = current
                if candidates:
                    target = candidates[0]
                    attempts[target] = attempts.get(target, 0) + 1
                    result, _ = access.call(
                        "terminate_session",
                        {
                            "session_id": target,
                            "idempotency_key": f"v2-terminate-{target}",
                        },
                    )
                    if access.denied or (not result.ok and result.verdict != "approval_blocked"):
                        status = "action_refused"
                        break
                    # Approved graph changes invalidate the request. Fresh planning, never
                    # a blind retry of the old request, decides whether another request is needed.
                    _, probe = access.call("inspect_service")
                    continue
                if not roots(current) and not capacity_trial:
                    _, plan = access.call("read_capacity_plan")
                    if not plan:
                        status = "observation_limit"
                        break
                    ceiling = min(
                        plan["approved_max"],
                        plan["resource_budget"] // plan["units_per_connection"],
                    )
                    demand = probe["history"][-1]["pending"]
                    target = min(ceiling, plan["pool_size"] + max(1, demand))
                    capacity_trial = True
                    if target > plan["pool_size"]:
                        result, _ = access.call(
                            "raise_pool_ceiling",
                            {
                                "max_size": target,
                                "idempotency_key": "v2-capacity-trial",
                            },
                        )
                        if not result.ok:
                            status = "action_refused"
                            break
                        _, probe = access.call("inspect_service")
                        continue
                access.call("wait_for_service", {"ticks": 1})
                _, probe = access.call("inspect_service")
            # Reserve time to conclude. No additional intervention after the planning budget.
            while known(probe) and not healthy(probe) and access.available(4, 4):
                access.call("wait_for_service", {"ticks": 1})
                _, probe = access.call("inspect_service")
    except ObservationLimit:
        status = "observation_limit"
    complete = finish(access, probe) if access.available(2, 2) else False
    return {
        "status": status,
        "execution_complete": complete,
        "calls": access.calls,
        "capacity_trials": int(capacity_trial),
    }
