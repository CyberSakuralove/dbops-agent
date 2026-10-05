"""临时排查脚本：确认 Agent 的信息面和判分器读什么。用完即删。"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbops_agent.incident.faults import build_fixture  # noqa: E402
from dbops_agent.judge import outcome as outcome_mod  # noqa: E402
from dbops_agent.tools.registry import ToolRegistry  # noqa: E402


def main() -> None:
    print("=== 1. Agent 能调用的全部工具 ===")
    registry = ToolRegistry()
    for schema in registry.schemas():
        fn = schema["function"]
        tool = registry.get(fn["name"])
        kind = "写" if tool and tool.is_write else "只读"
        print(f"  [{kind}] {fn['name']:<24} 动作={tool.action if tool else None}")

    print()
    print("=== 2. Agent 的信息面（workspace 与 fixture 根目录）===")
    tmp = Path(tempfile.mkdtemp())
    try:
        fx = build_fixture("f1_duplicate_payment", tmp / "t")
        ws = sorted(p.name for p in fx.workspace.iterdir())
        root = sorted(p.name for p in fx.root.iterdir())
        print(f"  workspace/     : {ws or '(空)'}")
        print(f"  fixture 根目录 : {root}")
        print("  -> 两个数据库都在 fixture 根目录下，不在 workspace 内。")
        print("     工具拿得到它们；Agent 没有文件读取工具，所以读不到原始 SQL。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=== 3. 判分器实际读了哪些数据源 ===")
    src = Path(outcome_mod.__file__).read_text(encoding="utf-8")
    print(f"  判分器中出现 'metrics' 的次数: {src.count('metrics')}")
    print(f"  判分器访问的数据库: {sorted(set(__import__('re').findall(chr(100)+'b_paths\\[.(\\w+).\\]', src)))}")
    print("  -> 判分只看 properties（都在 business.db）和 repair_log（也在 business.db）。")

    print()
    print("=== 4. 五个场景里，有多少条断言涉及 metrics.db ===")
    from dbops_agent.tasks.scenario import load_scenarios

    for scenario in load_scenarios():
        total = sum(len(g.assertions) for g in scenario.properties)
        metric = sum(
            1
            for g in scenario.properties
            for a in g.assertions
            if getattr(a, "db", None) == "metrics"
        )
        print(f"  {scenario.id:<22} 断言 {total} 条，其中查 metrics 的 {metric} 条")


if __name__ == "__main__":
    main()
