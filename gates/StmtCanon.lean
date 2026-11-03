/- Vendored from auto_verify_dalek (https://github.com/Beneficial-AI-Foundation/auto_verify_dalek),
   file harness/gates/StmtCanon.lean at commit 32ed856f4589a1cdfcdad6406849096b331e2711.
   Original author: Liao (GitHub Zhang-Liao). Licensed under the MIT License:

   Copyright (c) 2025 curve25519-dalek-verify contributors

   Permission is hereby granted, free of charge, to any person obtaining a copy of this software
   and associated documentation files (the "Software"), to deal in the Software without
   restriction, including without limitation the rights to use, copy, modify, merge, publish,
   distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the
   Software is furnished to do so, subject to the following conditions:

   The above copyright notice and this permission notice shall be included in all copies or
   substantial portions of the Software.

   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
   BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
   NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
   DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

   Changes for self-healing-fv (DESIGN.md §4.5), relative to upstream:
   - Inverted territory: nothing is δ-unfolded. Every non-extracted declaration is frozen, so a
     statement is fingerprinted as written, and extracted constants are named, not unfolded.
   - Definition bodies are fingerprinted too (`body_canon`), together with the bodies of their
     auxiliary definitions (`foo.match_1`, `foo._unary`, ...), so an edited spec predicate under
     an unchanged theorem is a change.
   - Axiom closure, sorry sources and project-local uses come from one memoized walk (Tarjan's
     SCC algorithm, iterative) shared by all declarations. Upstream walked the closure afresh per
     declaration, which took 8 minutes for the ~1350 declarations of one dalek commit.
   - Sorry sources and uses are reported as user-facing names (`foo._unary` → `foo`).
   - No hard-coded import root: `--import` and `--module` come from the target config.
   - Each record carries the source line of the declaration's name (`line`), so the gate names a
     declaration exactly from where it is written (2026-11-03). Fingerprints are unchanged.
   - The single-declaration mode and the `consts` field were dropped (unused here). -/
/- StmtCanon: per-declaration statement fingerprints and trust facts for the gate.

Usage (needs the package's LEAN_PATH, so always via `lake env`):

  lake env lean --run gates/StmtCanon.lean --import M1,M2,.. --module M1,M2,.. [--reference-closures]

`--import` lists the modules to load (those that built). `--module` lists the modules whose
user-facing declarations are audited (auxiliaries such as `proof_N`, `match_N`, `eq_N` and
instance helpers are dropped; `_private` names are kept). Emits one JSON line per declaration:

  { "name", "kind", "module",
    "written":    whether the declaration has a source range (false: added by a tactic),
    "line":       the line of its name in its module's source (null if none); not fingerprinted,
    "canon":     canonical α-invariant dump of the elaborated type (authoritative),
    "body_canon": definitions only: canonical dump of the value and of its auxiliary definitions,
    "pp", "pp_body": pretty-printed type / value (display only),
    "axioms":     axiom closure,
    "sorry_sources": user-facing declarations in the closure that mention `sorryAx` directly,
    "uses":       user-facing declarations of the audited modules in the closure (self excluded),
    "error":      present only on failure }

The canon dump ignores binder names (de Bruijn indices make it α-invariant) and mdata, so
pretty-print-level games cannot make two different propositions collide.
-/
import Lean
import Std.Data.HashMap
import Std.Data.HashSet
open Lean Meta

def moduleOf (env : Environment) (n : Name) : String :=
  match env.getModuleIdxFor? n with
  | some idx => toString env.header.moduleNames[idx.toNat]!
  | none => "<local>"

/-- Canonical dump: α-invariant (binder names dropped), mdata-transparent. -/
partial def canonDump : Expr → String
  | .bvar i        => s!"(b{i})"
  | .fvar id       => s!"(fv {id.name})"
  | .mvar id       => s!"(mv {id.name})"
  | .sort u        => s!"(s {u})"
  | .const n us    => s!"(c {n} {us})"
  | .app f a       => s!"(a {canonDump f} {canonDump a})"
  | .lam _ t b _   => s!"(l {canonDump t} {canonDump b})"
  | .forallE _ t b _ => s!"(p {canonDump t} {canonDump b})"
  | .letE _ t v b _  => s!"(z {canonDump t} {canonDump v} {canonDump b})"
  | .lit (.natVal v) => s!"(ln {v})"
  | .lit (.strVal v) => s!"(ls {v})"
  | .mdata _ b     => canonDump b
  | .proj s i b    => s!"(j {s} {i} {canonDump b})"

def kindOf : ConstantInfo → String
  | .axiomInfo _  => "axiom"
  | .defnInfo _   => "definition"
  | .thmInfo _    => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _   => "quot"
  | .inductInfo _ => "inductive"
  | .ctorInfo _   => "constructor"
  | .recInfo _    => "recursor"

/-- `pre` followed by digits, optionally in several groups: `match_1`, `match_1_1`. -/
def isNumbered (s pre : String) : Bool :=
  s.startsWith pre && s.length > pre.length &&
    ((s.drop pre.length).all fun ch => ch.isDigit || ch == '_') &&
    ((s.drop pre.length).take 1).all Char.isDigit

/-- Auxiliary constants whose existence/statement legitimately changes when
    only a PROOF changes: `foo.proof_1`, `foo.match_1`, `foo.eq_1`,
    `foo._cstage`, structural-recursion helpers, and constants that tactics
    create on demand inside proofs (`foo.congr_simp`, `foo.induct`, ...).
    Private user declarations (`_private.M.0.foo`) are kept: their statements
    are part of the supplied file.
    Changed from upstream: patterns are exact (upstream's `startsWith "eq_"`,
    `"inj"`, `"sizeOf"` also matched user names such as `eq_of_lt` or
    `injective`, which then silently dropped out of the frozen set). Lean's
    own `isInternalDetail` is not used for the same reason: it treats any
    component starting with `_` as internal, which drops user theorems about
    Rust identifiers such as `__m128i` or `_mm_xor_si128` (SymCrypt). A
    component starting with `_` (Lean's `_unary`, `_proof_1`, `_cstage1`, ...)
    therefore marks a constant as auxiliary only if it has no declaration
    range: Lean records source ranges for declarations written in source, not
    for the auxiliaries it generates. Without an environment (`userRoot`),
    the name alone decides. -/
def isAuxiliary (n : Name) (env? : Option Environment := none) : Bool :=
  let user := (privateToUserName? n).getD n
  let written := match env? with
    | some env => (declRangeExt.find? env n).isSome
    | none => false
  user.isInaccessibleUserName ||
  user.components.any fun c =>
    let s := c.toString (escape := false)  -- `«_aux_…»` must compare as `_aux_…`
    c.isNum ||
    isNumbered s "proof_" || isNumbered s "match_" || isNumbered s "eq_" ||
    isNumbered s "hcongr_" ||  -- congruence lemmas `simp` adds on demand (`Array.make.hcongr_4`)
    s == "_native" ||  -- per-use axioms of `native_decide`/`bv_decide` (`T._native.native_decide.ax_1_2`,
                       -- newer Lean); they carry the source range of the proof that made them
    (s.startsWith "_" && (!written || s.startsWith "_aux")) ||  -- `_aux…`: notation/macro helpers
    ["eq_def", "eq_unfold", "below", "brecOn", "binductionOn", "noConfusion",
     "noConfusionType", "casesOn", "recOn", "rec", "ibelow", "sizeOf_spec", "inj",
     "injEq", "ctorIdx", "congr_simp", "induct", "mutual_induct", "induct_unfolding",
     "fun_cases", "splitter"].contains s

/-- The user-facing declaration an auxiliary constant belongs to (`foo._unary` → `foo`). -/
partial def userRoot (n : Name) : Name :=
  if !isAuxiliary n then n else
  match n with
  | .str p _ => if p.isAnonymous then n else userRoot p
  | .num p _ => if p.isAnonymous then n else userRoot p
  | .anonymous => n

/-- All non-auxiliary constants declared in `mod`, in declaration order. -/
def moduleDecls (env : Environment) (mod : Name) : Array Name :=
  match env.getModuleIdx? mod with
  | none => #[]
  | some idx =>
    (env.header.moduleData[idx.toNat]!.constNames.filter fun n => !isAuxiliary n env)

/-! ### Closure facts, memoized over the whole constant graph -/

structure Info where
  axioms  : Std.HashSet Name := {}
  sorries : Std.HashSet Name := {}  -- constants whose own type/value mentions `sorryAx`
  uses    : Std.HashSet Name := {}  -- constants of the audited modules

def unionSet (a b : Std.HashSet Name) : Std.HashSet Name :=
  if a.size < b.size then a.fold (init := b) fun s x => s.insert x
  else b.fold (init := a) fun s x => s.insert x

def Info.union (a b : Info) : Info :=
  { axioms := unionSet a.axioms b.axioms, sorries := unionSet a.sorries b.sorries,
    uses := unionSet a.uses b.uses }

/-- Expressions a constant depends on, as in Lean's `collectAxioms`. -/
def depExprs : ConstantInfo → Array Expr
  | .defnInfo v   => #[v.type, v.value]
  | .thmInfo v    => #[v.type, v.value]
  | .opaqueInfo v => #[v.type, v.value]
  | ci            => #[ci.type]

/-- Successors of `c` in the dependency graph, and the facts `c` contributes itself. -/
def direct (env : Environment) (audited : Std.HashSet Name) (c : Name) : Array Name × Info := Id.run do
  let some ci := env.find? c | return (#[], {})
  let mut succ : Array Name := match ci with
    | .inductInfo v => v.ctors.toArray
    | _ => #[]
  let mut carries := false
  for e in depExprs ci do
    for n in e.getUsedConstants do
      if n == ``sorryAx then carries := true
      succ := succ.push n
  let mod := match env.getModuleIdxFor? c with
    | some idx => env.header.moduleNames[idx.toNat]!
    | none => Name.anonymous
  let info : Info := {
    axioms := if ci matches .axiomInfo _ then ({} : Std.HashSet Name).insert c else {},
    sorries := if carries then ({} : Std.HashSet Name).insert c else {},
    uses := if audited.contains mod then ({} : Std.HashSet Name).insert c else {} }
  return (succ, info)

structure Tarjan where
  index   : Std.HashMap Name Nat := {}
  low     : Std.HashMap Name Nat := {}
  onStack : Std.HashSet Name := {}
  stack   : Array Name := #[]
  acc     : Std.HashMap Name Info := {}  -- partial facts of nodes in unfinished SCCs
  done    : Std.HashMap Name Info := {}  -- complete closure facts
  counter : Nat := 0

/-- Closure facts of `root`. Iterative Tarjan: every member of a strongly connected component
    gets the union over the component plus everything it reaches, so cached results are exact
    even with cycles (e.g. `unsafe` recursion), and deep chains cannot overflow the stack. -/
def closure (env : Environment) (audited : Std.HashSet Name) (root : Name) : StateM Tarjan Info := do
  if let some i := (← get).done[root]? then return i
  let visit (v : Name) : StateM Tarjan (Name × Array Name × Nat) := do
    let (succ, info) := direct env audited v
    modify fun s => { s with
      index := s.index.insert v s.counter, low := s.low.insert v s.counter,
      counter := s.counter + 1, stack := s.stack.push v, onStack := s.onStack.insert v,
      acc := s.acc.insert v info }
    return (v, succ, 0)
  let mut frames : Array (Name × Array Name × Nat) := #[← visit root]
  while frames.size > 0 do
    let (v, succ, pos) := frames.back!
    if pos < succ.size then
      frames := frames.set! (frames.size - 1) (v, succ, pos + 1)
      let w := succ[pos]!
      let st ← get
      if let some r := st.done[w]? then
        modify fun s => { s with acc := s.acc.insert v ((s.acc.getD v {}).union r) }
      else if st.index.contains w then
        if st.onStack.contains w then
          modify fun s => { s with low := s.low.insert v (min (s.low.getD v 0) (s.index.getD w 0)) }
      else
        frames := frames.push (← visit w)
    else
      frames := frames.pop
      let st ← get
      if st.low.getD v 0 == st.index.getD v 0 then
        -- v is the root of an SCC: pop it, and give every member the union.
        let mut members : Array Name := #[]
        let mut info : Info := {}
        let mut stack := st.stack
        let mut onStack := st.onStack
        let mut fin := false
        while !fin && stack.size > 0 do
          let w := stack.back!
          stack := stack.pop
          onStack := onStack.erase w
          members := members.push w
          info := info.union (st.acc.getD w {})
          fin := w == v
        let mut done := st.done
        let mut acc := st.acc
        for m in members do
          done := done.insert m info
          acc := acc.erase m
        set { st with stack, onStack, done, acc }
      if frames.size > 0 then
        let (p, _, _) := frames.back!
        let st ← get
        if let some r := st.done[v]? then
          modify fun s => { s with acc := s.acc.insert p ((s.acc.getD p {}).union r) }
        else
          modify fun s => { s with low := s.low.insert p (min (s.low.getD p 0) (st.low.getD v 0)) }
  return (← get).done.getD root {}

/-- Canonical dump of a definition's value, followed by the values of the auxiliary
    definitions of the audited modules it reaches (`foo.match_1` holds a match's patterns). -/
def bodyCanon (env : Environment) (audited : Std.HashSet Name) (c : Name) : Option String := Id.run do
  let some ci := env.find? c | return none
  let some v := (match ci with
    | .defnInfo v => some v.value | .opaqueInfo v => some v.value | _ => none) | return none
  let mut out := canonDump v
  let mut seen : Std.HashSet Name := ({} : Std.HashSet Name).insert c
  let mut todo : Array Name := v.getUsedConstants
  while todo.size > 0 do
    let n := todo.back!
    todo := todo.pop
    if seen.contains n then continue
    seen := seen.insert n
    if !isAuxiliary n then continue
    let inAudited := match env.getModuleIdxFor? n with
      | some idx => audited.contains env.header.moduleNames[idx.toNat]!
      | none => false
    if !inAudited then continue
    if let some (.defnInfo w) := env.find? n then
      out := out ++ s!" [aux {n} : {canonDump w.type} := {canonDump w.value}]"
      todo := todo ++ w.value.getUsedConstants
  return some out

def namesJson (s : Std.HashSet Name) : Json :=
  toJson ((s.toArray.map toString).qsort (· < ·))

/-- Reference walk, for testing `closure` only (`--reference-closures`): Lean's own
    `collectAxioms`, and a plain per-declaration DFS (upstream's algorithm) for sorry
    sources and uses. No memoization, so it is slow on a full project. -/
def naiveClosure (audited : Std.HashSet Name) (root : Name) : MetaM Info := do
  let env ← getEnv
  let axioms ← collectAxioms root
  let mut seen : Std.HashSet Name := {}
  let mut todo : Array Name := #[root]
  let mut info : Info := { axioms := axioms.foldl (init := {}) fun s a => s.insert a }
  while todo.size > 0 do
    let c := todo.back!
    todo := todo.pop
    if seen.contains c then continue
    seen := seen.insert c
    let (succ, d) := direct env audited c
    info := { info with sorries := unionSet info.sorries d.sorries, uses := unionSet info.uses d.uses }
    todo := todo ++ succ
  return info

def truncate (s : String) (n : Nat := 4000) : String :=
  if s.length ≤ n then s else (s.toList.take n).foldl String.push "" ++ " …"

def auditDecl (audited : Std.HashSet Name) (declName : Name) (reference := false) :
    StateRefT Tarjan MetaM Json := do
  let env ← getEnv
  match env.find? declName with
  | none => return Json.mkObj [("name", toString declName), ("found", false)]
  | some ci =>
    let info ← if reference then naiveClosure audited declName
      else modifyGet fun s => (closure env audited declName).run s
    let pp ← ppExpr ci.type
    let roots (s : Std.HashSet Name) : Std.HashSet Name :=
      s.fold (init := {}) fun acc n => acc.insert (userRoot n)
    let mut fields : List (String × Json) := [
      ("name", toString declName), ("found", true),
      ("kind", kindOf ci), ("module", moduleOf env declName),
      -- false for declarations a tactic adds while a proof elaborates (e.g. `bv_decide`'s
      -- `PUnit.enumToBitVec`): they have no source range, and vanish when that proof is stubbed
      ("written", Json.bool (declRangeExt.find? env declName).isSome),
      -- The line of the declaration's name in its module's source (self-healing-fv: the gate names a
      -- broken theorem by file and line, not by its short name; DESIGN.md §4.5). Not fingerprinted.
      ("line", match declRangeExt.find? env declName with
        | some r => toJson r.selectionRange.pos.line
        | none => Json.null),
      ("canon", canonDump ci.type), ("pp", truncate (toString pp)),
      ("axioms", namesJson info.axioms),
      ("sorry_sources", namesJson (roots info.sorries)),
      ("uses", namesJson ((roots info.uses).erase declName))]
    if let some b := bodyCanon env audited declName then
      let ppb ← match ci with
        | .defnInfo v => do pure (toString (← ppExpr v.value))
        | .opaqueInfo v => do pure (toString (← ppExpr v.value))
        | _ => pure ""
      fields := fields ++ [("body_canon", Json.str b), ("pp_body", Json.str (truncate ppb))]
    return Json.mkObj fields

def splitNames (s : String) : Array Name :=
  ((s.splitOn ",").filter (· ≠ "")).toArray.map String.toName

unsafe def main (args : List String) : IO Unit := do
  initSearchPath (← findSysroot)
  let mut imports : Array Name := #[]
  let mut modules : Array Name := #[]
  let mut rest := args
  let mut reference := false
  while true do
    match rest with
    | "--import" :: v :: r => imports := imports ++ splitNames v; rest := r
    | "--reference-closures" :: r => reference := true; rest := r
    | "--module" :: v :: r => modules := modules ++ splitNames v; rest := r
    | [] => break
    | a :: _ => throw <| IO.userError s!"unexpected argument {a}"
  if imports.isEmpty then throw <| IO.userError "--import is required"
  let env ← importModules (imports.map fun m => {module := m}) {} (trustLevel := 1024)
  let ctx : Core.Context := { fileName := "<StmtCanon>", fileMap := default,
                              maxHeartbeats := 0 }
  let st : Core.State := { env }
  let mut decls : Array Name := #[]
  for m in modules do
    if env.getModuleIdx? m |>.isNone then
      throw <| IO.userError s!"--module {m}: not in the imported environment"
    decls := decls ++ moduleDecls env m
  let audited : Std.HashSet Name := modules.foldl (init := {}) fun s m => s.insert m
  let act : CoreM Unit := do
    let tarjan ← IO.mkRef ({} : Tarjan)
    for d in decls do
      let j ← try
        let (j, s) ← MetaM.run' ((auditDecl audited d reference).run (← tarjan.get))
        tarjan.set s
        pure j
      catch ex =>
        pure <| Json.mkObj [("name", toString d), ("found", true),
                            ("error", toString (← ex.toMessageData.toString))]
      IO.println j.compress
  let _ ← act.toIO ctx st
  pure ()
