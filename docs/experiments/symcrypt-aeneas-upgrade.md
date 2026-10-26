# Experiment: forward Aeneas upgrade of SymCrypt (held-out cases)

Status: first ladder done (2026-10-23). Linear: SCI-1714 (data), SCI-1715 (evaluation).

## Why

SymCrypt (`microsoft/SymCrypt`, branch `feature/verifiedcrypto`) is the held-out repo, but its public history has no proof breaks to mine: the whole Lean project arrived in one commit on 2026-07-12 ("Publish ML-KEM and SHA3 proofs"); only 3 commits on the branch touch Lean files. Instead we generate cases going forward: upgrade its Aeneas/Charon, re-run its own extraction (`make extract`), and see what breaks. The gates judge repairs; there is no human fix to compare with.

## Setup

SymCrypt requires Aeneas as a path dependency at `../../../aeneas/backends/lean` (relative to `SymCRust/lean/`) and its Makefile expects `AENEAS_HOME=../../aeneas`, `CHARON_HOME=../../aeneas/charon` (relative to `SymCRust/`). So each state is a directory holding `SymCrypt/` and `aeneas/` side by side (under `work/symcrypt-upgrade/`, gitignored):

```
work/symcrypt-upgrade/
  aeneas-src/            clone of AeneasVerif/aeneas (worktrees below)
  base/aeneas            worktree @ d71d2e3f   (SymCrypt's published pin, see Code/EXTRACTION.md)
  base/SymCrypt/SymCRust from `git archive origin/feature/verifiedcrypto SymCRust`
  upgrade/aeneas         worktree @ cd419953   (nightly 2026-10-15; Charon pin 26abff02)
  upgrade/SymCrypt/SymCRust
  logs/
```

Base needs only Aeneas's Lean library. Upgrade needs Charon and Aeneas binaries (no macOS release binaries exist; build from source):

```sh
# OCaml (see "macOS notes" for why this switch is configured this way)
ac_cv_func_pipe2=no MACOSX_DEPLOYMENT_TARGET=26.0 opam switch create 5.3.0-macos26 ocaml-base-compiler.5.3.0
eval "$(opam env --switch=5.3.0-macos26 --set-switch)"
cd upgrade/aeneas
opam install ./src --deps-only -y
gmake setup-charon        # clones Charon at charon-pin, builds with its Rust nightly
git -C charon submodule update --init --recursive && gmake setup-charon   # if minirust is missing
gmake                     # -> bin/aeneas

# Re-extract and build SymCrypt with the new Aeneas
cd ../SymCrypt/SymCRust
cp ../../aeneas/backends/lean/lean-toolchain lean/lean-toolchain   # deliberate Lean bump (Makefile check)
PATH="$(brew --prefix gnu-sed)/libexec/gnubin:$PATH" gmake extract
cd lean && lake update && lake exe cache get && lake build
```

## macOS notes (this machine)

- The macOS 27 SDK on a macOS 26.7 machine makes OCaml detect `pipe2()`, which is missing at run time: any `Unix.pipe` segfaults (seen as `ocamlbuild` / `topkg` exit 255 or 139). Configure the switch with `ac_cv_func_pipe2=no MACOSX_DEPLOYMENT_TARGET=26.0`. Not needed once SDK and OS match.
- Charon's Makefile needs GNU make (`brew install make`, use `gmake`); opam needs `gmp` and `pkgconf` from Homebrew; SymCrypt's postprocess uses GNU `sed -i`.
- `make setup-charon` can report success even when the Rust build failed: check that `charon/bin/charon` exists. Don't run two setups at once (parallel `rustup` installs of the same nightly corrupt each other).
- `dune build @doc` in charon-ml needs `odoc`; only the docs fail without it.

## Results so far

- Base (published state, Aeneas d71d2e3f, Lean v4.31.0): `lake build` green, 1900 jobs, 0 `sorry`, 10.4 min.
- Extraction with Aeneas cd419953: succeeds in 67 s. Diff vs. the published `Code/`: `Funs.lean` 51 signature + 23 body changes; `Types.lean` 7 type changes; `FunsExternal_Template.lean` 12 signature changes (`common.InPlaceOrDisjointBuffer.*`) and `TypesExternal_Template.lean` 1, while the hand-written `FunsExternal.lean`/`TypesExternal.lean` are unchanged (now out of sync). Generated files now use the Lean module system (`module`, `public import`, `@[expose] public section`).
- Upgrade build (Lean v4.35.0-rc4, Mathlib 1f414401): fails after 21 s with 19 errors in 5 modules, before the extracted code is reached:

