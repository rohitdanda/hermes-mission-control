"""Print a realistic backend state as JSON for tests/desktop_smoke.mjs:
the /snapshot and /events?since=0 bodies after a scripted turn with a tool
still in flight, a failed tool, a subagent pair, a denied approval and an
over-budget turn.

    python tests/desktop_state.py state.json
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as fx  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agent = _load("mc_agent_demo", "__init__.py")
api = _load("mc_api_demo", "dashboard/plugin_api.py")
run = asyncio.new_event_loop().run_until_complete

for i in range(3):
    agent.on_post_api_request(**fx.post_api_request(turn_id=f"old-{i}", inp=2000 * (i + 1), out=300))
agent.on_post_api_request(**fx.post_api_request(turn_id="t1", model="claude-opus-4-5", inp=120_000,
                                                out=9_000, cache_read=400_000, cache_write=20_000))
agent.on_pre_tool_call(**fx.pre_tool_call(call_id="c1", tool="read_file", turn_id="t1"))
agent.on_post_tool_call(**fx.post_tool_call(call_id="c1", tool="read_file", turn_id="t1", duration_ms=420))
agent.on_pre_tool_call(**fx.pre_tool_call(call_id="c2", tool="terminal", turn_id="t1"))
agent.on_post_tool_call(**fx.post_tool_call(call_id="c2", tool="terminal", turn_id="t1", status="error",
                                            error_type="FileNotFoundError", duration_ms=95))
agent.on_subagent_start(**fx.subagent_start(child_session_id="cs1", role="explore", parent_turn_id="t1"))
agent.on_post_api_request(**fx.post_api_request(turn_id="child-t1", session_id="cs1", inp=5000, out=100))
agent.on_pre_tool_call(**fx.pre_tool_call(call_id="k1", tool="search_files", turn_id="child-t1", session_id="cs1"))
agent.on_post_tool_call(**fx.post_tool_call(call_id="k1", tool="search_files", turn_id="child-t1",
                                            session_id="cs1", duration_ms=800))
agent.on_subagent_start(**fx.subagent_start(child_session_id="cs2", role="review", parent_turn_id="t1"))
agent.on_subagent_stop(**fx.subagent_stop(child_session_id="cs1", parent_turn_id="t1", role="explore"))
agent.on_pre_approval_request(**fx.pre_approval_request(turn_id="t1"))
agent.on_post_approval_response(**fx.post_approval_response(choice="deny", turn_id="t1"))
agent.on_api_request_error(**fx.api_request_error(turn_id="t1", retryable=True, status_code=529))
agent.on_pre_tool_call(**fx.pre_tool_call(call_id="c3", tool="web_search", turn_id="t1"))

out = json.dumps({"snapshot": run(api.snapshot()), "events": run(api.events(since=0))})
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(out, encoding="utf-8")
else:
    print(out)
