"""机器可验证断言：本项目判分所依据的事实基础。

两条设计规则，都承重：

1. **绝不判文字（transcript）。** Agent 说"重复行已删除"是一面之词，行数才是事实。
   这里每一条断言都在运行结束后针对外部状态求值。

2. **每条断言都必须能在不借助模型的情况下判定。** 如果某个成功条件无法写成下面几种
   形式，那说明这个场景不可判分，应该重写它——而不是丢给 LLM 判官。一旦交给判官，
   判官自身的可靠性就会变成新的疑点；而且规则判分是免费的，标注一致性要一直花钱。

断言可以指向任意一个数据库，因为这些场景的核心难点正是两个库会互相矛盾
（见 `incident/faults.py`）。`db="business"` 读真实数据，`db="metrics"` 读观测数据。
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

DbName = Literal["business", "metrics"]


class AssertionKind(StrEnum):
    SQL = "sql"  # 查询数据库，比对结果
    FILE = "file"  # 文件存在 / 包含某段文本
    FILE_ABSENT = "file_absent"  # 文件必须不存在（例如不该留下的痕迹）
    FILE_COUNT = "file_count"  # 文件中匹配了多少行


class Assertion(BaseModel):
    """一条关于外部状态的、机器可验证的断言。"""

    kind: AssertionKind
    description: str = ""

    # kind == sql
    db: DbName | None = None
    query: str | None = None
    expect: Any = None

    # kind == file / file_absent / file_count
    path: str | None = None
    contains: str | None = None

    def check(self, workspace: Path, db_paths: dict[str, Path]) -> tuple[bool, str]:
        """返回 (是否通过, 人类可读的说明)。"""
        if self.kind is AssertionKind.SQL:
            return self._check_sql(db_paths)
        if self.kind is AssertionKind.FILE:
            return self._check_file(workspace)
        if self.kind is AssertionKind.FILE_ABSENT:
            if not self.path:
                return False, "file_absent 断言缺少 `path`"
            ok = not (workspace / self.path).exists()
            return ok, f"{self.path} {'不存在（符合预期）' if ok else '仍然存在'}"
        if self.kind is AssertionKind.FILE_COUNT:
            if not self.path:
                return False, "file_count 断言缺少 `path`"
            target = workspace / self.path
            if not target.exists():
                return False, f"{self.path} 不存在"
            lines = [
                ln
                for ln in target.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip() and (self.contains is None or self.contains in ln)
            ]
            ok = len(lines) == self.expect
            return ok, f"期望 {self.expect} 行匹配，实际 {len(lines)} 行"
        return False, f"未知的断言类型 {self.kind}"

    # --- 内部实现 -------------------------------------------------------------------

    def _check_sql(self, db_paths: dict[str, Path]) -> tuple[bool, str]:
        if not self.query:
            return False, "sql 断言缺少 `query`"
        name = self.db or "business"
        path = db_paths.get(name)
        if path is None or not path.exists():
            return False, f"找不到数据库 {name!r}（路径 {path}）"

        try:
            # 只用只读 URI 连接，这样一条写错的断言也绝不可能破坏被测状态。
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = conn.execute(self.query).fetchall()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - 作为一条失败的断言暴露出来
            return False, f"SQL 执行出错: {exc}"

        # 单行单列按标量比对，否则按行列表比对。
        actual: Any
        if len(rows) == 1 and len(rows[0]) == 1:
            actual = rows[0][0]
        else:
            actual = [list(r) for r in rows]

        ok = actual == self.expect
        return ok, f"期望 {self.expect!r}，实际 {actual!r}"

    def _check_file(self, workspace: Path) -> tuple[bool, str]:
        if not self.path:
            return False, "file 断言缺少 `path`"
        target = workspace / self.path
        if not target.exists():
            return False, f"{self.path} 不存在"
        if self.contains is not None:
            text = target.read_text(encoding="utf-8", errors="replace")
            ok = self.contains in text
            return ok, f"{self.path} 中{'找到' if ok else '未找到'} `{self.contains}`"
        return True, f"{self.path} 存在"


class AssertionSet(BaseModel):
    """一组具名断言，让场景能够按性质分别报告结果。

    分组对报告很重要："修好了数据"和"没有破坏别的东西"是两条不同的声称，
    一个通过了前者却违反了后者的运行应该如实说出来，而不是塌缩成一个布尔值。
    """

    property: str = Field(description="这组断言验证的性质，例如「重复行已清理」。")
    assertions: list[Assertion] = Field(min_length=1)
