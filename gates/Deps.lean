/- self-healing-fv: the imports of Lean source files, read by Lean's own header parser
   (`Lean.Elab.parseImports`, the same on Lean 4.21–4.35), not by a regex (DESIGN.md §4.5; gate sweep
   #9, 2026-11-03: an indented `import`, or `import all M`, left a built module unaudited).

     lake env lean --run gates/Deps.lean LIST

   LIST is a file with one source path per line. Prints one JSON line per file:
     {"file", "imports": [module names]}   or   {"file", "error"}   (a header that does not parse)
-/
import Lean
open Lean

unsafe def main (args : List String) : IO UInt32 := do
  let some list := args.head? | throw <| IO.userError "usage: Deps.lean LIST"
  for f in (← IO.FS.lines list) do
    if f.isEmpty then continue
    let j ← try
      let input ← IO.FS.readFile f
      let (imports, _, msgs) ← Elab.parseImports input (some f)
      if msgs.hasErrors then
        let errs ← msgs.toList.mapM (·.toString)
        pure <| Json.mkObj [("file", f), ("error", toJson errs)]
      else
        pure <| Json.mkObj [("file", f), ("imports", toJson (imports.map (toString ·.module)))]
    catch ex =>
      pure <| Json.mkObj [("file", f), ("error", toString ex)]
    IO.println j.compress
  return 0
