"""Validate new policies without overwriting ANY historical result artifacts. No API."""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime

from scripts.validate import ROOT, source_fingerprints


def main():
    frozen = source_fingerprints()
    history = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "docs/results").glob("*.json")
        if p.name
        not in {
            "policy-v2-results.json",
            "policy-v2-freeze.json",
            "context-v2-results.json",
            "validation-v2-results.json",
        }
    }
    portable_history = {
        name: hashlib.sha256(
            (ROOT / "docs/results" / name).read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for name in history
    }
    ruff = ROOT / ".ruff-runtime/bin/ruff.exe"
    if not ruff.is_file():
        import shutil

        ruff = shutil.which("ruff")
    if not ruff:
        raise RuntimeError("Install development extra to run lint checks")
    commands = [
        [sys.executable, "-m", "scripts.smoke"],
        [sys.executable, "-m", "scripts.policy_v2_bench"],
        [sys.executable, "-m", "scripts.context_audit"],
        [sys.executable, "-m", "compileall", "-q", "dbops_agent", "scripts", "tests"],
        [str(ruff), "check", "dbops_agent", "scripts", "tests"],
        [str(ruff), "format", "--check", "dbops_agent", "scripts", "tests"],
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "diff", "--check"],
    ]
    temp = ROOT / "runs/validation-temp"
    temp.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", TEMP=str(temp), TMP=str(temp))
    results = []
    for command in commands:
        output = subprocess.run(
            command, cwd=ROOT, env=environment, capture_output=True, encoding="utf-8", timeout=240
        )
        results.append(
            {
                "command": command,
                "exit_code": output.returncode,
                "stdout": output.stdout,
                "stderr": output.stderr,
            }
        )
        print(f"{' '.join(command[1:])}: exit={output.returncode}", flush=True)
    unchanged = all(
        hashlib.sha256((ROOT / "docs/results" / name).read_bytes()).hexdigest() == value
        for name, value in history.items()
    )
    tests = re.search(r"Ran (\d+) tests", results[0]["stderr"])
    source_unchanged = frozen == source_fingerprints()
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "source_sha256": frozen,
        "source_unchanged": source_unchanged,
        "historical_results_unchanged": unchanged,
        "historical_result_sha256": portable_history,
        "historical_hash_normalization": "UTF-8 bytes, CRLF normalized to LF",
        "historical_results_unchanged_check": "local raw bytes before/after, without normalization",
        "unit_tests": int(tests[1]) if tests else None,
        "passed": source_unchanged and unchanged and all(r["exit_code"] == 0 for r in results),
        "scope": "known-case regression, offline context checks, no provider calls",
        "runs": results,
    }
    (ROOT / "docs/results/validation-v2-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"V2 validation passed={report['passed']}; tests={report['unit_tests']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
