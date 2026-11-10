"""Mission Control — observer-hook telemetry collector.

Read-only observer hooks write into a process-wide store anchored in
sys.modules so this file and dashboard/plugin_api.py can never see two
copies, regardless of which one the loader imports first. Every callback
is fail-open: it never raises into the agent loop.
"""

import re
import sys
import threading
import time
from collections import deque

_STATE_KEY = "_hermes_mission_control_state"
_PLUGIN_ID = "mission-control"
_TURNS_MAX = 100
_SUBAGENTS_MAX = 50

# Token buckets as Hermes's CanonicalUsage reports them. "in" excludes cache
# reads/writes; "prompt" is the full prompt size (in + cache_read + cache_write).
_BUCKETS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "tokens_prompt")

_DENY_CHOICES = frozenset({"deny", "smart_deny"})
_UNANSWERED_CHOICES = frozenset({"timeout", "cancelled", "notify_failed"})


def _today_str():
    return time.strftime("%Y-%m-%d", time.localtime())


def _fresh_daily():
    d = {"date": _today_str(), "turns": 0, "tool_calls": 0, "errors": 0,
         "api_retries": 0, "approvals": 0, "approvals_denied": 0,
         "approvals_unanswered": 0, "est_cost": 0.0, "unpriced_calls": 0}
    d.update({k: 0 for k in _BUCKETS})
    return d


def _init_state():
    return {
        "lock": threading.Lock(),
        "events": deque(maxlen=500),
        "turns": {},
        "subagents": {},
        "inflight": {},        # tool_call_id -> {tool, turn_id, started_at}
        "child_sessions": {},  # subagent child_session_id -> parent turn_id
        "turn_parent": {},     # subagent turn_id -> parent turn_id
        "daily": _fresh_daily(),
        "seq": 0,
    }


# THE anchor: one object, one address, for every loader path and load order.
STATE = sys.modules.setdefault(_STATE_KEY, _init_state())
# A store left by an older copy of this file (plugin reload in a live gateway)
# gains any keys added since, instead of failing silently in fail-open hooks.
for _k, _v in _init_state().items():
    STATE.setdefault(_k, _v)
del _k, _v

_LINKS_MAX = 500


def _roll_daily_locked():
    today = _today_str()
    if STATE["daily"]["date"] != today:
        STATE["daily"] = _fresh_daily()


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _link_locked(table, key, value):
    if not key:
        return
    m = STATE[table]
    m.pop(key, None)
    m[key] = value
    while len(m) > _LINKS_MAX:
        m.pop(next(iter(m)))


def _root_locked(turn_id):
    """The top-level turn a (possibly nested) subagent turn belongs to."""
    for _ in range(8):
        parent = STATE["turn_parent"].get(turn_id)
        if not parent:
            break
        turn_id = parent
    return turn_id


def _remember_turn(turn_id, started_at=None, parent=None):
    """Insert a new turn aggregate; bump the daily turn counter exactly once
    per top-level turn_id (subagent turns roll up into their parent instead);
    evict oldest past _TURNS_MAX. A None/empty turn_id (provider calls outside
    a turn) gets a synthetic key so the counter and bounds still hold."""
    if not turn_id:
        turn_id = "turn_%d" % int(time.time() * 1000)
    with STATE["lock"]:
        if turn_id in STATE["turns"]:
            return turn_id, STATE["turns"][turn_id]
        _roll_daily_locked()
        if parent:
            _link_locked("turn_parent", turn_id, parent)
        else:
            STATE["daily"]["turns"] += 1
        while len(STATE["turns"]) >= _TURNS_MAX:
            STATE["turns"].pop(next(iter(STATE["turns"])))
        now = time.time()
        t = {"started_at": started_at or now, "ended_at": now, "tool_calls": 0,
             "api_calls": 0, "est_cost": 0.0, "priced": None, "model": None,
             "provider": None, "session_id": None, "last_api_duration": None,
             "subagent": bool(parent), "parent": parent}
        t.update({k: 0 for k in _BUCKETS})
        STATE["turns"][turn_id] = t
        return turn_id, t


def _record(event):
    with STATE["lock"]:
        _roll_daily_locked()
        STATE["seq"] += 1
        event["seq"] = STATE["seq"]
        event["ts"] = time.time()
        STATE["events"].append(event)
    _push(event)


