"""End-to-end over Hermes's real web server: discovery → manifest parse →
in-process mount of dashboard/plugin_api.py → HTTP snapshot/events/reset,
with observer-hook traffic (shaped like Hermes's real call sites, see
tests/fixtures.py) fired in-process.

Run with the Hermes venv python. The hermes-agent checkout is found from
HERMES_AGENT_DIR, else %LOCALAPPDATA%/hermes/hermes-agent, else ~/.hermes/hermes-agent:
    <hermes-agent>/venv/Scripts/python.exe tests/e2e_http.py
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as fx  # noqa: E402


def _hermes_agent_dir():
    candidates = [os.environ.get("HERMES_AGENT_DIR"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "hermes-agent"),
                  os.path.expanduser("~/.hermes/hermes-agent")]
    for c in candidates:
        if c and (Path(c) / "hermes_cli" / "web_server.py").is_file():
            return c
    sys.exit("hermes-agent checkout not found; set HERMES_AGENT_DIR")


sys.path.insert(0, _hermes_agent_dir())

home = Path(tempfile.mkdtemp(prefix="mc-e2e-"))
os.environ["HERMES_HOME"] = str(home)
(home / "plugins").mkdir(parents=True, exist_ok=True)
shutil.copytree(ROOT, home / "plugins" / "mission-control",
                ignore=shutil.ignore_patterns(".git", "__pycache__", "tests"))
(home / "config.yaml").write_text(json.dumps(
    {"plugins": {"enabled": ["mission-control"], "isolation": "in_process"}}))

from starlette.testclient import TestClient          # noqa: E402
from hermes_cli import web_server                    # noqa: E402


async def _loopback_peer(scope, receive, send):
    scope["hermes.gateway_socket_peer"] = ("127.0.0.1", 50000)
    await web_server.app(scope, receive, send)


client = TestClient(_loopback_peer)
AUTH = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
BASE = "/api/plugins/mission-control"


def get(path):
    r = client.get(BASE + path, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


# 1. Backend-absent state: routes valid, empty, no 500.
j = get("/snapshot")
assert j["daily"] is None, j

# 2. Agent half enters the process AFTER the server mounted the API
#    (the v3 load-order scenario).
spec = importlib.util.spec_from_file_location(
    "mc_e2e_agent", home / "plugins" / "mission-control" / "__init__.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

agent.on_post_api_request(**fx.post_api_request(turn_id="t1", inp=1200, out=300,
                                                cache_read=4000, cache_write=100))
agent.on_pre_tool_call(**fx.pre_tool_call(call_id="c1", turn_id="t1"))
agent.on_post_tool_call(**fx.post_tool_call(call_id="c1", turn_id="t1"))
agent.on_post_tool_call(**fx.post_tool_call(call_id="c2", tool="terminal", turn_id="t1",
                                            status="error", error_type="FileNotFoundError",
                                            duration_ms=7))
agent.on_api_request_error(**fx.api_request_error(turn_id="t1", retryable=False))
agent.on_api_request_error(**fx.api_request_error(turn_id="t1", retryable=True))
agent.on_subagent_start(**fx.subagent_start(parent_turn_id="t1"))
agent.on_subagent_stop(**fx.subagent_stop(parent_turn_id="t1"))
agent.on_pre_approval_request(**fx.pre_approval_request(turn_id="t1"))
agent.on_post_approval_response(**fx.post_approval_response(choice="deny", turn_id="t1"))

# 3. Snapshot reflects everything.
j = get("/snapshot")
d = j["daily"]
assert d["turns"] == 1, d
assert d["tokens_in"] == 1200 and d["tokens_out"] == 300, d
assert d["cache_read"] == 4000 and d["cache_write"] == 100, d
assert d["tokens_prompt"] == 5300, d
assert d["tool_calls"] == 2, d
assert d["errors"] == 2 and d["api_retries"] == 1, d   # tool error + fatal api error
assert d["approvals"] == 1 and d["approvals_denied"] == 1, d
assert d["est_cost"] > 0 and d["unpriced_calls"] == 0, d
assert j["inflight"] == [], j
turn = j["turns"][0]
assert turn["turn_id"] == "t1" and turn["model"] == "claude-sonnet-4-6", turn
assert turn["provider"] == "anthropic" and turn["priced"] is True, turn
assert turn["tool_calls"] == 2, turn
assert j["subagents"][0]["status"] == "completed", j["subagents"]
assert j["spark"] and j["spark"][-1]["turn_id"] == "t1", j["spark"]
kinds = [e["kind"] for e in j["events"]]
for k in ("api.done", "tool.start", "tool.end", "api.error", "subagent.start",
          "subagent.end", "approval.ask", "approval.answer"):
    assert k in kinds, kinds

# 4. Events cursor, restart resync, and reset over HTTP.
since = j["max_seq"]
assert get(f"/events?since={since + 1000}")["reset"] is True
r = client.post(BASE + "/reset", headers=AUTH)
assert r.status_code == 200, r.text
j2 = get(f"/events?since={since}")
assert j2["events"] == [] and j2["reset"] is False, j2
j3 = get("/snapshot")
assert j3["daily"]["turns"] == 0 and j3["turns"] == [], j3["daily"]

print("E2E PASS — real web server, mount, hooks, snapshot/events/reset over HTTP")
shutil.rmtree(home, ignore_errors=True)
