# Codex CLI adapter (SCI-1715)

The adapter runs one headless `codex exec --json` session per theorem. The only existing
Python code changed is the CLI import and `ADAPTERS` entry. It uses `build_prompt`,
`DIAGNOSIS_SCHEMA`, `validate_diagnosis`, `Attempt` and `Budget` unchanged. The gate, pipeline,
report, existing adapters, configs and tests are untouched. It imports the pinned MCP package
and local-tool allowlist from `claude_code.py` rather than duplicating their policy.

## Capabilities

| Capability | Codex CLI 0.164.0 | Existing Claude Code adapter |
|---|---|---|
| Headless, one session per theorem | `exec --json --ephemeral` | `-p --output-format stream-json` |
| Requested model | `--model`, no adapter default | `--model` |
| Resolved model | Recorded if an event supplies `model`; normally absent in public exec stream | Init/modelUsage |
| Harness version | `codex --version` | `claude --version` |
| Shared prompt | Unchanged | Unchanged |
| Tool calling / MCP / Lean LSP | Yes; required per-run server, same local-tool allowlist | Yes |
| Structured diagnosis | `--output-schema`, shared schema and validation | `--json-schema` |
| Skills | Disabled for isolation; capability false | Capability true |
| Minutes | Process-group TERM, then KILL after up to 10 seconds | Process-group TERM/KILL |
| Turn cap | Unsupported; `turn_limit=false` | `--max-turns` |
| Dollar cap within session | Unsupported; `budget_usd=false` | `--max-budget-usd` |
| Usage | `turn.completed.usage`; missing/invalid usage is incomplete | Final result plus per-call lower bounds |
| Stops | done / gave-up / timeout / error | Also native max-turns / budget |
| Command restrictions | Workspace-write, no network, no approval escalation | Explicit Bash/tool allowlist |
| CI key contract | `api_key_env="CODEX_API_KEY"`, optional constructor `api_key` | `ANTHROPIC_API_KEY`, optional `api_key` |
| Key removed from agent commands | `shell_environment_policy.exclude` and default secret exclusions | `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` |

Codex's completed agent messages are counted in `Attempt.turns`. These are observable messages,
not an equivalent count of Claude API calls. A Codex `turn.started` encloses the entire tool
loop, so counting those would usually report one. No unsupported cap is simulated by counting
tool events. `max-turns` and `budget` are never invented from error strings.

Input usage includes cached input. The adapter's local normalizer subtracts
`cached_input_tokens` from `input_tokens` and records it under `cache_read`, avoiding double
charging. Output includes the model's output total; reasoning is not added again. Cache writes
are zero. No prices were added: the unchanged pipeline reports `no-price`, not zero dollars.
No final usage means `tokens_complete=false`; there are no public per-call lower bounds to
recover within an interrupted exec turn. A completed turn without usage can still be `done`:
usage completeness and proof acceptance are independent.

Malformed or missing final diagnosis becomes `None`. A validated `outcome=gave-up` or the
shared unstructured `GIVE UP:` marker produces `gave-up`. An interrupted/nonzero/failed session
is `error`, with bounded error text; a wall deadline takes precedence as `timeout`. The gate
alone decides acceptance of the edits.

## Isolation and keys

The adapter never opens personal config or auth files. `--ignore-user-config` tells Codex to
skip its user config, while `--ignore-rules` skips user/project execpolicy rules. CLI trust
overrides mark the workspace and ancestors untrusted, skipping their `.codex` config layers.
`project_doc_max_bytes=0` disables project instruction discovery. Plugins, apps, hooks,
memories, multi-agent tools, shell snapshots and host skill discovery are disabled per run.
Login shells are disabled so shell startup cannot restore personal environment settings.
These settings require 0.164.0; older Codex installations are not supported.

**Global AGENTS limitation in login mode:** an offline `codex debug prompt-input` check with
a temporary Codex home, a global sentinel AGENTS.md, a workspace sentinel AGENTS.md and
a project config sentinel confirmed that only the global sentinel was still injected.
`project_doc_max_bytes=0` does not suppress `$CODEX_HOME/AGENTS.md`. No documented CLI switch
was found to disable that while retaining the same Codex home for saved authentication.
The adapter does not edit that home or copy/symlink `auth.json` to work around it. Therefore
login mode cannot guarantee the requested absence of global instructions. Use key mode for
an empty per-attempt Codex home. Administrator-managed requirements/instructions remain under
Codex's control and cannot be overridden by this adapter.

With an explicit key, `_env` removes ambient model-key variables, sets `CODEX_API_KEY` only
in Codex's environment and points `CODEX_HOME` at an empty directory under the attempt log.
The key is never an argument, config value, prompt or file. Codex's
`shell_environment_policy.exclude=["CODEX_API_KEY", "OPENAI_API_KEY"]` keeps it out of shell
commands; `ignore_default_excludes=false` also enables the default secret-name exclusions.
This is the Codex equivalent of subprocess environment scrubbing, rather than a dedicated
scrub environment variable. MCP uses Codex's default restricted subprocess environment and
the explicit Lean project/thread variables; no model key is listed in its `env`/`env_vars`.
The CLI contract removes the key from the pipeline's environment before any builds
or gate subprocesses start. This branch was rebased onto main after that contract landed
(main at `c7358da`); its key handling is reused unchanged. A CLI integration test checks
the actual Codex registration, model selection, missing-key error and key transfer without
starting a repair or calling a model.

