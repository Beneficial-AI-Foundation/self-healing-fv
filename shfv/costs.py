"""Cost from token counts, the same way for every agent and model (DESIGN.md §9.5).

Adapters report tokens; the pipeline prices them with the per-model table in the target config
(`[prices."<model>"]`, USD per million tokens). The agent harness's own cost figure, when it has
one, is kept only as a cross-check. A model with no price entry has cost `unknown`, and the
dollar cap is not enforced for it: never reported as $0.

TODO(§9.5, SCI-1717): this is accounting, not enforcement. A hard cap needs the metering proxy.
"""
from __future__ import annotations

FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def normalize(usage: dict) -> dict:
    """Anthropic-style usage → our token fields. Cache writes with no 5m/1h split are counted
    as 1h writes, the more expensive kind, so the estimate errs high."""
    cc = usage.get("cache_creation") or {}
    writes = usage.get("cache_creation_input_tokens", 0) or 0
    w5 = cc.get("ephemeral_5m_input_tokens", 0) if cc else 0
    w1 = cc.get("ephemeral_1h_input_tokens", 0) if cc else writes
    return {"input": usage.get("input_tokens", 0) or 0, "output": usage.get("output_tokens", 0) or 0,
            "cache_read": usage.get("cache_read_input_tokens", 0) or 0, "cache_write_5m": w5 or 0,
            "cache_write_1h": w1 or 0}


def price(tokens: dict, prices: dict | None) -> float | None:
    """USD for these tokens at these prices, or None if the model has no complete price entry."""
    if not prices or any(k not in prices for k in FIELDS):
        return None
    return round(sum(tokens.get(k, 0) * prices[k] for k in FIELDS) / 1e6, 8)


def status(prices: dict | None, complete: bool) -> str:
    """The cost status of priced tokens: `partial` when usage is incomplete (a lower bound),
    `upper-bound` when the price entry says its rates bound the true price from above (`upper_bound
    = true`: e.g. a model priced per request by prompt length, at its long-prompt rates)."""
    if not complete:
        return "partial"
    return "upper-bound" if prices and prices.get("upper_bound") else "computed"


def describe(status: str, usd: float | None, model: str | None = None) -> str:
    """Human wording for the report."""
    if status == "computed" and usd is not None:
        return f"${usd:.2f} (API-equivalent, from token counts at list prices)"
    if status == "upper-bound" and usd is not None:
        return f"at most ${usd:.2f} (API-equivalent upper bound, from token counts at the model's highest list prices)"
    if status == "partial" and usd is not None:
        return f"at least ${usd:.2f} (a session was stopped before reporting full usage)"
    if status == "no-price":
        return f"unknown: no prices configured for `{model}`, so the dollar cap is not enforced"
    return "unknown"