def _push(event):
    """Push the event to connected Desktop windows the instant it happens, via
    Hermes's public plugin event bridge (wire name plugin.mission-control.telemetry).
    Only when this process hosts the gateway's transports; elsewhere (hermes
    chat, cron, validate) there is no Desktop to reach and the UI's /events
    poll is the feed."""
    if "tui_gateway.server" not in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(_PLUGIN_ID, "telemetry", dict(event))
    except Exception:
        pass


def _cid(kw):
    """Correlation id for a tool call: tool_call_id, with a deterministic
    fallback when a provider omits it (Hermes passes "" when missing)."""
    return kw.get("tool_call_id") or "%s:%s" % (kw.get("turn_id"), kw.get("tool_name"))


def _usage_buckets(usage):
    """Hermes passes usage as a plain dict of CanonicalUsage fields; raw
    provider spellings are accepted as fallbacks for older builds."""
    u = usage if isinstance(usage, dict) else {}

    def n(*keys):
        for k in keys:
            v = u.get(k)
            if v:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    b = {"tokens_in": n("input_tokens", "prompt_tokens"),
         "tokens_out": n("output_tokens", "completion_tokens"),
         "cache_read": n("cache_read_tokens", "cache_read_input_tokens"),
         "cache_write": n("cache_write_tokens", "cache_creation_input_tokens")}
    b["tokens_prompt"] = b["tokens_in"] + b["cache_read"] + b["cache_write"]
    return b


# ---- observer callbacks (**kwargs; forward-compatible; never raise) ----

def on_pre_tool_call(**kw):
    try:
        cid = _cid(kw)
        with STATE["lock"]:
            STATE["inflight"][cid] = {"tool": kw.get("tool_name"),
                                      "turn_id": kw.get("turn_id"),
                                      "started_at": time.time()}
            root = _root_locked(kw.get("turn_id"))
        _record({"kind": "tool.start", "id": cid, "tool": kw.get("tool_name"),
                 "turn_id": kw.get("turn_id"), "root_turn_id": root,
                 "session_id": kw.get("session_id")})
    except Exception:
        pass


def on_post_tool_call(**kw):
    try:
        status = kw.get("status") or "ok"
        cid = _cid(kw)
        turn_id = kw.get("turn_id")
        with STATE["lock"]:
            STATE["inflight"].pop(cid, None)
            root = _root_locked(turn_id)
            for tid in {turn_id, root}:
                t = STATE["turns"].get(tid)
                if t is not None:
                    t["tool_calls"] += 1
            _roll_daily_locked()
            STATE["daily"]["tool_calls"] += 1
            if status == "error":
                STATE["daily"]["errors"] += 1
        _record({"kind": "tool.end", "id": cid, "tool": kw.get("tool_name"),
                 "status": status, "duration_ms": kw.get("duration_ms"),
                 "error_type": kw.get("error_type"), "turn_id": turn_id,
                 "root_turn_id": root, "session_id": kw.get("session_id")})
    except Exception:
        pass


_TOTALS = _BUCKETS + ("est_cost", "priced", "api_calls", "tool_calls", "started_at", "ended_at")


def on_post_api_request(**kw):
    try:
        b = _usage_buckets(kw.get("usage"))
        model = kw.get("model") or kw.get("response_model")
        has_tokens = any(b[k] for k in _BUCKETS)
        cost, priced = _cost(model, b) if has_tokens else (0.0, None)
        sid = kw.get("session_id")
        with STATE["lock"]:
            parent = STATE["child_sessions"].get(sid) if sid else None
        # Subagent calls (their session is a known child session) roll up into
        # the parent turn; only top-level turns bump the daily turn count.
        turn_id, t = _remember_turn(kw.get("turn_id"), _num(kw.get("started_at")), parent)
        ended = _num(kw.get("ended_at")) or time.time()
        with STATE["lock"]:
            _roll_daily_locked()
            daily = STATE["daily"]
            root_id = _root_locked(turn_id)
            root = STATE["turns"].get(root_id) if root_id != turn_id else None
            for target in (t, root):
                if target is None:
                    continue
                for k in _BUCKETS:
                    target[k] += b[k]
                target["api_calls"] += 1
                target["est_cost"] += cost
                if priced is not None:
                    target["priced"] = priced if target["priced"] is None else (target["priced"] and priced)
                target["ended_at"] = max(target.get("ended_at") or 0, ended)
            for k in _BUCKETS:
                daily[k] += b[k]
            t["model"] = model or t["model"]
            t["provider"] = kw.get("provider") or t["provider"]
            t["session_id"] = sid or t["session_id"]
            t["last_api_duration"] = kw.get("api_duration")
            daily["est_cost"] += cost
            if priced is False:
                daily["unpriced_calls"] += 1
            top = root if root is not None else t
            totals = {k: top.get(k) for k in _TOTALS}
        _record({"kind": "api.done", "turn_id": root_id if root is not None else turn_id,
                 "child_turn_id": turn_id if root is not None else None,
                 "model": model if root is None else top.get("model"),
                 "provider": kw.get("provider") if root is None else top.get("provider"),
                 "api_duration": kw.get("api_duration"), "cost": cost, "priced": priced,
                 "usage": b, "turn": totals})
    except Exception:
        pass


