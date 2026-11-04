"""The repair instructions, shared by all adapters so they are compared on the same task (§7)."""
from __future__ import annotations

from .base import RepairTask

GIVE_UP = "GIVE UP:"

# The agent's final report (Dana's review note 1; DESIGN.md §7.4, after Maestro's "report why you
# failed"). Advisory model output: it explains escalations and detects give-ups, never decides.
DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["repaired", "gave-up"]},
        "summary": {"type": "string", "description": "What broke and what you changed, in two or three sentences."},
        "failure_scope": {"type": "string", "enum": ["none", "local", "global"],
                          "description": "none if repaired; local: a missing lemma or step; global: a fundamental barrier."},
        "suspects_statement": {"type": "boolean",
                               "description": "true if you believe the statement is no longer true as written."},
        "statement_doubt": {"type": "string", "description": "Why you suspect the statement, or an empty string."},
        "helpers_added": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["outcome", "summary", "failure_scope", "suspects_statement", "statement_doubt", "helpers_added"],
    "additionalProperties": False,
}


def validate_diagnosis(d) -> dict | None:
    """The diagnosis if it matches DIAGNOSIS_SCHEMA, else None ("no diagnosis"; never breaks a run)."""
    if not isinstance(d, dict) or set(d) != set(DIAGNOSIS_SCHEMA["required"]):
        return None
    props = DIAGNOSIS_SCHEMA["properties"]
    for k, spec in props.items():
        v = d[k]
        if spec["type"] == "string" and not isinstance(v, str):
            return None
        if spec["type"] == "boolean" and not isinstance(v, bool):
            return None
        if spec["type"] == "array" and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return None
        if "enum" in spec and v not in spec["enum"]:
            return None
    return d


def build_prompt(task: RepairTask) -> str:
    errors = "\n\n".join(e.strip() for e in task.errors) or "(no error text recorded)"
    if len(errors) > 6000:
        errors = errors[:6000] + "\n… (truncated)"
    others = ""
    if task.repaired_before:
        others += ("\nOther theorems broken by the same change have already been repaired in this run; you may "
                   "use them but must not change them: " + ", ".join(f"`{t}`" for t in task.repaired_before) + "\n")
    if task.still_stubbed:
        others += ("\nOther theorems broken by the same change are temporarily replaced by `sorry`; they are "
                   "not your task and you must not touch them: " + ", ".join(f"`{t}`" for t in task.still_stubbed)
                   + ". Do not use them in your proof either: a proof that rests on one depends on `sorry` and "
                   "cannot be accepted.\n")
    tools = ("The Lean LSP tools (goal states, diagnostics, trying tactics) are available too."
             if task.lsp_tools else "Read the compiler's error messages for goal states.")
    context = ""
    if task.context_dir:
        context = (f"\nRead-only context for this change is in `{task.context_dir}` "
                   "(for example `extraction.diff`: how the extracted Lean code changed).\n")
    module = task.module or task.file[:-len(".lean")].replace("/", ".")
    retry = ""
    if task.attempt > 1:
        retry = (f"\nThis is attempt {task.attempt}. A previous attempt failed and its edits were rolled back:\n\n"
                 f"```\n{task.feedback[:3000] or '(no details)'}\n```\n"
                 + ("Do not patch the old proof this time: write a fresh proof from the goal state.\n"
                    if task.strategy == "fresh" else ""))
    ending = ("Then report the outcome in the structured output: whether you repaired it or gave up, what "
              "broke and what you changed, whether a failure is local or global, whether you suspect the "
              "statement itself, and any helper lemmas you added." if task.structured else
              f"End your reply with two or three sentences: what broke and what you changed. If you gave up, "
              f"end with a line starting `{GIVE_UP}` and the reason.")
    return f"""You are repairing a Lean 4 proof in a verified-Rust project (Rust → Charon/Aeneas → Lean).
The upstream change {task.trigger} (cause: {task.cause}) broke the proof of one theorem.
The working directory is the project, on toolchain {task.toolchain}, with Mathlib and Aeneas.

Broken theorem: `{task.theorem}` (written `{task.decl}`), in `{task.file}` near line {task.line}.
Its proof worked before the change. Now it fails with:

```
{errors}
```
{others}{context}{retry}
Check your work with `lake build {module}` (or `lake env lean {task.file}`). {tools}

Rules. They are checked mechanically afterwards, and any violation rejects the whole repair:
- Change only the proof of `{task.decl}`: the text after its `:=`. Do not change its statement,
  name or attributes, and do not change any other declaration, statement, definition or proof.
  Text outside proofs is compared byte for byte: do not edit, add or remove comments, docstrings,
  blank lines (except around a helper you add) or any other command (`open`, `attribute`,
  `#check`, ...) outside the proof, and do not leave a comment after the proof's last tactic (it
  counts as outside the proof).
- You may add helper lemmas (`theorem`/`lemma`) in the same file, placed before `{task.decl}` and
  separated from it by a blank line. Every helper must be used by the repaired proof.
- Never use `sorry`, `admit`, `axiom`, `native_decide`, `opaque`, `@[extern]`, `@[implemented_by]`,
  `set_option debug.*`, `maxHeartbeats 0`, or metaprogramming (`run_tac`, `#eval`, ...).
  You may raise the limit with `set_option maxHeartbeats N in` directly before the theorem
  (it is reported to the reviewers).
- Do not edit any other file: not the extracted code (`Funs.lean`, `Types.lean`), the external
  models, the lakefile, the toolchain or the manifest.
- If you conclude that the statement can no longer be proved as written, do not change it.
  Stop and report that you gave up, and why.

When you are done, make sure `{task.file}` builds with no errors. {ending}
"""
