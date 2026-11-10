"""Mission Control — backend routes (mounted at /api/plugins/mission-control/).

Never bind the shared store at import time: lookup is dynamic per request,
so it does not matter whether the server imports this file before or after
the agent half (__init__.py). When the agent half was never imported in
this process, every route degrades to empty-but-valid responses, never 500.
"""

import sys
import time
from fastapi import APIRouter

_STATE_KEY = "_hermes_mission_control_state"
_INFLIGHT_MAX_AGE_S = 120
_SPARK_TURNS = 12


def _state():
    """Dynamic lookup of the sys.modules-anchored store created by
    __init__.py's setdefault, or an empty mapping when absent."""
    return sys.modules.get(_STATE_KEY) or {}


def _reap_inflight(s):
    """Drop in-flight tool rows older than _INFLIGHT_MAX_AGE_S so a cancelled
    turn never leaves a zombie pulsing forever."""
    cutoff = time.time() - _INFLIGHT_MAX_AGE_S
    stale = [k for k, v in s["inflight"].items() if v.get("started_at", 0) < cutoff]
    for k in stale:
        s["inflight"].pop(k, None)
    return stale


def _zero_daily(daily):
    for k, v in list(daily.items()):
        if k != "date":
            daily[k] = 0.0 if isinstance(v, float) else 0


router = APIRouter()


@router.get("/snapshot")
async def snapshot():
    s = _state()
    lock = s.get("lock")
    if not lock:
        return {"daily": None, "turns": [], "subagents": [], "inflight": [],
                "events": [], "spark": [], "max_seq": 0}
    with lock:
        stale = _reap_inflight(s)
        for cid in stale:  # close zombie rows visibly in the feed
            s["seq"] += 1
            s["events"].append({"kind": "tool.end", "id": cid, "tool": None,
                                "status": "cancelled", "duration_ms": None,
                                "seq": s["seq"], "ts": time.time()})
        # Newest first; insertion order breaks ties (Windows clocks tick ~15 ms).
        ranked = [({"turn_id": k, **v}, i) for i, (k, v) in enumerate(s["turns"].items())
                  if not v.get("subagent")]
        turns = [t for t, _ in sorted(ranked, key=lambda p: (p[0]["started_at"], p[1]), reverse=True)]
        # Per-turn series, oldest first, so a 90 s demo visibly moves it.
        spark = [{"turn_id": t["turn_id"], "tokens": t.get("tokens_prompt", 0) + t.get("tokens_out", 0),
                  "est_cost": t.get("est_cost", 0.0), "priced": t.get("priced")}
                 for t in reversed(turns[:_SPARK_TURNS])]
        subagents = sorted((dict(v) for v in s["subagents"].values()),
                           key=lambda a: a["started_at"])[-20:]
        return {
            "daily": dict(s["daily"]),
            "turns": turns[:20],
            "subagents": subagents,
            "inflight": [{"id": k, **v} for k, v in s["inflight"].items()],
            "events": [dict(e) for e in list(s["events"])[-50:]],
            "spark": spark,
            "max_seq": s["seq"],
            "now": time.time(),
        }


@router.get("/events")
async def events(since: int = 0):
    s = _state()
    lock = s.get("lock")
    if not lock:
        return {"events": [], "max_seq": 0, "reset": since > 0}
    with lock:
        # A cursor ahead of the server means the process restarted and seq
        # began again at 0: hand back everything and tell the UI to resync.
        reset = since > s["seq"]
        floor = 0 if reset else since
        return {"events": [dict(e) for e in s["events"] if e["seq"] > floor],
                "max_seq": s["seq"], "reset": reset, "now": time.time()}


@router.post("/reset")
async def reset():
    s = _state()
    lock = s.get("lock")
    if lock:
        with lock:
            s["events"].clear()
            s["turns"].clear()
            s["subagents"].clear()
            s["inflight"].clear()
            _zero_daily(s["daily"])
    return {"ok": True}
