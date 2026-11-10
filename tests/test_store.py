"""Mission Control store tests: load-order parity, zombie reap, turn counting,
token/cost accounting, subagent pairing, approvals, events cursor, and hook
parity with plugin.yaml (catalog rule 6).

Payloads come from tests/fixtures.py, shaped like Hermes's real call sites.
Runnable with plain `python tests/test_store.py` or pytest. Stdlib only.
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as fx  # noqa: E402

_STATE_KEY = "_hermes_mission_control_state"
_EMPTY_SNAPSHOT = {"daily": None, "turns": [], "subagents": [], "inflight": [],
                   "events": [], "spark": [], "max_seq": 0}


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fresh(tag):
    """Drop the shared store, then load both halves under unique names."""
    sys.modules.pop(_STATE_KEY, None)
    agent = _load("mc_agent_" + tag, "__init__.py")
    api = _load("mc_api_" + tag, "dashboard/plugin_api.py")
    return agent, api


# ---- load order ----

def test_api_first_load_order_survives():
    """plugin_api.py imported BEFORE __init__.py: routes must see telemetry
    fired after the agent half loads (dynamic per-request lookup)."""
    sys.modules.pop(_STATE_KEY, None)
    api = _load("mc_api_first", "dashboard/plugin_api.py")
    assert _run(api.snapshot()) == _EMPTY_SNAPSHOT
    agent = _load("mc_agent_first", "__init__.py")
    agent.on_post_api_request(**fx.post_api_request(turn_id="t1", model="gpt-5",
                                                    provider="openai", inp=100, out=50))
    snap = _run(api.snapshot())
    assert snap["daily"]["turns"] == 1
    assert snap["daily"]["tokens_in"] == 100
    assert snap["daily"]["tokens_out"] == 50
    assert snap["turns"][0]["turn_id"] == "t1"
    assert snap["turns"][0]["model"] == "gpt-5"
    assert snap["turns"][0]["provider"] == "openai"


def test_init_first_load_order_survives():
    agent, api = _fresh("second")
    agent.on_post_api_request(**fx.post_api_request(turn_id="t2", inp=7, out=3))
    snap = _run(api.snapshot())
    assert snap["daily"]["turns"] == 1
    assert snap["daily"]["tokens_in"] == 7


# ---- turns ----

def test_turn_counter_once_per_turn_id():
    agent, _ = _fresh("turns")
    for _ in range(3):
        agent.on_post_api_request(**fx.post_api_request(turn_id="t9", inp=1))
    st = sys.modules[_STATE_KEY]
    assert st["daily"]["turns"] == 1
    assert st["turns"]["t9"]["api_calls"] == 3


def test_none_and_empty_turn_id_get_synthetic_key():
    agent, api = _fresh("none")
    agent.on_post_api_request(**fx.post_api_request(turn_id=None, inp=5))
    time.sleep(0.002)
    agent.on_post_api_request(**fx.post_api_request(turn_id="", inp=5))
    snap = _run(api.snapshot())
    assert snap["daily"]["turns"] == 2
    assert all(t["turn_id"].startswith("turn_") for t in snap["turns"])


# ---- tokens and cost ----

def test_cache_tokens_counted_from_hermes_fields():
    agent, api = _fresh("cache")
    agent.on_post_api_request(**fx.post_api_request(inp=1000, out=200,
                                                    cache_read=4000, cache_write=500))
    d = _run(api.snapshot())["daily"]
    assert d["tokens_in"] == 1000
    assert d["cache_read"] == 4000
    assert d["cache_write"] == 500
    assert d["tokens_prompt"] == 5500


def test_cost_prices_each_bucket_not_prompt_tokens():
    agent, api = _fresh("cost")
    # Sonnet 4.6: $3 in, $15 out, $0.30 cache read, $3.75 cache write per 1M.
    agent.on_post_api_request(**fx.post_api_request(inp=1_000_000, out=0,
                                                    cache_read=1_000_000))
    snap = _run(api.snapshot())
    assert abs(snap["daily"]["est_cost"] - 3.30) < 1e-9, snap["daily"]["est_cost"]
    assert snap["turns"][0]["priced"] is True


def test_cost_default_cache_ratios_when_row_has_none():
    agent, _ = _fresh("ratio")
    cost, priced = agent._cost("gpt-5", {"tokens_in": 0, "tokens_out": 0,
                                         "cache_read": 1_000_000, "cache_write": 1_000_000})
    assert priced and abs(cost - (0.125 + 1.5625)) < 1e-9, cost


def test_model_name_normalisation():
    agent, _ = _fresh("norm")
    row = agent._price_row
    sonnet = agent._PRICE_TABLE["claude-sonnet-4-6"]
    assert row("anthropic/claude-sonnet-4.6") == sonnet
    assert row("claude-sonnet-4-6-20250414") == sonnet
    assert row("claude-sonnet-4-5@20250929") == agent._PRICE_TABLE["claude-sonnet-4-5"]
    assert row("us.anthropic.claude-sonnet-4-6-v1:0") == sonnet
    assert row("Claude-Opus-4-5") == (5.00, 25.00, 0.50, 6.25)
    assert row("gpt-4o-mini-2024-07-18") == agent._PRICE_TABLE["gpt-4o-mini"]
    assert row("openai/gpt-5.6-sol-pro") == agent._PRICE_TABLE["gpt-5-6-sol"]
    assert row("gemini-3-pro-preview") == agent._PRICE_TABLE["gemini-3-pro"]
    assert row("accounts/fireworks/models/kimi-k2p6") == agent._PRICE_TABLE["kimi-k2p6"]
    assert row("totally-unknown-model") is None
    assert row(None) is None


def test_unknown_model_is_unpriced_not_free():
    agent, api = _fresh("unpriced")
    agent.on_post_api_request(**fx.post_api_request(model="my-local-llama", inp=10, out=10))
    snap = _run(api.snapshot())
    assert snap["turns"][0]["priced"] is False
    assert snap["daily"]["unpriced_calls"] == 1
    assert snap["daily"]["est_cost"] == 0.0


def test_missing_usage_leaves_priced_unknown():
    agent, api = _fresh("nousage")
    kw = fx.post_api_request()
    kw["usage"] = None
    agent.on_post_api_request(**kw)
    snap = _run(api.snapshot())
    assert snap["turns"][0]["priced"] is None
    assert snap["daily"]["unpriced_calls"] == 0


def test_api_done_event_carries_running_turn_totals():
    agent, api = _fresh("apidone")
    agent.on_post_api_request(**fx.post_api_request(turn_id="t1", inp=100, out=10))
    agent.on_post_tool_call(**fx.post_tool_call(turn_id="t1"))
    agent.on_post_api_request(**fx.post_api_request(turn_id="t1", inp=200, out=20))
    done = [e for e in _run(api.events(since=0))["events"] if e["kind"] == "api.done"]
    assert len(done) == 2
    last = done[-1]
    assert last["usage"]["tokens_in"] == 200
    assert last["turn"]["tokens_in"] == 300
    assert last["turn"]["api_calls"] == 2
    assert last["turn"]["tool_calls"] == 1
    assert last["turn"]["est_cost"] > 0


# ---- subagent rollup ----

def test_subagent_calls_roll_up_into_parent_turn():
    """A child agent's model and tool calls carry their own turn_id; they must
    count toward the parent turn, not as extra top-level turns."""
    agent, api = _fresh("rollup")
    agent.on_post_api_request(**fx.post_api_request(turn_id="parent", inp=100, out=10))
    agent.on_subagent_start(**fx.subagent_start(child_session_id="child-sess", parent_turn_id="parent"))
    agent.on_post_api_request(**fx.post_api_request(turn_id="child-turn", session_id="child-sess",
                                                    inp=1000, out=50))
    agent.on_pre_tool_call(**fx.pre_tool_call(call_id="k1", turn_id="child-turn", session_id="child-sess"))
    agent.on_post_tool_call(**fx.post_tool_call(call_id="k1", turn_id="child-turn", session_id="child-sess"))
    snap = _run(api.snapshot())
    assert snap["daily"]["turns"] == 1, snap["daily"]
    assert snap["daily"]["tokens_in"] == 1100
    assert [t["turn_id"] for t in snap["turns"]] == ["parent"]
    parent = snap["turns"][0]
    assert parent["tokens_in"] == 1100 and parent["api_calls"] == 2 and parent["tool_calls"] == 1
    assert [p["turn_id"] for p in snap["spark"]] == ["parent"]
    evs = _run(api.events(since=0))["events"]
    done = [e for e in evs if e["kind"] == "api.done"]
    assert done[-1]["turn_id"] == "parent" and done[-1]["child_turn_id"] == "child-turn"
    assert done[-1]["turn"]["tokens_in"] == 1100
    ends = [e for e in evs if e["kind"] == "tool.end"]
    assert ends[0]["root_turn_id"] == "parent"


def test_turn_times_come_from_the_call_not_the_hook():
    agent, api = _fresh("times")
    kw = fx.post_api_request(turn_id="t1", inp=1)
    agent.on_post_api_request(**kw)
    t = _run(api.snapshot())["turns"][0]
    assert t["started_at"] == kw["started_at"] and t["ended_at"] == kw["ended_at"]
    assert t["ended_at"] - t["started_at"] > 1.0


def test_old_store_gains_new_keys_on_reload():
    sys.modules[_STATE_KEY] = {k: v for k, v in _fresh("oldstore")[0]._init_state().items()
                               if k not in ("child_sessions", "turn_parent")}
    agent = _load("mc_agent_oldstore2", "__init__.py")
    agent.on_subagent_start(**fx.subagent_start())
    assert "child_sessions" in sys.modules[_STATE_KEY]
    assert sys.modules[_STATE_KEY]["child_sessions"]["child-sess-1"] == "turn-1"


# ---- loop detection ----

def _repeat_calls(agent, n, tool="read_file", args=None, session_id="sess-1", turn_id="T", start=0):
    for i in range(n):
        agent.on_post_tool_call(**fx.post_tool_call(call_id=f"{session_id}-{start + i}", tool=tool,
                                                    turn_id=turn_id, session_id=session_id,
                                                    args=args if args is not None else {"path": "same.txt"}))


def _kinds(api, kind):
    return [e for e in _run(api.events(since=0))["events"] if e["kind"] == kind]


def test_loop_alert_fires_once_per_streak():
    agent, api = _fresh("loop1")
    agent.on_post_api_request(**fx.post_api_request(turn_id="T", inp=1))
    _repeat_calls(agent, 8)
    alerts = _kinds(api, "loop.alert")
    assert len(alerts) == 1 and alerts[0]["count"] == 5 and alerts[0]["tool"] == "read_file"
    assert _run(api.snapshot())["daily"]["loops"] == 1
    t = _run(api.snapshot())["turns"][0]
    assert t["loop_max"] == 8 and t["loop_tool"] == "read_file"


def test_tool_end_still_recorded_and_carries_repeat():
    """The non-alerting path must never lose the tool.end event."""
    agent, api = _fresh("loop2")
    _repeat_calls(agent, 6)
    ends = _kinds(api, "tool.end")
    assert [e["repeat"] for e in ends] == [1, 2, 3, 4, 5, 6]
    seqs = [(e["kind"], e["seq"]) for e in _run(api.events(since=0))["events"]]
    end5 = next(s for k, s in seqs if k == "tool.end" and s == ends[4]["seq"])
    alert = next(s for k, s in seqs if k == "loop.alert")
    assert alert > end5, "loop.alert is recorded after the tool.end that triggered it"


def test_loop_broken_by_different_tool_then_fires():
    agent, api = _fresh("loop3")
    _repeat_calls(agent, 4)
    _repeat_calls(agent, 1, tool="terminal", args={"command": "ls"}, start=10)
    assert _kinds(api, "loop.alert") == []
    _repeat_calls(agent, 5, start=20)
    assert len(_kinds(api, "loop.alert")) == 1


def test_same_tool_different_args_is_not_a_loop():
    agent, api = _fresh("loop4")
    for i in range(8):
        agent.on_post_tool_call(**fx.post_tool_call(call_id=f"r{i}", tool="read_file",
                                                    turn_id="T", args={"path": f"file{i}.md"}))
    assert _kinds(api, "loop.alert") == []
    assert all(e["repeat"] == 1 for e in _kinds(api, "tool.end"))


def test_interleaved_sessions_do_not_build_a_fake_streak():
    """Two parallel subagents doing the same thing alternately: each agent's
    own streak counts, the shared turn never sees a combined 8."""
    agent, api = _fresh("loop5")
    for i in range(4):
        _repeat_calls(agent, 1, session_id="child-a", start=i)
        _repeat_calls(agent, 1, session_id="child-b", start=i)
    assert _kinds(api, "loop.alert") == []
    assert max(e["repeat"] for e in _kinds(api, "tool.end")) == 4


def test_loop_rolls_up_to_root_turn():
    agent, api = _fresh("loop6")
    agent.on_post_api_request(**fx.post_api_request(turn_id="P", inp=1))
    agent.on_subagent_start(**fx.subagent_start(child_session_id="kid", parent_turn_id="P"))
    agent.on_post_api_request(**fx.post_api_request(turn_id="C", session_id="kid", inp=1))
    _repeat_calls(agent, 5, session_id="kid", turn_id="C")
    alerts = _kinds(api, "loop.alert")
    assert len(alerts) == 1 and alerts[0]["turn_id"] == "P"
    assert _run(api.snapshot())["turns"][0]["loop_max"] == 5


def test_blocked_or_cancelled_calls_do_not_count():
    agent, api = _fresh("loop7")
    for i in range(6):
        agent.on_post_tool_call(**fx.post_tool_call(call_id=f"b{i}", status="blocked", turn_id="T"))
    assert _kinds(api, "loop.alert") == []
    assert all(e["repeat"] == 0 for e in _kinds(api, "tool.end"))


def test_unhashable_args_and_bare_call_fail_open():
    agent, api = _fresh("loop8")
    agent.on_post_tool_call()
    _repeat_calls(agent, 5, args={"obj": object()})        # default=str keeps it hashable
    assert len(_kinds(api, "loop.alert")) == 1


def test_args_are_never_stored():
    agent, api = _fresh("loop9")
    _repeat_calls(agent, 5, args={"secret": "hunter2"})
    import json as _json
    blob = _json.dumps(_run(api.snapshot()), default=str) + _json.dumps(_run(api.events(since=0)), default=str)
    blob += repr(sys.modules[_STATE_KEY]["streaks"])
    assert "hunter2" not in blob


# ---- tools ----

def test_zombie_reap():
    agent, api = _fresh("zombie")
    st = sys.modules[_STATE_KEY]
    st["inflight"]["zombie-1"] = {"tool": "read_file", "turn_id": "t1",
                                  "started_at": time.time() - 200}
    st["inflight"]["live-1"] = {"tool": "bash", "turn_id": "t1",
                                "started_at": time.time()}
    snap = _run(api.snapshot())
    ids = {r["id"] for r in snap["inflight"]}
    assert "zombie-1" not in ids and "live-1" in ids
    cancelled = [e for e in snap["events"]
                 if e["kind"] == "tool.end" and e.get("status") == "cancelled"]
    assert any(e["id"] == "zombie-1" for e in cancelled)


def test_tool_call_without_id_pairs_by_fallback():
    agent, api = _fresh("noid")
    agent.on_pre_tool_call(**fx.pre_tool_call(call_id=""))
    assert len(_run(api.snapshot())["inflight"]) == 1
    agent.on_post_tool_call(**fx.post_tool_call(call_id=""))
    assert _run(api.snapshot())["inflight"] == []


# ---- api errors ----

def test_api_errors_split_retryable():
    agent, api = _fresh("apierr")
    agent.on_api_request_error(**fx.api_request_error(retryable=True, status_code=529))
    agent.on_api_request_error(**fx.api_request_error(retryable=False))
    snap = _run(api.snapshot())
    assert snap["daily"]["api_retries"] == 1
    assert snap["daily"]["errors"] == 1
    errs = [e for e in snap["events"] if e["kind"] == "api.error"]
    assert errs[0]["error_type"] == "APIStatusError" and errs[0]["retryable"] is True


# ---- subagents ----

def test_subagent_stop_pairs_by_child_session_id():
    agent, api = _fresh("subpair")
    agent.on_subagent_start(**fx.subagent_start(child_session_id="c1", subagent_id="sa-0-a"))
    agent.on_subagent_start(**fx.subagent_start(child_session_id="c2", subagent_id="sa-1-b"))
    agent.on_subagent_stop(**fx.subagent_stop(child_session_id="c2", status="failed"))
    subs = {s["id"]: s for s in _run(api.snapshot())["subagents"]}
    assert subs["c1"]["status"] == "running"
    assert subs["c2"]["status"] == "failed"
    assert subs["c2"]["summary"] == "found 3 files"
    assert subs["c2"]["subagent_id"] == "sa-1-b"


def test_subagent_stop_fallback_by_parent_and_role():
    agent, api = _fresh("subfallback")
    agent.on_subagent_start(**fx.subagent_start(child_session_id=None, subagent_id="sa-0-a"))
    agent.on_subagent_stop(**fx.subagent_stop(child_session_id=None))
    subs = _run(api.snapshot())["subagents"]
    assert subs[0]["id"] == "sa-0-a" and subs[0]["status"] == "completed"


def test_subagent_cap():
    agent, _ = _fresh("subcap")
    st = sys.modules[_STATE_KEY]
    for i in range(60):
        agent.on_subagent_start(**fx.subagent_start(child_session_id=f"c{i}"))
    assert len(st["subagents"]) == 50  # _SUBAGENTS_MAX


# ---- approvals ----

def test_approval_choices_bucketed():
    agent, api = _fresh("approvals")
    for choice in ("once", "always", "deny", "smart_deny", "timeout", "cancelled"):
        agent.on_post_approval_response(**fx.post_approval_response(choice=choice))
    d = _run(api.snapshot())["daily"]
    assert d["approvals"] == 6
    assert d["approvals_denied"] == 2
    assert d["approvals_unanswered"] == 2


# ---- routes ----

def test_events_since_cursor():
    agent, api = _fresh("events")
    agent.on_pre_tool_call(**fx.pre_tool_call(call_id="c1", tool="bash"))
    mid = _run(api.events(since=0))["max_seq"]
    agent.on_post_tool_call(**fx.post_tool_call(call_id="c1", tool="bash", duration_ms=12))
    delta = _run(api.events(since=mid))
    assert [e["kind"] for e in delta["events"]] == ["tool.end"]
    assert delta["reset"] is False


def test_events_cursor_ahead_of_server_resets():
    agent, api = _fresh("restart")
    agent.on_pre_tool_call(**fx.pre_tool_call())
    r = _run(api.events(since=9999))
    assert r["reset"] is True
    assert [e["kind"] for e in r["events"]] == ["tool.start"]


def test_snapshot_returns_copies():
    agent, api = _fresh("copies")
    agent.on_post_api_request(**fx.post_api_request(inp=10))
    snap = _run(api.snapshot())
    snap["daily"]["tokens_in"] = 999
    assert sys.modules[_STATE_KEY]["daily"]["tokens_in"] == 10


def test_spark_is_per_turn_oldest_first():
    agent, api = _fresh("spark")
    for i in range(15):
        agent.on_post_api_request(**fx.post_api_request(turn_id=f"t{i}", inp=i + 1))
        time.sleep(0.001)
    spark = _run(api.snapshot())["spark"]
    assert len(spark) == 12
    assert [p["turn_id"] for p in spark] == [f"t{i}" for i in range(3, 15)]
    assert spark[-1]["tokens"] == 15


def test_reset_route_zeroes_daily():
    agent, api = _fresh("reset")
    agent.on_post_api_request(**fx.post_api_request(inp=10, cache_read=5))
    agent.on_post_tool_call(**fx.post_tool_call(status="error", error_type="FileNotFoundError"))
    _run(api.reset())
    snap = _run(api.snapshot())
    d = snap["daily"]
    assert all(v == 0 for k, v in d.items() if k != "date"), d
    assert isinstance(d["est_cost"], float)
    assert snap["turns"] == [] and snap["inflight"] == [] and snap["spark"] == []


# ---- contract ----

def test_hook_parity_with_manifest():
    """register() must declare exactly the 8 hooks listed in plugin.yaml."""
    import re

    yaml_text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    m = re.search(r"provides_hooks:\n((?:  - .+\n?)+)", yaml_text)
    declared = set(re.findall(r"-\s+(\w+)", m.group(1)))
    agent = _load("mc_agent_parity", "__init__.py")
    registered = {}

    class FakeCtx:
        def register_hook(self, name, cb):
            registered[name] = cb

    agent.register(FakeCtx())
    assert set(registered) == declared
    assert len(registered) == 8


def test_callbacks_fail_open_on_garbage():
    agent, _ = _fresh("failopen")
    agent.on_pre_tool_call()
    agent.on_post_tool_call()
    agent.on_post_api_request(usage="not-a-dict")
    agent.on_post_api_request(usage={"input_tokens": "lots"})
    agent.on_api_request_error(error="not-a-dict")
    agent.on_subagent_start(child_subagent_id=None)
    agent.on_subagent_stop()
    agent.on_pre_approval_request()
    agent.on_post_approval_response()
    st = sys.modules[_STATE_KEY]
    assert isinstance(st["daily"]["turns"], int)


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)} tests passed")


if __name__ == "__main__":
    _main()
