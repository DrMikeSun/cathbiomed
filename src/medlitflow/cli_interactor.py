"""medlitflow cli_interactor.py — CLI 交互器（把 pipeline 事件转发到终端）。"""

from __future__ import annotations

import sys
from datetime import datetime

class CLIInteractor:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def notify(self, level: str, message: str) -> None:
        prefix = {
            "info":    "[INFO ]",
            "warning": "[WARN ]",
            "error":   "[ERROR]",
        }.get(level, "[INFO ]")
        stream = sys.stderr if level in ("warning", "error") else sys.stdout
        print(f"{self._ts()} {prefix} {message}", file=stream, flush=True)

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"{self._ts()} [DEBUG] {message}", flush=True)

    def progress(self, stage: str, current: int, total: int) -> None:
        if self.verbose:
            pct = current / total * 100 if total > 0 else 0
            print(
                f"{self._ts()} [PROG ] {stage}: "
                f"{current}/{total} ({pct:.1f}%)",
                flush=True,
            )

    def prompt(self, text: str, choices: list[str]) -> str:
        if not choices:
            return ""
        default = choices[0]
        if not sys.stdin.isatty():
            return default
        print(text, flush=True)
        choices_repr = "/".join(choices)
        while True:
            try:
                reply = input(
                    f"请选择 [{choices_repr}]（默认 {default}）: "
                ).strip()
            except EOFError:
                return default
            if not reply:
                return default
            if reply in choices:
                return reply
            print(f"无效输入，请输入 {choices_repr} 之一。")