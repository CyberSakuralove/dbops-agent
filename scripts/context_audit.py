"""Offline byte projection of archived calibration requests. Never calls a provider."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

from dbops_agent.runtimes.context import EvidenceContext
from scripts.validate import ROOT, source_fingerprints


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path, default=ROOT / "runs/real-llm-20261009/calibration-run"
    )
    args = parser.parse_args()
    sources = source_fingerprints()
    rows = []
    for name in ("underconfigured", "stalled"):
        path = args.input / f"{name}-provider.json"
        if not path.is_file():
            continue
        requests = json.loads(path.read_text(encoding="utf-8"))
        before, after = 0, 0
        unchanged = True
        for record in requests:
            request = record["request"]
            original = copy.deepcopy(request)
            projected = {**request, "messages": EvidenceContext().project(request["messages"])}
            before += len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            after += len(json.dumps(projected, ensure_ascii=False).encode("utf-8"))
            unchanged = unchanged and original == request
        rows.append(
            {
                "case": name,
                "responses": len(requests),
                "input_artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "original_serialized_utf8_bytes": before,
                "projected_serialized_utf8_bytes": after,
                "reduction_fraction": 1 - after / before,
                "raw_requests_unchanged": unchanged,
            }
        )
    report = {
        "scope": "offline serialization estimate on prior calibration; NOT token usage, fees, "
        "model correctness or a new LLM trial",
        "source_sha256": sources,
        "source_unchanged": source_fingerprints() == sources,
        "status": "observed" if rows else "private_calibration_not_available",
        "cases": rows,
        "provider_calls": 0,
    }
    (ROOT / "docs/results/context-v2-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"status": report["status"], "cases": rows}, ensure_ascii=False))
    return 0 if report["source_unchanged"] and all(r["raw_requests_unchanged"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
