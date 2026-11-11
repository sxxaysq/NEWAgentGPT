"""Optional cache-friendly ordering; never drop or rewrite context values."""

import json

STABLE_FIELDS = (
    "language", "语言", "available_downstream", "私有知识库",
    "thread_summary", "turn_summaries", "memory_context", "recent_turns",
)


def payload_json(payload: dict, enabled: bool = False) -> str:
    if enabled:
        # Preserve nested order and source/version IDs. Sorting all keys would move
        # current_request ahead of history and defeat sequential prefix reuse.
        payload = {
            **{key: payload[key] for key in STABLE_FIELDS if key in payload},
            **{key: value for key, value in payload.items() if key not in STABLE_FIELDS},
        }
    return json.dumps(payload, ensure_ascii=False)