def on_api_request_error(**kw):
    try:
        err = kw.get("error") if isinstance(kw.get("error"), dict) else {}
        retryable = bool(kw.get("retryable"))
        with STATE["lock"]:
            _roll_daily_locked()
            STATE["daily"]["api_retries" if retryable else "errors"] += 1
        _record({"kind": "api.error", "model": kw.get("model"),
                 "status_code": kw.get("status_code"), "error_type": err.get("type"),
                 "retryable": retryable, "reason": kw.get("reason"),
                 "turn_id": kw.get("turn_id")})
    except Exception:
        pass


def on_subagent_start(**kw):
    try:
        # subagent_stop carries child_session_id but not child_subagent_id,
        # so the session id is the key that pairs start with stop.
        key = (kw.get("child_session_id") or kw.get("child_subagent_id")
               or "sub_%d" % int(time.time() * 1000))
        with STATE["lock"]:
            _link_locked("child_sessions", kw.get("child_session_id"), kw.get("parent_turn_id"))
            while len(STATE["subagents"]) >= _SUBAGENTS_MAX:
                STATE["subagents"].pop(next(iter(STATE["subagents"])))
            STATE["subagents"][key] = {
                "id": key, "subagent_id": kw.get("child_subagent_id"),
                "role": kw.get("child_role"), "goal": str(kw.get("child_goal") or "")[:120],
                "started_at": time.time(), "parent": kw.get("parent_turn_id"),
                "status": "running"}
        _record({"kind": "subagent.start", "id": key, "role": kw.get("child_role"),
                 "goal": str(kw.get("child_goal") or "")[:120],
                 "turn_id": kw.get("parent_turn_id")})
    except Exception:
        pass


def _find_subagent_locked(kw):
    s = STATE["subagents"].get(kw.get("child_session_id"))
    if s is not None:
        return s
    # Fallback: newest still-running child of the same parent turn and role.
    for s in reversed(list(STATE["subagents"].values())):
        if (s["status"] == "running" and s["parent"] == kw.get("parent_turn_id")
                and s["role"] == kw.get("child_role")):
            return s
    return None


def on_subagent_stop(**kw):
    try:
        status = kw.get("child_status") or "completed"
        with STATE["lock"]:
            s = _find_subagent_locked(kw)
            if s is not None:
                s["status"] = status
                s["ended_at"] = time.time()
                s["duration_ms"] = kw.get("duration_ms")
                if kw.get("child_summary") is not None:
                    s["summary"] = str(kw.get("child_summary"))[:200]
            sid = s["id"] if s is not None else kw.get("child_session_id")
            summary = s.get("summary") if s is not None else None
        _record({"kind": "subagent.end", "id": sid, "status": status,
                 "duration_ms": kw.get("duration_ms"), "summary": summary,
                 "turn_id": kw.get("parent_turn_id")})
    except Exception:
        pass


def on_pre_approval_request(**kw):
    try:
        _record({"kind": "approval.ask", "turn_id": kw.get("turn_id"),
                 "surface": kw.get("surface")})
    except Exception:
        pass


