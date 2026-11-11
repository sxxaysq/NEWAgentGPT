"""Unsupported save claims must not leak through the answer or event stream."""

import pytest

from agentgpt.host.memory_receipts import sanitize_memory_claims
from agentgpt.host.orchestrator import Orchestrator


@pytest.mark.parametrize("status", ["noop", "failed", "disabled", "clarification"])
@pytest.mark.parametrize(
    "claim",
    [
        "已记下你的饮食偏好：**不吃香菜**。",
        "我已经将该偏好纳入长期记忆。",
        "我已保存你的语言偏好。",
        "I've saved your preferences for future conversations.",
    ],
)
def test_uncommitted_save_claim_is_corrected_without_losing_business_answer(status, claim):
    text = claim + "\n向量数据库用于检索相似向量。\n1. 保留业务内容。"
    corrected = sanitize_memory_claims(text, {"status": status})
    assert claim not in corrected
    assert "本轮未提交长期记忆变更。" in corrected
    assert corrected.endswith("\n向量数据库用于检索相似向量。\n1. 保留业务内容。")
    assert sanitize_memory_claims(corrected, {"status": status}) == corrected


def test_business_saves_and_negative_disclosures_are_preserved():
    text = "已保存文件到 report.txt。\n本轮无法确认是否已记下该偏好。"
    assert sanitize_memory_claims(text, {"status": "failed"}) == text
    assert sanitize_memory_claims("已记下你的偏好。", {"status": "committed"}) == "已记下你的偏好。"
    assert sanitize_memory_claims(text, None) == text


@pytest.mark.parametrize(
    "text",
    [
        "客服回复模板：已记下您反馈的故障，我们会尽快安排维修。",
        "会议纪要：已记下三项待办，请负责人在周五前确认。",
        "客服回复模板：已记下您的住宿偏好，我们会据此安排。",
        "> 已保存你的偏好。",
    ],
)
def test_business_artifacts_are_not_treated_as_host_memory_confirmations(text):
    assert sanitize_memory_claims(text, {"status": "noop"}) == text


async def test_stream_is_sanitized_before_events_are_written():
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    orchestrator = Orchestrator(None, None, None, emit)
    orchestrator.context = {"memory_receipt": {"status": "failed"}}
    await orchestrator._say("已记下你的偏好。这里是本次业务答复。")
    assert events and all(kind == "token" for kind, _ in events)
    streamed = "".join(data["text"] for _, data in events)
    assert "已记下" not in streamed
    assert "本次业务答复" in streamed
