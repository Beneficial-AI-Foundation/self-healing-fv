"""Cost is computed from tokens with the target config's price table, for any adapter."""
from shfv import costs

PRICES = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write_5m": 5.0, "cache_write_1h": 8.0}


def test_reproduces_a_real_claude_code_cost():
    # Opus 5.5 usage and Claude Code's own total_cost_usd from the 2026-10-23 PR 782 run.
    usage = {"input_tokens": 18, "output_tokens": 2881, "cache_read_input_tokens": 196637,
             "cache_creation_input_tokens": 31384,
             "cache_creation": {"ephemeral_1h_input_tokens": 31384, "ephemeral_5m_input_tokens": 0}}
    assert abs(costs.price(costs.normalize(usage), PRICES) - 0.3480914) < 1e-7


def test_cache_writes_without_a_split_are_priced_as_1h():
    t = costs.normalize({"cache_creation_input_tokens": 1000})
    assert t["cache_write_1h"] == 1000 and t["cache_write_5m"] == 0


def test_an_upper_bound_price_entry_says_at_most():
    import tomllib
    from pathlib import Path
    from shfv.agents.base import Attempt, Budget
    from shfv.pipeline import price_attempt
    for target in ("dalek-lean-verify", "spqr", "symcrypt"):
        cfg = tomllib.loads((Path(__file__).parents[1] / "targets" / f"{target}.toml").read_text())
        sol = cfg["prices"]["gpt-6.1-sol"]
        assert sol["upper_bound"] and all(k in sol for k in costs.FIELDS)
    att = Attempt("codex", "gpt-6.1-sol", "T", tokens={"input": 1_000_000, "output": 0, "cache_read": 0,
                                                       "cache_write_5m": 0, "cache_write_1h": 0}, tokens_complete=True)
    price_attempt(att, sol, Budget(5.0, 1, 1))
    assert att.cost_status == "upper-bound" and att.cost_usd == 4.0  # the >272K input rate
    assert costs.describe(att.cost_status, att.cost_usd).startswith("at most $4.00")
    assert costs.status(sol, complete=False) == "partial"  # incomplete usage stays a lower bound
    from types import SimpleNamespace
    from shfv.pipeline import Run
    a = {"cost_usd": 1.0, "cost_status": "upper-bound", "turns": 1, "seconds": 1}
    rep = {"repairs": [{"attempts": [a, {**a, "cost_status": "computed"}]}]}
    t = Run.totals(SimpleNamespace(timing={}), rep, 1)
    assert t["cost_status"] == "upper-bound" and t["cost_usd"] == 2.0


def test_incomplete_or_missing_prices_give_unknown_not_zero():
    t = costs.normalize({"input_tokens": 10})
    assert costs.price(t, None) is None
    assert costs.price(t, {"input": 1.0}) is None
    assert "unknown" in costs.describe("no-price", None, "m")
