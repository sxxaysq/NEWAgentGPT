"""Official SDK wire objects and validated application artifacts."""

from typing import Literal

from a2a.helpers import new_data_part, new_text_part
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict, Field

SUBMITTED = TaskState.TASK_STATE_SUBMITTED
WORKING = TaskState.TASK_STATE_WORKING
INPUT_REQUIRED = TaskState.TASK_STATE_INPUT_REQUIRED
COMPLETED = TaskState.TASK_STATE_COMPLETED
FAILED = TaskState.TASK_STATE_FAILED
CANCELED = TaskState.TASK_STATE_CANCELED
REJECTED = TaskState.TASK_STATE_REJECTED
TERMINAL_STATES = {COMPLETED, FAILED, CANCELED, REJECTED}


def DataPart(data):
    return new_data_part(data, media_type="application/json")


def TextPart(text):
    return new_text_part(text)


def part_data(parts):
    for part in parts:
        if part.HasField("data"):
            return MessageToDict(part.data)
    return {}


class StrictModel(BaseModel):
    """Business artifacts are strict: an unknown field is a parsing error."""

    model_config = ConfigDict(extra="forbid", strict=False, str_strip_whitespace=True)


class TaskSpec(StrictModel):
    id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=2000)
    depends_on: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list, max_length=6)
    output_kind: Literal["findings", "answer"] = "findings"


class PlanArtifact(StrictModel):
    tasks: list[TaskSpec] = Field(min_length=1, max_length=10)
    rationale: str = Field(default="", max_length=1000)


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    context_ids: list[str] = Field(default_factory=list, max_length=20)


class FindingsArtifact(StrictModel):
    task_id: str = ""
    summary: str = Field(default="", max_length=8000)
    claims: list[Claim] = Field(default_factory=list, max_length=40)
    gaps: list[str] = Field(default_factory=list, max_length=20)
    tool_calls: int = 0


class ReviewArtifact(StrictModel):
    task_id: str | None = None
    verdict: Literal["pass", "revise", "reject"]
    reasons: list[str] = Field(default_factory=list, max_length=8)
    actions: list[str] = Field(default_factory=list, max_length=8)


class FinalArtifact(StrictModel):
    answer_md: str = Field(min_length=1, max_length=20000)
    citations: list[str] = Field(default_factory=list, max_length=200)


__all__ = [
    "AgentCard",
    "AgentCapabilities",
    "AgentInterface",
    "AgentSkill",
    "Artifact",
    "Message",
    "Part",
    "Role",
    "Task",
    "TaskState",
    "TaskStatus",
    "TaskStatusUpdateEvent",
    "TaskArtifactUpdateEvent",
    "DataPart",
    "TextPart",
    "part_data",
    "SUBMITTED",
    "WORKING",
    "INPUT_REQUIRED",
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "REJECTED",
    "TERMINAL_STATES",
    "StrictModel",
    "TaskSpec",
    "PlanArtifact",
    "Claim",
    "FindingsArtifact",
    "ReviewArtifact",
    "FinalArtifact",
]
