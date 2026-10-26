import Toy.Funs

/-- A spec predicate: frozen, including its body. -/
def IsEven (n : Nat) : Prop := n % 2 = 0

theorem double_spec (x : Nat) : double x = 2 * x := by
  unfold double
  omega

theorem double_even (x : Nat) : IsEven (double x) := by
  unfold IsEven
  rw [double_spec]
  omega

theorem triple_spec (x : Nat) : triple x = 3 * x := by
  unfold triple
  omega

theorem le_add (x y : Nat) : x ≤ x + y := by
  omega
