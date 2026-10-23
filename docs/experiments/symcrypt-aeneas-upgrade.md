# Experiment: forward Aeneas upgrade of SymCrypt (held-out cases)

Status: in progress (2026-10-23). Linear: SCI-1714 (data), SCI-1715 (evaluation).

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

## Next steps

1. Generalize `replay/replay_case.py` to generated cases (a base tree and an upgraded tree instead of two commits) and run its round-by-round unblocking on this case to list every break by layer.
2. Try a smaller Aeneas step (first release after d71d2e3f that stays on Lean v4.31.0) for a mostly-proof-repair case.
3. Describe generated held-out cases in DESIGN.md §8 (evaluation): frozen once generated, never used for tuning.
