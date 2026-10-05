"""Run the free validation set and save outputs plus source fingerprints. No model API."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ruff = shutil.which("ruff")
    if ruff is None:
        local = ROOT / ".ruff-runtime/bin/ruff.exe"
        if local.is_file():
            ruff = str(local)
    commands = [
        [sys.executable, "-m", "scripts.smoke"],
        [sys.executable, "-m", "scripts.audit_shortcuts"],
        [sys.executable, "-m", "scripts.pair_bench"],
        [sys.executable, "-m", "compileall", "-q", "dbops_agent", "scripts", "tests"],
        ["git", "diff", "--check"],
    ]
    if ruff:
        commands.extend(
            [
                [ruff, "check", "dbops_agent", "scripts", "tests"],
                [ruff, "format", "--check", "dbops_agent", "scripts", "tests"],
            ]
        )
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    runs = []
    for command in commands:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        runs.append(
            {
                "command": command,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        print(f"{Path(command[0]).name} {' '.join(command[1:])}: exit={result.returncode}")
    tests = re.search(r"Ran (\d+) tests", runs[0]["stderr"])
    source_paths = sorted(
        [
            *ROOT.glob("dbops_agent/**/*.py"),
            *ROOT.glob("scripts/*.py"),
            *ROOT.glob("tests/*.py"),
            ROOT / "pyproject.toml",
            ROOT / "dbops_agent/tasks/seed.yaml",
        ]
    )
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "scope": "local synthetic SQLite, scripted actors, simulated test operator, no model API",
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "pydantic": version("pydantic"),
        "pyyaml": version("pyyaml"),
        "unit_tests": int(tests[1]) if tests else None,
        "ruff_available": bool(ruff),
        "passed": all(r["exit_code"] == 0 for r in runs) and bool(ruff),
        "source_sha256": {
            str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source_paths
        },
        "runs": runs,
    }
    output = ROOT / "docs/validation-results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {output}; passed={report['passed']}; tests={report['unit_tests']}")
    if not ruff:
        print("Install the project's dev extra to run the required lint/format checks.")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
