"""The gate (DESIGN.md §4): the trusted core of self-healing-fv.

Deterministic and model-free. It imports nothing from the agent, pipeline or report code
(§4.8), and is reached only through its command-line interface (`gates/cli.py`, §4.5).
"""

import hashlib as _hashlib
from pathlib import Path as _Path


def version() -> str:
    """Hash of the gate's own sources: recorded in every snapshot and verdict (§9.1)."""
    h = _hashlib.sha256()
    for p in sorted(_Path(__file__).parent.glob("*")):
        if p.suffix in (".py", ".lean"):
            h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:12]
