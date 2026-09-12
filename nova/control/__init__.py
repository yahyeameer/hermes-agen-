"""The Platform Control API — the only path from a customer surface to the runtime.

The dashboard talks to this and to nothing else. It never reaches a runtime endpoint,
never learns a runtime path, and never sees a runtime's own vocabulary. That boundary is
the reason this package exists; see ``docs/platform/ARCHITECTURE_BOUNDARIES.md`` §5.

Phase 1 is **read-only**. There is no write path to get wrong, and the read models are
what every later feature depends on.
"""

from nova.control.api import ControlAPI, Response
from nova.control.server import serve

__all__ = ["ControlAPI", "Response", "serve"]
