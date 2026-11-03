"""Prompt invariants: teaching by example, not by prohibition.

The old prompt carried an 870-character source policy with 26 forced words injected
into three nodes. These tests keep the replacement honest: every prompt has a role
statement, worked examples, and only a handful of imperatives.
"""

import re

import pytest

from agentgpt.agents import prompts

PROMPTS = {
    "PLANNER": prompts.PLANNER,
    "PLANNER_REVISE": prompts.PLANNER_REVISE,
    "WORKER": prompts.WORKER,
    "CRITIC_VERIFY": prompts.CRITIC_VERIFY,
    "CRITIC_PLAN": prompts.CRITIC_PLAN,
    "SYNTHESIZER": prompts.SYNTHESIZER,
}
FORCED = ("必须", "不得", "禁止", "不要", "只能", "不允许", "严禁")
LEGACY = "根据用户语义选择资料来源"


@pytest.mark.parametrize("name", PROMPTS)
def test_every_prompt_opens_with_a_role(name):
    assert PROMPTS[name].lstrip().startswith("# 角色"), name


@pytest.mark.parametrize(
    "name", ["PLANNER", "WORKER", "CRITIC_VERIFY", "CRITIC_PLAN", "SYNTHESIZER"]
)
def test_teaching_happens_through_examples(name):
    body = PROMPTS[name]
    assert "示例" in body, name
    assert body.count("# 示例") >= 2, name


@pytest.mark.parametrize("name", PROMPTS)
def test_imperatives_stay_within_budget(name):
    body = PROMPTS[name]
    total = sum(body.count(word) for word in FORCED)
    assert total <= 5, f"{name} 有 {total} 处强制词：{body}"


@pytest.mark.parametrize("name", PROMPTS)
def test_prompts_stay_compact(name):
    assert len(PROMPTS[name]) < 2600, name


@pytest.mark.parametrize("name", PROMPTS)
def test_the_old_source_policy_is_gone(name):
    assert LEGACY not in PROMPTS[name], name


def test_the_audit_checklist_is_no_longer_an_output_format():
    assert "已确认内容、缺失来源和未满足标准" not in prompts.WORKER
    assert "缺失来源" not in prompts.WORKER


def test_planner_teaches_granularity_including_when_not_to_split():
    body = prompts.PLANNER
    assert "别拆" in body or "不拆" in body
    assert "depends_on" in body
    # both directions must be demonstrated: one task, and several with dependencies
    assert body.count('"id":"t1"') >= 2
    assert 'depends_on":["t1","t2"]' in body


def test_planner_revision_keeps_the_previous_plan_visible():
    rendered = prompts.planner_revise('{"tasks": []}', "把两个取证任务拆开")
    assert '{"tasks": []}' in rendered and "把两个取证任务拆开" in rendered
    assert not re.search(r"\{.*\{.*\}", rendered.split("# 上一版计划")[0])