Sandbox configuration is `workspace-write`, no additional roots, `/tmp` and `$TMPDIR` writes
excluded, network off, approvals `never`. Context is readable without `--add-dir`, which would
grant unwanted writes. This is command write isolation, not a container: reads outside the
workspace remain possible, including a model deliberately opening an instruction file. MCP
servers are host processes outside the command sandbox. The Lean MCP has only the existing
local-tool allowlist; remote search tools and `lean_build` are not exposed. Broader command
restrictions equivalent to Claude's Bash allowlist are not available through these exec flags.

## Verification

After rebasing onto main at `c7358da`, `uv run pytest` passed: **144 passed, 2 skipped**
(the optional real-dalek-tree tests). All existing tests ran unchanged relative to main.

Unit tests use a fake `codex` executable on PATH and synthetic events matching the
[documented exec stream](https://developers.openai.com/codex/noninteractive). They cover a normal
finish, usage/pricing, malformed/missing diagnosis, give-up, missing usage, malformed events,
failure, model provenance, stderr pipe draining, process-group timeout including a child that
ignores TERM, profile configuration, key handling, registration and version. No tests call a
real model. No real model call was used to capture a fixture; the event samples are explicitly
synthetic, not presented as a recording.

The installed `codex exec --help` reports 0.164.0 and supports the flags used here. An offline
`codex debug prompt-input` check accepted the configuration overrides in minimal mode and
confirmed the project/global instruction behavior above. An LSP-mode debug initialization
failed to handshake with the MCP server in this restricted environment; real Lean LSP
operation and actual model/schema output remain unverified. No real-case repair or full target
Lean build was run. The full pytest suite builds only its existing toy Lean fixture.

Configuration references: [official config reference](https://developers.openai.com/codex/config-reference)
for environment exclusions, untrusted project layers and sandbox roots;
[instruction discovery](https://developers.openai.com/codex/guides/agents-md) for global/project guidance.

## Findings about the shared design

1. The protocol, shared prompt, schema, pricing and report support another harness without
   modification. Registration is the only shared-code edit needed for login mode.
2. DESIGN §7.1 lists context size as a declared capability; `base.py` and
   `check_capabilities` have no such requirement. No context-size value was invented.
3. DESIGN §7.3 and the target comment say turns mean the same across agents. Public exec
   events cannot provide Claude's API-call count or enforce its cap. Proposed follow-up:
   define observable turns and require budget capabilities where comparable caps matter.
   Current capability checking only tests tool calling / LSP, so it does not reject a Codex run
   configured with a turn or dollar cap. Also, the pipeline treats `error` with zero reported
   turns as infrastructure failure; Codex can have run tools before emitting an agent message.
   That heuristic may misclassify an interrupted Codex failure. These behaviors were not changed.
4. DESIGN §7.2's restricted editing/check/pregate tools and §7.5's sandbox-log/metering-proxy
   conformance are future architecture, not the current adapter contract. Synthetic adapter
   tests do not prove real proof repair or full sandbox conformance.
5. MCP policy constants live in the Claude-specific module. A neutral shared policy module
   would remove this dependency, but would require an out-of-scope change to existing code.
6. Global instruction suppression is harness-specific. Proposed follow-up: record isolation
   capabilities/limitations in provenance, or require key mode and an empty Codex home in CI.
7. `shfv doctor` and existing provenance wording in CLI docs refer specifically to Claude.
   Proposed follow-up: select the adapter for doctor/version/login checks and use neutral prose.
8. Existing target defaults still select Claude models, and there are no Codex prices. Pass
   `--model` explicitly; changing target defaults/prices belongs to a later comparison run.
9. The API-key contract makes key ownership explicit. No changes to base.py were
   needed here. The adapter exposes the attribute and constructor parameter and uses main's
   CLI key handling after the rebase.
10. AGENTS.md requires full tests before every commit touching adapters; this includes toy
    Lean builds even when real-case builds are deferred. Those tests are authorized explicitly
    by this task. It supplies no adapter-specific isolation/conformance policy beyond DESIGN.
11. The pipeline derives `task.lsp_tools` from adapter capabilities, rather than the chosen
    profile. Thus choosing `minimal` with an LSP-capable adapter still enables LSP. The adapter
    honors `task.lsp_tools` as passed; changing the pipeline for a true profile ablation is an
    out-of-scope follow-up.

No prohibited shared code was changed. The proposed changes above remain findings for review.

## Deferred PR 41 run

After reviewing the login-mode global-instruction limitation, the exact command is:

```sh
uv run shfv repair --case dalek-lean-verify/9d82afb9 --agent codex --model gpt-5.4-mini
```

With `CODEX_API_KEY` already provisioned securely, use:

```sh
uv run shfv repair --case dalek-lean-verify/9d82afb9 --agent codex --model gpt-5.4-mini --auth api-key
```

These select a model explicitly; availability for the account must be checked at run time.
Neither command was executed. They retain the existing target's tool profile and budgets;
Codex cannot enforce its turn or intra-session dollar limits.
