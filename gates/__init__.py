"""The gate (DESIGN.md §4): the trusted core of self-healing-fv.

Deterministic and model-free. It imports nothing from the agent, pipeline or report code
(§4.8), and is reached only through its command-line interface (`gates/cli.py`, §4.5).
"""
