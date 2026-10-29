"""Readable live progress on the terminal: one line per stage, one updating line for the agent."""
from __future__ import annotations

import sys
import time

CHECKS = [("G0", "scope"), ("G3", "clean rebuild"), ("G1", "statements unchanged"),
          ("G2", "no new axioms/sorry"), ("G5", "lean4checker")]


class Progress:
    def __init__(self, stream=sys.stdout):
        self.s = stream
        self.t0 = time.time()
        self.tty = stream.isatty()
        self._live = False

    def _stamp(self) -> str:
        s = int(time.time() - self.t0)
        return f"{s // 60:02d}:{s % 60:02d}"

    def _print(self, text: str) -> None:
        if self._live:
            self.done_live()
        print(text, file=self.s, flush=True)

    def header(self, text: str) -> None:
        self._print(text)

    def step(self, text: str, took: float | None = None) -> None:
        suffix = f"  ({int(took)}s)" if took is not None else ""
        self._print(f"  {self._stamp()}  {text}{suffix}")

    def result(self, text: str) -> None:
        self._print(f"  {self._stamp()}      → {text}")

    def live(self, text: str) -> None:
        # One line: an agent's command can span several (a heredoc, a multi-line `lake env lean`).
        line = f"  {self._stamp()}      … {' '.join(text.split())}"
        if self.tty:
            print("\r\033[K" + line[:150], end="", file=self.s, flush=True)
            self._live = True
        else:
            print(line[:150], file=self.s, flush=True)

    def done_live(self) -> None:
        if self._live:
            print(file=self.s, flush=True)
            self._live = False

    def gate_line(self, g: dict) -> None:
        parts = []
        for cid, label in CHECKS:
            c = g["checks"].get(cid)
            if c is None:
                parts.append(f"{label} –")
            elif c.get("ok") is None:
                parts.append(f"{label} – not run")
            else:
                partial = c["ok"] and (c.get("info") or {}).get("incomplete")
                parts.append(f"{label} {'✓' if c['ok'] else '✗'}" + (" (incomplete: native code)" if partial else ""))
        self.result(" · ".join(parts) + f" → {g['verdict'].upper()}")

    def final(self, text: str) -> None:
        self._print(text)
