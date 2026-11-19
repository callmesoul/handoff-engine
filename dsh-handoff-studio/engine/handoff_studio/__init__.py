"""handoff-studio：把 DSH 自身当作 handoff 的实现执行器。

与 ``handoff`` 包的关系是**只读复用**：这里 import ``handoff.*`` 的既有模块
（Worker、Workspace、schema、store…），一行都不去改它们。原面板
（``handoffctl panel``）仍然按老路子跑，两套互不影响。

本包只补两样东西：

    runner.py   DshAgentRunner —— 与 CodexRunner/ZcodeRunner 同构的鸭子执行器，
                但执行者是 DSH 宿主里的一个 Agent（可选模型），不是子进程。
    service.py  StudioService —— PanelService 的子类，把执行器表换成
                ("dsh", "codex", "zcode") 并接上 DSH 侧提供的模型目录。
"""

from __future__ import annotations

__all__ = ["runner", "service"]