| Layer | Errors | Repair class |
|---|---|---|
| `Intrinsics/BVRealize.lean` | imports a Lean core module removed in v4.35 | import repair |
| `Intrinsics/Simd.lean`, `Spec/AES/ArrayBridge.lean` | `Data.ListN.ListN` no longer accepts `⟨...⟩`; rewrites/unification fail | definition changes in trusted models: escalate |
| `Symcrust/Properties/MLKEM/Helpers/KPKEStructure.lean`, `Spec/SHA3/Properties.lean` | `split` failed (7), unsolved goals | proof repair |
| not reached | module-system imports of hand-written `*External.lean`; 12 changed external signatures; specs over 51 changed functions | escalate (trusted externals), then statement checks and proof repair |

## Automation

- `replay/generate_aeneas_upgrade.py --config symcrypt --repo repos/SymCrypt --aeneas-src work/symcrypt-upgrade/aeneas-src --aeneas <commit>` builds the upgraded tree under `work/symcrypt-gen/<commit>/` (about 5-7 min: Charon at the Aeneas pin, Aeneas, `make extract`, Lake setup). Every step is checked by its outputs.
- `replay/replay_case.py --generated symcrypt/gen-aeneas-<commit> --config symcrypt --base-tree work/symcrypt-upgrade/base --upgraded-tree work/symcrypt-gen/<commit> --repo-subdir SymCrypt` analyzes it like a historical broken state (rounds of `sorry`, statement / body / file errors, blocked modules). Results: `data/replay/symcrypt/gen-aeneas-*.json`.

## Ladder (base: Aeneas d71d2e3f, Lean v4.31.0, green)

Aeneas kept Lean v4.31.0 and Mathlib until 73b7cf9f (2026-10-12) and switched its output to the Lean module system at a3446f52 (2026-09-23), so the steps isolate one kind of change each.

| Aeneas | Date | Lean | Extraction vs. published | Outcome |
|---|---|---|---|---|
| 6e167c9b | 07-24 | v4.31 | identical | **proof repair**: 3 broken proofs (`agrind` failed; `Case tag h1 not found` from `step` naming), nothing blocked; 10 min |
| 5d08da45 | 08-19 | v4.31 | fails | **extraction fails**: Charon type errors on `core::slice::iter` (mismatched trait clause), ~20 Aeneas internal errors; 1203 functions translated instead of 508. A toolchain regression, fixed by later releases. |
| 12a018bb | 09-22 | v4.31 | changed | **large break**: 228 broken proofs, 21 statement-broken declarations in the hand-written intrinsic models (`Intrinsics/Simd.lean`, `Intrinsics/Bytes.lean`: definitional equalities such as `subArray_val`, `u32le_width` no longer hold), 136 blocked modules; 8 rounds, 19 min |
| 557f7a15 | 09-24 | v4.31 | module system | **setup failure**: generated `Types.lean` (a `module`) cannot import the hand-written non-module `TypesExternal`; 7 proofs broken below it, 140 modules blocked |
| cd419953 | 10-14 | v4.35-rc4 | module system | **setup failure**, same cause, plus Lean drift (removed core module, `ListN` changes) |

Outcome classes seen: proof repair (bot repairs), escalation of trusted-model changes (statement-broken intrinsic models, external models out of sync with the generated templates), structural repair (module-system migration), and extraction failure (stop and report the tool errors). Small steps give repair cases; large steps mostly escalation.

## Next steps

1. Bisect between 6e167c9b and 12a018bb (about 35 nightlies, Charon pins change often) for more mid-sized proof-repair cases, and find where the intrinsic-model statements start to break.
2. A "migrated" variant of the module-system steps: convert the hand-written `*External.lean` files to modules first, to see the proof breaks behind the setup failure.
3. Describe generated held-out cases in DESIGN.md §8: frozen once generated, never used for tuning; plus a time-split holdout on dalek/SPQR.
4. Run the repair loop (SCI-1715) on `gen-aeneas-6e167c9b`: a small, clean held-out repair case.
