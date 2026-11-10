"""Push-bridge test: hooks broadcast each event to the Desktop through
hermes_cli.plugin_events, only when the gateway transport is loaded.
Uses stub modules, so it runs without Hermes installed.
"""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as fx  # noqa: E402

_STATE_KEY = "_hermes_mission_control_state"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_push_only_with_gateway_and_never_raises():
    sys.modules.pop(_STATE_KEY, None)
    sent = []
    events_mod = types.ModuleType("hermes_cli.plugin_events")
    events_mod.broadcast_plugin_event = lambda pid, ev, payload: sent.append((pid, ev, payload))
    saved = {k: sys.modules.get(k) for k in ("hermes_cli", "hermes_cli.plugin_events", "tui_gateway.server")}
    sys.modules["hermes_cli"] = types.ModuleType("hermes_cli")
    sys.modules["hermes_cli.plugin_events"] = events_mod
    sys.modules.pop("tui_gateway.server", None)
    try:
        agent = _load("mc_agent_push")
        agent.on_pre_tool_call(**fx.pre_tool_call(call_id="c1"))
        assert sent == [], "no gateway transport loaded: must not push"

        sys.modules["tui_gateway.server"] = types.ModuleType("tui_gateway.server")
        agent.on_post_tool_call(**fx.post_tool_call(call_id="c1"))
        assert len(sent) == 1
        pid, ev, payload = sent[0]
        assert (pid, ev) == ("mission-control", "telemetry")
        assert payload["kind"] == "tool.end" and payload["seq"] == 2

        def boom(*a, **k):
            raise RuntimeError("transport down")
        events_mod.broadcast_plugin_event = boom
        agent.on_post_api_request(**fx.post_api_request(inp=5))   # must not raise
        assert sys.modules[_STATE_KEY]["daily"]["tokens_in"] == 5
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


if __name__ == "__main__":
    test_push_only_with_gateway_and_never_raises()
    print("PASS test_push_only_with_gateway_and_never_raises")
