"""The repair instructions, shared by all adapters so they are compared on the same task (§7)."""
from __future__ import annotations

from .base import RepairTask

GIVE_UP = "GIVE UP:"


def build_prompt(task: RepairTask) -> str:
    errors = "\n\n".join(e.strip() for e in task.errors) or "(no error text recorded)"
    if len(errors) > 6000:
        errors = errors[:6000] + "\n… (truncated)"
    others = ""
    if task.other_broken:
        others = ("\nOther theorems broken by the same change are temporarily replaced by `sorry`; they are "
                  "not your task and you must not touch them: " + ", ".join(f"`{t}`" for t in task.other_broken) + "\n")
    context = ""
    if task.context_dir:
        context = (f"\nRead-only context for this change is in `{task.context_dir}` "
                   "(for example `extraction.diff`: how the extracted Lean code changed).\n")
    module = task.file[:-len(".lean")].replace("/", ".")
    return f"""You are repairing a Lean 4 proof in a verified-Rust project (Rust → Charon/Aeneas → Lean).
The upstream change {task.trigger} (cause: {task.cause}) broke the proof of one theorem.
The working directory is the project, on toolchain {task.toolchain}, with Mathlib and Aeneas.

Broken theorem: `{task.theorem}` (written `{task.decl}`), in `{task.file}` near line {task.line}.
Its proof worked before the change. Now it fails with:

```
{errors}
```
{others}{context}
Check your work with `lake build {module}` (or `lake env lean {task.file}`). The Lean LSP tools
(goal states, diagnostics, trying tactics) are available too.

Rules. They are checked mechanically afterwards, and any violation rejects the whole repair:
- Change only the proof of `{task.decl}`: the text after its `:=`. Do not change its statement,
  name or attributes, and do not change any other declaration, statement, definition or proof.
- You may add helper lemmas (`theorem`/`lemma`) in the same file, placed before `{task.decl}`.
  Every helper must be used by the repaired proof.
- Never use `sorry`, `admit`, `axiom`, `native_decide`, `opaque`, `@[extern]`, `@[implemented_by]`,
  `set_option debug.*`, `maxHeartbeats 0`, or metaprogramming (`run_tac`, `#eval`, ...).
  You may raise the limit with `set_option maxHeartbeats N in` directly before the theorem
  (it is reported to the reviewers).
- Do not edit any other file: not the extracted code (`Funs.lean`, `Types.lean`), the external
  models, the lakefile, the toolchain or the manifest.
- If you conclude that the statement can no longer be proved as written, do not change it.
  Stop, and end your reply with a line starting `{GIVE_UP}` and the reason.

When you are done, make sure `{task.file}` builds with no errors. End your reply with two or
three sentences: what broke and what you changed.
"""
