"""The global scan state (data/control/state.json) as template context.

Shared by the accounts page (which can also stop a run) and the dashboard's
run bar. Cross-process: reflects runs started by the daemon or the CLI too.
"""

from __future__ import annotations

from src import control


def run_state_ctx() -> dict:
    state = control.read_state()
    if not state:
        return {"status": "idle", "msg": "", "progress": {}, "trigger": None}
    return {
        "status": state.get("status", "idle"),
        "msg": state.get("error") or "",
        "progress": state.get("progress") or {},
        "trigger": state.get("trigger"),
        "pid": state.get("pid"),
        "holder_alive": bool(state.get("holder_alive")),
    }
