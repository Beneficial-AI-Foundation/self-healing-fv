"""`shfv doctor` shows why a check failed, and .github/scripts/install_toolchain.sh installs Lean
toolchains with retries before anything uses them (issue 38)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from shfv import cli

CONFIG = cli.ROOT / "targets" / "dalek-lean-verify.toml"
INSTALL = cli.ROOT / ".github" / "scripts" / "install_toolchain.sh"
ELAN_FAIL = ("warning: could not canonicalize path: '/home/runner/.elan/toolchains'\n"
             "info: downloading component 'lean'\n"
             "error: could not download file from 'https://github.com/leanprover/lean4/releases/x.tar.zst'\n"
             "error: caused by: HTTP status server error (503 Service Unavailable)\n")


def result(returncode=0, stdout="", stderr="", timed_out=False):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out)


def test_failure_shows_stderr_past_a_leading_warning():
    out = cli.check_line("lake", result(1, stderr=ELAN_FAIL)).splitlines()
    assert out[0] == "✗ lake: missing or failing (exit 1)"
    assert out[1:] == ["    " + ln for ln in ELAN_FAIL.strip().splitlines()]
    assert any("503 Service Unavailable" in ln for ln in out)  # the cause, not only the first line


def test_failure_with_empty_stderr_shows_stdout():
    out = cli.check_line("uv", result(2, stdout="error: something broke\nhint: try again\n"))
    assert out == "✗ uv: missing or failing (exit 2)\n    error: something broke\n    hint: try again"


def test_failure_keeps_the_last_lines_and_says_how_many_were_cut():
    err = "\n".join(f"line {i}" for i in range(1, 31))
    out = cli.check_line("lake", result(1, stderr=err)).splitlines()
    assert out[1] == f"    … {30 - cli.DOCTOR_TAIL} earlier lines"
    assert out[2:] == [f"    line {i}" for i in range(31 - cli.DOCTOR_TAIL, 31)]


def test_failure_without_output_or_by_timeout():
    assert cli.check_line("git", result(1)) == "✗ git: missing or failing (exit 1), no output"
    out = cli.check_line("lake", result(-1, stderr="partial\n", timed_out=True))
    assert out == f"✗ lake: missing or failing (timed out after {cli.DOCTOR_TIMEOUT} s)\n    partial"


def test_passing_check_stays_one_line():
    long = "Lake version 5.0.0-src+eb6cafb (Lean version 4.35.0) " + "x" * 100
    out = cli.check_line("lake", result(0, stdout=long + "\nsecond line\n", stderr="warning: noise\n"))
    assert out == f"✓ lake: {long[:80]}"
    assert cli.check_line("uvx", result(0, stderr="only stderr\nmore\n")) == "✓ uvx: only stderr"


def test_doctor_prints_the_failing_check_in_full(monkeypatch, capsys, tmp_path):
    from gates import proc
    monkeypatch.setattr(proc, "run", lambda cmd, *a, **k: result(1, stderr=ELAN_FAIL) if cmd[0] == "lake"
                        else result(0, stdout=f"{cmd[0]} 1.0\nmore"))
    assert cli.doctor(CONFIG, tmp_path) == 1
    out = capsys.readouterr().out
    assert "✗ lake: missing or failing (exit 1)\n    warning: could not canonicalize path" in out
    assert "    error: caused by: HTTP status server error (503 Service Unavailable)\n" in out
    assert "✓ uv: uv 1.0\n" in out and "more" not in out


def test_doctor_missing_executable(monkeypatch, capsys, tmp_path):
    from gates import proc

    def run(cmd, *a, **k):
        if cmd[0] == "elan":
            raise FileNotFoundError(2, "No such file or directory", "elan")
        return result(0, stdout="ok")
    monkeypatch.setattr(proc, "run", run)
    assert cli.doctor(CONFIG, tmp_path) == 1
    assert "✗ elan (Lean toolchains): missing or failing: [Errno 2] No such file or directory: 'elan'" \
        in capsys.readouterr().out


# install_toolchain.sh, run against a fake elan that fails a given number of times per toolchain.

def fake_elan(tmp_path: Path, failures: dict[str, int]) -> dict:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    elan = bin_ / "elan"
    elan.write_text("#!/usr/bin/env bash\n"
                    'echo "$*" >> "$FAKE_LOG"\n'
                    'n=$(grep -c -x -F "$*" "$FAKE_LOG")\n'
                    'limit=$(sed -n "s|^$3=||p" "$FAKE_FAILURES")\n'
                    'if [ "$n" -le "${limit:-0}" ]; then\n'
                    '  echo "warning: could not canonicalize path" >&2\n'
                    '  echo "error: download failed for $3 (try $n)" >&2; exit 1\n'
                    "fi\n"
                    'echo "info: installed $3"\n')
    elan.chmod(0o755)
    (tmp_path / "failures").write_text("".join(f"{k}={v}\n" for k, v in failures.items()))
    return {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_LOG": str(tmp_path / "log"),
            "FAKE_FAILURES": str(tmp_path / "failures"), "SHFV_INSTALL_DELAY": "0"}


def install(env: dict, *toolchains: str):
    return subprocess.run(["bash", str(INSTALL), *toolchains], env=env, capture_output=True, text=True)


def test_install_retries_until_the_download_works(tmp_path):
    env = fake_elan(tmp_path, {"leanprover/lean4:v4.35.0": 2})
    r = install(env, "leanprover/lean4:v4.35.0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "log").read_text().count("toolchain install leanprover/lean4:v4.35.0") == 3
    assert "attempt 3 of 3" in r.stdout and "info: installed leanprover/lean4:v4.35.0" in r.stdout
    assert "download failed for leanprover/lean4:v4.35.0 (try 2)" in r.stderr  # elan's output is kept


def test_install_fails_after_the_last_attempt_with_elans_output(tmp_path):
    env = fake_elan(tmp_path, {"leanprover/lean4:v4.35.0": 5})
    r = install(env, "leanprover/lean4:v4.35.0")
    assert r.returncode == 1
    assert (tmp_path / "log").read_text().count("\n") == 3
    assert "::error::could not install the Lean toolchain leanprover/lean4:v4.35.0 after 3 attempts" in r.stdout
    assert r.stderr.count("could not canonicalize path") == 3 and "(try 3)" in r.stderr


def test_install_several_toolchains_reports_each_failure(tmp_path):
    env = fake_elan(tmp_path, {"leanprover/lean4:v4.34.1": 9})
    env["SHFV_INSTALL_ATTEMPTS"] = "2"
    r = install(env, "leanprover/lean4:v4.35.0", "leanprover/lean4:v4.34.1")
    assert r.returncode == 1
    assert "info: installed leanprover/lean4:v4.35.0" in r.stdout
    assert "Lean toolchain leanprover/lean4:v4.34.1 after 2 attempts" in r.stdout
    assert "v4.35.0 after" not in r.stdout


def test_install_needs_a_toolchain(tmp_path):
    r = install(fake_elan(tmp_path, {}))
    assert r.returncode == 2 and "usage:" in r.stderr