def on_post_approval_response(**kw):
    try:
        choice = kw.get("choice")
        with STATE["lock"]:
            _roll_daily_locked()
            STATE["daily"]["approvals"] += 1
            if choice in _DENY_CHOICES:
                STATE["daily"]["approvals_denied"] += 1
            elif choice in _UNANSWERED_CHOICES:
                STATE["daily"]["approvals_unanswered"] += 1
        _record({"kind": "approval.answer", "choice": choice, "turn_id": kw.get("turn_id")})
    except Exception:
        pass


# ---- pricing ----

_VENDOR_DOT = re.compile(r"^(?:(?:us|eu|apac|global)\.)?(?:anthropic|amazon|meta|mistral|cohere)\.")


def _norm_model(name):
    """Normalise provider spellings to one key: drop vendor/ and Bedrock
    prefixes, version/date suffixes, and treat '.' and '_' as '-'."""
    s = str(name or "").strip().lower().rsplit("/", 1)[-1].split(":", 1)[0]
    s = _VENDOR_DOT.sub("", s)
    s = re.sub(r"-v\d+$", "", s)
    s = re.sub(r"[-@]\d{8}$", "", s)
    s = re.sub(r"-latest$", "", s)
    return s.replace(".", "-").replace("_", "-")


def _price_row(model):
    m = _norm_model(model)
    if not m:
        return None
    row = _PRICES.get(m)
    if row is not None:
        return row
    for k in _PRICE_KEYS:          # longest first, so gpt-4o-mini beats gpt-4o
        if m.startswith(k + "-"):
            return _PRICES[k]
    return None


def _cost(model, b):
    """(est. USD, priced?) for one API call. Missing cache rates default to
    0.10x input (read) and 1.25x input (write)."""
    row = _price_row(model)
    if row is None:
        return 0.0, False
    i, o, cr, cw = row
    cr = i * 0.10 if cr is None else cr
    cw = i * 1.25 if cw is None else cw
    return (b["tokens_in"] * i + b["tokens_out"] * o
            + b["cache_read"] * cr + b["cache_write"] * cw) / 1e6, True


_OPUS = (5.00, 25.00, 0.50, 6.25)
_SONNET = (3.00, 15.00, 0.30, 3.75)

