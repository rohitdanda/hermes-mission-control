"""Hook payloads shaped like Hermes's real call sites (Hermes 0.22.5):

- post_api_request: agent/turn_response_intake.py; usage from
  agent/api_request_hooks.py (CanonicalUsage dict, input_tokens excludes cache)
- pre/post_tool_call: hermes_cli/plugins.py, model_tools.py (ids coerced to "")
- api_request_error: agent/api_request_hooks.py
- subagent_start: tools/delegate_tool.py
- subagent_stop: tools/delegate_tool_results.py (NO child_subagent_id)
- approvals: tools/approval_context.py
"""

import time



def usage(inp=0, out=0, cache_read=0, cache_write=0, reasoning=0):
    prompt = inp + cache_read + cache_write
    return {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write, "reasoning_tokens": reasoning,
            "request_count": 1, "prompt_tokens": prompt, "total_tokens": prompt + out}


def post_api_request(turn_id="turn-1", model="claude-sonnet-4-6", provider="anthropic",
                     session_id="sess-1", **u):
    now = time.time()
    return {"task_id": "task-1", "turn_id": turn_id, "api_request_id": "req-1",
            "session_id": session_id, "platform": "desktop", "model": model,
            "provider": provider, "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages", "api_call_count": 1, "api_duration": 1.23,
            "started_at": now - 1.23, "ended_at": now,
            "first_chunk_at": now - 0.7, "finish_reason": "stop", "message_count": 4,
            "response_model": model, "response": {}, "usage": usage(**u),
            "context_length": 200000, "assistant_message": None,
            "assistant_content_chars": 120, "assistant_tool_call_count": 1,
            "moa_references": None, "telemetry_schema_version": 1}


def pre_tool_call(call_id="call-1", tool="read_file", turn_id="turn-1", session_id="sess-1"):
    return {"tool_name": tool, "args": {"path": "x"}, "task_id": "task-1",
            "session_id": session_id, "tool_call_id": call_id, "turn_id": turn_id,
            "api_request_id": "req-1", "middleware_trace": [], "telemetry_schema_version": 1}


def post_tool_call(call_id="call-1", tool="read_file", turn_id="turn-1", status="ok",
                   duration_ms=42, error_type=None, session_id="sess-1"):
    return {"tool_name": tool, "args": {"path": "x"}, "result": "{}", "task_id": "task-1",
            "session_id": session_id, "tool_call_id": call_id, "turn_id": turn_id,
            "api_request_id": "req-1", "duration_ms": duration_ms, "status": status,
            "error_type": error_type, "error_message": None, "middleware_trace": [],
            "telemetry_schema_version": 1}


def api_request_error(turn_id="turn-1", retryable=False, status_code=500):
    return {"task_id": "task-1", "turn_id": turn_id, "api_request_id": "req-2",
            "session_id": "sess-1", "platform": "desktop", "model": "claude-sonnet-4-6",
            "provider": "anthropic", "base_url": "", "api_mode": "anthropic_messages",
            "api_call_count": 2, "api_duration": 0.4, "started_at": 0.0, "ended_at": 0.4,
            "status_code": status_code, "retry_count": 0, "max_retries": 3,
            "retryable": retryable, "reason": "server_error",
            "error": {"type": "APIStatusError", "message": "boom"}, "request": {}}


def subagent_start(child_session_id="child-sess-1", subagent_id="sa-0-abcd1234",
                   role="explore", parent_turn_id="turn-1"):
    return {"parent_session_id": "sess-1", "parent_turn_id": parent_turn_id,
            "parent_subagent_id": None, "child_session_id": child_session_id,
            "child_subagent_id": subagent_id, "child_role": role, "child_goal": "scan repo"}


def subagent_stop(child_session_id="child-sess-1", role="explore", parent_turn_id="turn-1",
                  status="completed", duration_ms=1500):
    return {"parent_session_id": "sess-1", "parent_turn_id": parent_turn_id,
            "child_session_id": child_session_id, "child_role": role,
            "child_summary": "found 3 files", "child_status": status,
            "tool_call_history": [], "duration_ms": duration_ms}


def pre_approval_request(turn_id="turn-1"):
    return {"command": "rm -rf build", "description": "delete build", "pattern_key": "rm",
            "pattern_keys": ["rm"], "session_key": "sess-1", "surface": "desktop",
            "turn_id": turn_id, "tool_call_id": "call-9", "session_id": "sess-1"}


def post_approval_response(choice="deny", turn_id="turn-1"):
    return {**pre_approval_request(turn_id), "choice": choice}
