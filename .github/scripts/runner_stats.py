"""Runner resource use during a replay on GitHub Actions (SCI-1717; DESIGN.md §3.4, §9.4).

  sample     every few seconds: memory in use, swap, disk in use under a path, and the RSS of the
             Lean processes (lean, lake, the kernel checkers), as CSV; runs until killed.
  summarize  peak memory and disk per pipeline stage, using the stage lines of `shfv repair`'s
             output ("  mm:ss  <stage>"), plus the run's own timings from report.json; Markdown.

Linux only (reads /proc/meminfo). Standard library only, so it runs before `uv sync`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

FIELDS = ["t", "mem_used_mb", "mem_avail_mb", "swap_used_mb", "disk_used_gb", "disk_free_gb",
          "lean_rss_mb", "lean_procs", "top_rss_mb", "top_cmd"]
LEAN = {"lean", "lake", "leanchecker", "lean4checker", "leantar"}
STAGE_RE = re.compile(r"^  (\d+):(\d\d)  (?![ →…])(.+?)(?:  \((\d+)s\))?$")


def meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        out[k] = int(v.split()[0]) // 1024  # MB
    return out


def processes() -> list[tuple[int, str]]:
    p = subprocess.run(["ps", "-eo", "rss=,comm="], capture_output=True, text=True)
    rows = []
    for line in p.stdout.splitlines():
        rss, _, comm = line.strip().partition(" ")
        if rss.isdigit():
            rows.append((int(rss) // 1024, comm.strip()))
    return rows


def sample(out: Path, path: Path, interval: float) -> None:
    new = not out.exists()
    with out.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        while True:
            m = meminfo()
            st = os.statvfs(path)
            procs = processes()
            lean = [r for r in procs if r[1] in LEAN]
            top = max(procs, default=(0, ""))
            w.writerow([int(time.time()), m["MemTotal"] - m["MemAvailable"], m["MemAvailable"],
                        m["SwapTotal"] - m["SwapFree"],
                        round((st.f_blocks - st.f_bfree) * st.f_frsize / 2**30, 1),
                        round(st.f_bavail * st.f_frsize / 2**30, 1),
                        sum(r[0] for r in lean), len(lean), top[0], top[1]])
            f.flush()
            time.sleep(interval)


def stages(log: Path) -> list[tuple[int, str]]:
    """(seconds since the run started, stage text) for each stage line of the pipeline's output."""
    out = []
    for line in log.read_text(errors="replace").splitlines():
        m = STAGE_RE.match(line)
        if m:
            out.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3)[:90]))
    return out


def summarize(samples: Path, log: Path, start: int, report: Path | None) -> str:
    rows = list(csv.DictReader(samples.open())) if samples.exists() else []
    for r in rows:
        for k in FIELDS[:-1]:
            r[k] = float(r[k])
    lines = ["## Runner resources", ""]
    if not rows:
        return "\n".join(lines + ["No samples were recorded.", ""])
    total = rows[0]["mem_used_mb"] + rows[0]["mem_avail_mb"]
    peak = max(rows, key=lambda r: r["mem_used_mb"])
    lines += [f"- Memory: peak {peak['mem_used_mb'] / 1024:.1f} GB in use of {total / 1024:.1f} GB "
              f"(lowest available {min(r['mem_avail_mb'] for r in rows) / 1024:.1f} GB); "
              f"peak swap {max(r['swap_used_mb'] for r in rows) / 1024:.1f} GB",
              f"- Lean processes: peak {max(r['lean_rss_mb'] for r in rows) / 1024:.1f} GB RSS in total, "
              f"up to {int(max(r['lean_procs'] for r in rows))} at once; largest single process "
              f"{max(r['top_rss_mb'] for r in rows) / 1024:.1f} GB",
              f"- Disk (work volume): peak {max(r['disk_used_gb'] for r in rows):.1f} GB used, "
              f"lowest free {min(r['disk_free_gb'] for r in rows):.1f} GB",
              f"- Samples: {len(rows)} over {(rows[-1]['t'] - rows[0]['t']) / 60:.0f} min", ""]
    st = stages(log) if log.exists() else []
    if st:
        # Some stage lines announce a stage, others report one that just ended, so each window runs
        # from one line to the next and is named by the line that closes it.
        end = int(rows[-1]["t"] - start)
        bounds = [(0, None)] + st + [(max(end, st[-1][0]), f"(after the last line: {st[-1][1]})")]
        lines += ["| Window | Closed by the line | Duration | Peak memory | Peak Lean RSS | Peak disk used |",
                  "|---|---|---|---|---|---|"]
        for (s, _), (e, text) in zip(bounds, bounds[1:]):
            win = [r for r in rows if s <= r["t"] - start <= e]
            peaks = (f"{max(r['mem_used_mb'] for r in win) / 1024:.1f} GB | "
                     f"{max(r['lean_rss_mb'] for r in win) / 1024:.1f} GB | "
                     f"{max(r['disk_used_gb'] for r in win):.1f} GB") if win else "– | – | –"
            lines.append(f"| {s // 60:02d}:{s % 60:02d}–{e // 60:02d}:{e % 60:02d} | {text.replace('|', '/')} | "
                         f"{fmt(e - s)} | {peaks} |")
        lines.append("")
    if report and report.exists():
        r = json.loads(report.read_text())
        t = r.get("totals", {})
        lines += [f"Run `{r.get('run_id')}`: verdict **{r.get('verdict')}**, "
                  f"wall {fmt(t.get('wall_seconds') or 0)}, cost "
                  + (f"${t['cost_usd']:.2f}" if t.get("cost_usd") is not None else "unknown"), ""]
        if timing := t.get("stages"):
            lines += ["Pipeline timings: " + ", ".join(f"{k.removesuffix('_s')} {fmt(v)}" for k, v in timing.items()), ""]
    return "\n".join(lines)


def fmt(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--path", type=Path, required=True, help="a path on the volume the workspaces live on")
    s.add_argument("--interval", type=float, default=5)
    m = sub.add_parser("summarize")
    m.add_argument("--samples", type=Path, required=True)
    m.add_argument("--log", type=Path, required=True, help="the output of shfv repair")
    m.add_argument("--start", type=int, required=True, help="epoch seconds when shfv repair started")
    m.add_argument("--report", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "sample":
        sample(a.out, a.path, a.interval)
    else:
        print(summarize(a.samples, a.log, a.start, a.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
