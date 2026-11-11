"""Layout changes must retain authoritative context and invalidation inputs."""

import copy
import json

from agentgpt.prefix_layout import payload_json


def test_layout_preserves_context_and_moves_changing_request_after_history():
    payload = {
        "current_request": "改为800",
        "manifest": {"user_id": "u1", "context_ids": ["memory:m:v2"]},
        "recent_turns": [{"text": "预算1000"}],
        "memory_context": [{"context_id": "memory:m:v2", "content": "中文"}],
        "context_sources": [{"text": "中文", "user_id": "u1"}],
        "memory_receipt": {"status": "saved"},
    }
    original = copy.deepcopy(payload)
    baseline = payload_json(payload)
    candidate = payload_json(payload, True)
    assert baseline == json.dumps(payload, ensure_ascii=False)
    assert json.loads(candidate) == payload == original
    assert candidate.index('"recent_turns"') < candidate.index('"current_request"')
    changed = copy.deepcopy(payload)
    changed["memory_context"] = []
    changed["manifest"]["context_ids"] = []
    assert json.loads(payload_json(changed, True))["memory_context"] == []
    assert payload_json(changed, True) != candidate
