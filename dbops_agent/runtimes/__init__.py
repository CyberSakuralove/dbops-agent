"""运行时适配器。

`BareLoop` 是控制组，也是目前唯一实现的运行时。框架适配器属于第 3 周的工作，
它们被列在路线图里而不是做成空壳放在这里——一个什么都不导入的空壳不构成任何证据。
"""

from .bare_loop import BareLoop, RunResult

__all__ = ["BareLoop", "RunResult"]
