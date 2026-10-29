# The model-free stages on Lean 4.31 and 4.35 (2026-10-29)

Part of SCI-1717. Status: **done**; no agent, no model cost. Tool commit: branch `dana/lean-4-31`.

## Why

Until now the gate and the replay ran on Lean 4.24–4.25 (dalek, early SPQR history) and v4.31 (SymCrypt's generated cases). Two candidate pilots are newer:
- **libsignal-verify** `main` (`468fcd6a`): Lean v4.31.0, Aeneas nightly-2026.09.30-0855ce1, VCVio, Mathlib v4.31.0. Three Lake libraries: `Libsignal` (with `srcDir = "rust"`), `Protocols`, `ProtocolsTest`.
- **SparsePostQuantumRatchet-verify** `main` (`d1b778e`): Lean v4.35.0-rc4, Aeneas nightly-2026.10.15-cd41995. Every one of its 369 files uses the Lean module system (`module`, `public import`, `@[expose] public section`). A shadow pilot on SPQR would run on this toolchain.

## Method

On each repo's `main`, used as both base and candidate:
1. Build, then take a gate snapshot (StmtCanon fingerprints) and run the audit.
2. Stub one proof (`:= by sorry`) as the broken state. Run candidates exactly as the pipeline does: `gate diff` → `scope` → `snapshot --apply --clean` (G3 clean build, G5 kernel replay) → `compare`.
3. Candidates: the original proof put back (a no-op, which must be accepted), then planted violations (must be rejected): a changed statement, a `sorry` inside the proof, and an axiom added to a trusted `*External.lean`.

Stubbed theorems: libsignal `derive_spec` (`rust/Libsignal/Spec/Protocol/Pqxdh/Derive.lean`) and `uakeInitiator_secure_dh` (`Protocols/PQXDH/Model/UAKE/Security.lean`, which rests on unproved lemmas); SPQR `compress_lt_two_pow` (`Spqr/Crypto/Fips203/Conversion/Compress.lean`) and `nttP_deltaP_eq_formulaP_0` (`Spqr/Crypto/Fips203/NTT/Kernel0.lean`, proved by `native_decide`). Laptop (Apple Silicon), Mathlib cache present.

## Results

| Stage | libsignal-verify (v4.31.0) | SPQR (v4.35.0-rc4) |
|---|---|---|
| Fetch cache + build | 20 s + 75 s, 2.8 GB peak | 26 s + 224 s, 4.4 GB peak |
| Snapshot (StmtCanon) | ✅ 60 modules, 801 records, 0 errors, 18 s (4.8 GB) | ✅ 374 modules, 2,605 records, 0 errors, 39 s (4.9 GB) |
| Source inventory | ✅ 75 theorems, none missing | ✅ 1,405 theorems, none missing |
| Audit | ✅ `no-change` on itself; a weakened statement → `changed` | ✅ `no-change`; a new `sorry` → `changed` (12 dependents); a head that does not build → `incomplete` |
| G3 clean build | ✅ 39 s | ✅ 137 s |
| G5 kernel replay | ✅ bundled `leanchecker`, 5 s | ✅ bundled `leanchecker`, 5 s |
| No-op repair | ✅ accepted; the Protocols one flagged 🏷️ inherited sorry (4 lemmas) | ✅ accepted, both; the `native_decide` one flagged 🏷️ native code (added afterwards, see below) |
| Changed statement | ✅ rejected (G0, G1) | ✅ rejected (G0, G1, G3: the weaker `≤` also broke dependents) |
| `sorry` in the proof | ✅ rejected (G0, G2) | ✅ rejected (G0, G2) |
| Axiom added to `*External.lean` | ✅ rejected (G0; never built) | ✅ rejected (G0; never built) |

The vendored StmtCanon compiled and ran unchanged on both toolchains, as did the module-system syntax (`public import` was already handled). One candidate takes 1–3 minutes, almost all of it the clean rebuild.

## What had to change

- **Libraries under a `srcDir`.** The gate assumed module `A.B` lives at `A/B.lean`. In libsignal-verify, `Libsignal.*` lives in `rust/`, and the gate skipped root modules it could not find. So **the whole Libsignal library would have left the frozen set silently**, and a statement change there would not have been checked. Now the gate looks modules up where Lean does: through the `LEAN_SRC_PATH` that `lake env` sets, which has one entry per library `srcDir`. `[lean] src_dirs` in the target config overrides the lookup. The gate fails closed when a root module has no file or when snapshots disagree on the layout ([DESIGN.md §4.5](../DESIGN.md#45-implementation-vendored-then-adapted)). The pipeline has the same lookup in `shfv/modpaths.py`.
- **Audit of trusted-model directories.** A trusted-model entry naming a directory (SymCrypt's `Intrinsics/`, libsignal's `Translated/`) was never reported as changed. This affected the report only.
- **Replay.** It now copies the root files of libraries that the change adds, such as libsignal PR 17's `Protocols.lean`. External models' support files are looked up under each `srcDir`.
- **Miner.** A commit that says "fix" no longer counts as a follow-up fix if it touches only proof files created after the trigger. This dropped libsignal PR 39 from PR 17, and dalek PR 792 from PR 782. PR 792 touched only a spec file added after PR 782. The committed PR 782 record now has the change commit as its only fix. Its replay is unaffected, because the replay's fixed state is the change commit.

## Findings for the owner

- **Native code on new Lean was accepted without a flag.** On Lean 4.35, `leanchecker` replays a module with `native_decide` completely. Each native proof is now its own auxiliary axiom, so the replay no longer needs compiled code. A repair whose base already used native code is accepted by G2 (`Lean.ofReduceBool` only where the base had it), and the `kernel_replay_incomplete` flag (DESIGN.md §4.3) only arises on older Lean, so such a repair got **no 🏷️ flag**. **Decided 2026-10-29:** a report-only `native_code` flag now names every repaired theorem that rests on native code as its base version did, whether or not the replay completes (`gates/compare.py`, `tests/gate/test_native_flag.py`). Re-running `compare` on the cached SPQR snapshots: the `nttP_deltaP_eq_formulaP_0` no-op is accepted with `native_code: ["Fips203.nttP_deltaP_eq_formulaP_0"]`; the plain SPQR and libsignal no-ops are accepted with no flag.
- **`@[step]` twins.** Recent Aeneas generates `T.mvcgen_spec` for each `@[step]` theorem `T`. It is fingerprinted as a separate frozen declaration, so one weakened statement is reported twice (libsignal `derive_spec`). It changes exactly when `T` changes, so this is noise and not a safety gap. Treating it as an auxiliary would need a planted test.
- **Fresh `git clone`s fail inside the Claude Code sandbox** (HTTP 400 on repos with many refs) when Lake fetches dependencies. Fetching each package at its pinned commit works. This affects local sessions only, not GitHub Actions.

## Layout assumptions and how each is handled

| Assumption | Where | How it is handled now |
|---|---|---|
| Module `A.B` is the file `A/B.lean` at the project root | gate, pipeline, replay | Lake's `LEAN_SRC_PATH` with a `src_dirs` override (gate: `gates/snapshot.py`, pipeline: `shfv/modpaths.py`). The miner and replay parse `srcDir` from the lakefile (`mine_history.lean_libs`). |
| Several `lean_lib`s | target config | `root_modules` and `build_targets` list each library to audit and build. A library that is not a root (SPQR `SrcTranslated`) is audited through imports. One that nothing imports (SPQR `Utils`, `Tests`) is not audited, by choice. |
| `lakefile.lean` vs `lakefile.toml` | all | The gate and pipeline ask Lake, so both work. The miner's `lean_libs` parses both with regexes, best effort. Hashes and protected files list both. |
| The Lake project is not the repo root | target config | `repo_lean_root` / `workspace_project` (SymCrypt: `SymCRust/lean`). |
| Path dependencies (`require … from "../.."`) | replay | Not supported: the replay reports "unsupported" (SymCrypt's Aeneas). Generated cases are used instead. |
| The library root is named like the library | gate, replay | Assumed. A `lean_lib` with `roots := #[...]` or `globs` other than its name needs its roots in `root_modules`, and the replay's new-root overlay would miss it. |
| One directory per top-level module name | gate | Required. A top-level name split across two `srcDir`s is refused (`gate-unavailable`). |
| Nested packages: Lake dependencies vendored inside the repo | gate, pipeline | The lookup keeps only project directories outside `.lake/`. A vendored package elsewhere in the repo would appear as a project directory. Not seen yet. |
| The Lean module system (`module`, `public`/`meta import`, `@[expose] public section`) | G0 parser, inventory | Imports are parsed. `@[expose] public section` is read as an attribute line before the next declaration, the same in both trees, so G0 is unaffected, and the inventory found every SPQR theorem. `public`/`private` modifiers on declarations are not parsed specially (not seen yet). |

## Layout check (`shfv doctor`)

A wrong `source_roots` fails safe: the gate refuses a diff outside it, and a root module with no file is `gate-unavailable`. A trusted model missing from `[protected] trusted_models` does not fail: it is just editable. `shfv doctor` now classifies every tracked `.lean` file of the target clone against the config, finding modules as the gate does (`shfv/layout.py`; CLI.md §7). Results on the three local clones, 2026-10-29:

| Target (clone commit) | Files | Audited | Trusted models | Extracted | Build config | Unclassified | Ignored | Warnings |
|---|---|---|---|---|---|---|---|---|
| dalek-lean-verify (`5f15b01c`) | 244 | 227 | 2 | 2 | 0 | 0 | 13 (`Tests/`, `Utils/`) | none |
| SPQR (`d1b778e8`) | 385 | 370 | 2 | 2 | 3 (`HeaderLinter/`, `SrcTranslated.lean`) | 0 | 8 (`Tests/`, `Utils/`, `scripts/Audit.lean`) | 2 |
| libsignal-verify (`468fcd6a`) | 69 | 39 | 21 (`Translated/`, incl. `Shared/`, `CurveProvider`) | 8 | 1 (`scripts/Ledger.lean`) | 0 | 0 | none |

Source directories from Lake: `.` for dalek and SPQR, `rust/` and `.` for libsignal-verify. Every file the root modules import is in the config, and no file is unclassified.

**SPQR's 2 warnings are not gaps.** `Spqr/Specs/Kdf/HkdfToSlice.lean` and `Spqr/Specs/Lib/StatePb.lean` declare spec axioms for external functions (`hkdf_to_slice_spec`, `encode_state_pb_spec`, `decode_state_pb_total`). The gate fingerprints axioms as frozen declarations (`kind: axiom` in the base snapshot), and G0 allows no edit outside proof bodies, so the agent cannot change them. `StatePb.lean` also holds a theorem the agent may need to repair, so protecting the whole file would be wrong. No target config was changed.