# (input, output, cache_read, cache_write) USD per 1M tokens; None = default
# ratio. Rows mirror Hermes's cited official-docs snapshot
# (agent/usage_pricing.py, Hermes 0.22.5) plus community rows marked (c).
# Labelled "est." in the UI; context-tier surcharges are not modelled.
_PRICE_TABLE = {
    # Anthropic — platform.claude.com/docs/en/about-claude/pricing
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": _OPUS,
    "claude-opus-4-8-fast": (10.00, 50.00, 1.00, 12.50),
    "claude-opus-4-8": _OPUS, "claude-opus-4-7": _OPUS,
    "claude-opus-4-6": _OPUS, "claude-opus-4-5": _OPUS,
    "claude-opus-4": (15.00, 75.00, 1.50, 18.75),
    "claude-3-opus": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),      # intro rate
    "claude-sonnet-4-6": _SONNET, "claude-sonnet-4-5": _SONNET,
    "claude-sonnet-4": _SONNET, "claude-3-7-sonnet": _SONNET, "claude-3-5-sonnet": _SONNET,
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "claude-3-5-haiku": (0.80, 4.00, 0.08, 1.00),
    "claude-3-haiku": (0.25, 1.25, 0.03, 0.30),
    # OpenAI — openai.com/api/pricing
    "gpt-6-astra": (10.00, 50.00, 1.00, 12.50),
    "gpt-6-1-sol": (2.00, 10.00, 0.10, 2.50),
    "gpt-6-sol": (2.00, 10.00, 0.20, 2.50),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.125),
    "gpt-5-6-sol": (5.00, 30.00, 0.50, 6.25),
    "gpt-5-6-terra": (2.50, 15.00, 0.25, 3.125),
    "gpt-5-6-luna": (1.00, 6.00, 0.10, 1.25),
    "gpt-5-2": (1.75, 14.00, None, None),               # (c)
    "gpt-5-1": (1.25, 10.00, None, None),               # (c)
    "gpt-5": (1.25, 10.00, None, None),                 # (c)
    "gpt-5-mini": (0.25, 2.00, None, None),             # (c)
    "gpt-5-nano": (0.05, 0.40, None, None),             # (c)
    "gpt-4o": (2.50, 10.00, 1.25, None),
    "gpt-4o-mini": (0.15, 0.60, 0.075, None),
    "gpt-4-1": (2.00, 8.00, 0.50, None),
    "gpt-4-1-mini": (0.40, 1.60, 0.10, None),
    "gpt-4-1-nano": (0.10, 0.40, 0.025, None),
    "o3": (10.00, 40.00, 2.50, None),
    "o3-mini": (1.10, 4.40, 0.55, None),
    "o4-mini": (1.10, 4.40, None, None),                # (c)
    "gpt-oss-120b": (0.15, 0.60, 0.015, None),
    "gpt-oss-20b": (0.07, 0.30, 0.035, None),
    # Google — ai.google.dev/gemini-api/docs/pricing
    "gemini-4-argon": (2.00, 10.00, 0.10, None),        # intro rate
    "gemini-3-8-flash": (0.75, 3.75, 0.075, None),
    "gemini-3-7-flash": (0.75, 3.75, 0.075, None),
    "gemini-3-6-flash": (1.50, 7.50, 0.15, None),
    "gemini-3-5-flash": (1.50, 9.00, 0.15, None),
    "gemini-3-5-flash-lite": (0.30, 2.50, 0.03, None),
    "gemini-3-1-pro": (2.00, 12.00, 0.20, None),
    "gemini-3-1-flash-lite": (0.25, 1.50, 0.025, None),
    "gemini-3-pro": (2.00, 12.00, 0.20, None),
    "gemini-3-flash": (0.50, 3.00, 0.05, None),
    "gemini-2-5-pro": (1.25, 10.00, 0.125, None),
    "gemini-2-5-flash": (0.15, 0.60, 0.015, None),
    "gemini-2-0-flash": (0.10, 0.40, 0.01, None),
    # DeepSeek — api-docs.deepseek.com/quick_start/pricing (off-peak)
    "deepseek-v4-pro": (0.66, 1.98, 0.022, None),
    "deepseek-v4-flash": (0.15, 0.60, 0.003, None),
    "deepseek-flash": (0.15, 0.60, 0.003, None),
    "deepseek-chat": (0.15, 0.60, 0.003, None),
    "deepseek-reasoner": (0.15, 0.60, 0.003, None),
    # Amazon / MiniMax / Moonshot / Zhipu / Qwen
    "nova-pro": (0.80, 3.20, None, None),
    "nova-lite": (0.06, 0.24, None, None),
    "nova-micro": (0.035, 0.14, None, None),
    "minimax-m3": (0.30, 1.20, 0.06, None),
    "minimax-m2-7": (0.30, 1.20, 0.06, None),
    "kimi-k2p7-code": (0.95, 4.00, 0.19, None),
    "kimi-k2p6": (0.95, 4.00, 0.16, None),
    "kimi-k2": (0.60, 2.50, None, None),                # (c)
    "glm-5p2": (1.40, 4.40, 0.14, None),
    "glm-5p1": (1.40, 4.40, 0.26, None),
    "glm-4-6": (0.60, 2.20, None, None),                # (c)
    "qwen3p7-plus": (0.40, 1.60, 0.08, None),
    "qwen3-coder": (0.50, 2.00, None, None),            # (c)
    # Mistral / xAI / Meta (c)
    "mistral-large-3": (2.00, 6.00, None, None),
    "mistral-medium-3": (0.40, 2.00, None, None),
    "devstral-medium": (0.40, 2.00, None, None),
    "codestral": (0.30, 0.90, None, None),
    "grok-4": (3.00, 15.00, None, None),
    "grok-3-mini": (0.30, 0.50, None, None),
    "llama-4-maverick": (0.19, 0.59, None, None),
    "llama-4-scout": (0.11, 0.34, None, None),
    "llama-3-3-70b": (0.59, 0.79, None, None),
}

_PRICES = {_norm_model(k): v for k, v in _PRICE_TABLE.items()}
_PRICE_KEYS = sorted(_PRICES, key=len, reverse=True)


def register(ctx):
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("subagent_stop", on_subagent_stop)
    ctx.register_hook("pre_approval_request", on_pre_approval_request)
    ctx.register_hook("post_approval_response", on_post_approval_response)
    # Exactly the 8 hooks declared in provides_hooks — catalog rule 6 parity.
