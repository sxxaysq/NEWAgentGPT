"""Validate source-backed session proposals without elevating generated text to authority."""

from copy import deepcopy
from uuid import uuid4


def constraint_scope(value):
    """Normalize legacy labels; newly generated proposals use the canonical enum."""
    if value in {"turn", "本次", "本轮", "仅本次", "仅本轮", "this_turn", "one_turn"}:
        return "turn"
    if value in {"project", "项目", "整个项目"}:
        return "project"
    return "thread"


def apply_proposal(previous, proposal, source):
    """Return a derived view; invalid quotes/conflicting targets never change constraints."""
    state = deepcopy(previous)
    for key in ("objects", "constraints", "open_questions", "artifact_refs"):
        state.setdefault(key, [])
    text, input_id = source["text"], source["id"]
    conflicts = []
    for obj in proposal.get("objects", []):
        quote = obj.get("quote", "")
        if not quote or quote not in text or not obj.get("name"):
            raise ValueError("Object proposal has no exact user source")
        state["objects"] = [
            existing for existing in state["objects"] if existing.get("name") != obj["name"]
        ] + [{**obj, "source_input_id": input_id}]
    for item in proposal.get("constraints", []):
        item = {**item, "scope": constraint_scope(item.get("scope", ""))}
        quote = item.get("quote", "")
        if not quote or quote not in text:
            raise ValueError("Constraint proposal has no exact user source")
        if not all(
            isinstance(item.get(k), str) and item[k].strip()
            for k in ("entity", "property", "value")
        ):
            raise ValueError("Invalid constraint structure")
        target = next(
            (c for c in state["constraints"] if c["constraint_id"] == item.get("supersedes")), None
        )
        matching = [
            c
            for c in state["constraints"]
            if c["status"] == "active"
            and (c["entity"], c["property"], constraint_scope(c.get("scope", "")))
            == (item["entity"], item["property"], item["scope"])
            and (item["scope"] != "turn" or c.get("source_turn_id") == source.get("turn_id"))
        ]
        if item.get("supersedes") and (not target or target not in matching):
            conflicts.append(
                {"text": "无法确认要替换的约束", "proposal": item, "source_input_id": input_id}
            )
            continue
        same = next(
            (
                c
                for c in matching
                if (c["value"], c.get("unit", "")) == (item["value"], item.get("unit", ""))
            ),
            None,
        )
        if same:
            same.setdefault("confirmations", []).append(
                {"source_input_id": input_id, "quote": quote}
            )
            continue
        if matching and not target:
            conflicts.append(
                {
                    "text": "相同属性存在不同值，需确认覆盖关系",
                    "proposal": item,
                    "source_input_id": input_id,
                }
            )
            continue
        identifier = uuid4().hex
        if target:
            target.update(status="superseded", superseded_by=identifier)
        state["constraints"].append(
            {
                **item,
                "constraint_id": identifier,
                "status": "active",
                "source_input_id": input_id,
                "source_turn_id": source.get("turn_id", ""),
                "applies_to_turn_id": source.get("turn_id", "") if item["scope"] == "turn" else "",
                "superseded_by": None,
            }
        )
    for question in proposal.get("open_questions", []):
        quote = question.get("quote", "")
        if not quote or quote not in text:
            raise ValueError("Question proposal has no exact user source")
        state["open_questions"].append({**question, "source_input_id": input_id})
    state["open_questions"].extend(conflicts)
    return state
