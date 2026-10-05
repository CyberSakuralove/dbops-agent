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


def source_fingerprints():
    paths = sorted(
        [
            *ROOT.glob("dbops_agent/**/*.py"),
            *ROOT.glob("scripts/*.py"),
            *ROOT.glob("tests/*.py"),
            ROOT / "pyproject.toml",
            ROOT / "dbops_agent/tasks/seed.yaml",
        ]
    )
    return {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
    }


def main():
    sources_before = source_fingerprints()
    ruff = shutil.which("ruff")
    if ruff is None:
        local = ROOT / ".ruff-runtime/bin/ruff.exe"
        if local.is_file():
            ruff = str(local)
    commands = [
        [sys.executable, "-m", "scripts.smoke"],
        [sys.executable, "-m", "scripts.audit_shortcuts"],
        [sys.executable, "-m", "scripts.pair_bench"],
        [sys.executable, "-m", "scripts.identity_bench"],
        [sys.executable, "-m", "compileall", "-q", "dbops_agent", "scripts", "tests"],
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "diff", "--check"],
    ]
    if ruff:
        commands.extend(
            [
                [ruff, "check", "dbops_agent", "scripts", "tests"],
                [ruff, "format", "--check", "dbops_agent", "scripts", "tests"],
            ]
        )
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    # Temp paths are scoped to this validation subprocess tree, so sandboxed
    # Windows profiles can run without writing to an inaccessible account temp.
    temp_root = ROOT / "runs/validation-temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    environment.update(TMP=str(temp_root), TEMP=str(temp_root), TMPDIR=str(temp_root))
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
    sources_after = source_fingerprints()
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "scope": "local synthetic SQLite, scripted actors, simulated test operator, no model API",
        "python": sys.version,
        "sqlite": sqlite3.sqlite_version,
        "pydantic": version("pydantic"),
        "pyyaml": version("pyyaml"),
        "unit_tests": int(tests[1]) if tests else None,
        "ruff_available": bool(ruff),
        "passed": all(r["exit_code"] == 0 for r in runs)
        and bool(ruff)
        and sources_before == sources_after,
        "source_unchanged_during_validation": sources_before == sources_after,
        "source_sha256": sources_before,
        "runs": runs,
    }
    output = ROOT / "docs/results/validation-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {output}; passed={report['passed']}; tests={report['unit_tests']}")
    if not ruff:
        print("Install the project's dev extra to run the required lint/format checks.")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
