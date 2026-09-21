"""M5-T07 注入与权限交集反例。

证明的是**权限不能被 Skill 扩大**、**顺序是身份**、**验证范围标签诚实**，
而不是字段存在。全部使用真实契约对象（SkillVersion）。
"""
from __future__ import annotations

import pytest

from motte_skill.injection import (
    InjectionError,
    compile_injection,
    injection_digest,
    intersect_permissions,
    select_for_execution,
)
from motte_skill.versions import SkillResource, SkillVersion, verify_resources

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def skill(skill_id="cancel-helper", version="1", **overrides):
    payload = {
        "skill_id": skill_id,
        "version": version,
        "kind": "instruction",
        "instruction": "取消订单前必须先取得用户确认。",
        "published_at": PUBLISHED_AT,
        "injection_mode": "context-section",
    }
    payload.update(overrides)
    return SkillVersion.model_validate(payload)


PLATFORM = {
    "tools": ["orders.get", "orders.cancel"],
    "tool_modes": {"orders.get": "mock", "orders.cancel": "mock"},
    "filesystem_read": ["/work"],
    "filesystem_write": ["/work/out"],
    "network": "none",
    "deny_tools": [],
}
SCENARIO = {"tools": ["orders.get", "orders.cancel"], "network": "none"}
TARGET = {"tools": ["orders.get", "orders.cancel"]}


def policies(skill_permissions=None):
    return {
        "platform": dict(PLATFORM),
        "scenario": dict(SCENARIO),
        "target": dict(TARGET),
        "skill": dict(skill_permissions or {}),
    }


def test_order_of_skills_is_part_of_the_identity():
    first = skill("alpha", instruction="A")
    second = skill("beta", instruction="B")
    forward = compile_injection(selected=[first, second], policies=policies())
    backward = compile_injection(selected=[second, first], policies=policies())
    assert forward.plan_hash != backward.plan_hash
    assert injection_digest(forward) != injection_digest(backward)
    assert [entry.skill_id for entry in forward.entries] == ["alpha", "beta"]
    assert forward.entries[0].rendered_hash != backward.entries[0].rendered_hash


def test_platform_deny_cannot_be_overridden_by_a_skill():
    platform = {**PLATFORM, "deny_tools": ["orders.cancel"]}
    plan = compile_injection(
        selected=[skill(requested_permissions={"tools": ["orders.get", "orders.cancel"]})],
        policies={**policies(), "platform": platform},
    )
    assert plan.effective_permissions.tools == ("orders.get",)
    assert any("tool:orders.cancel" in item for item in plan.effective_permissions.denied)
    with pytest.raises(InjectionError, match="not granted"):
        select_for_execution(plan, {"orders.cancel": "real"})


def test_a_skill_cannot_promote_a_mock_tool_to_real():
    platform = {**PLATFORM, "tool_modes": {"orders.get": "mock", "orders.cancel": "mock"},
                "default_tool_mode": "mock"}
    plan = compile_injection(
        selected=[skill(requested_permissions={"tools": ["orders.get", "orders.cancel"]})],
        policies={**policies(), "platform": platform},
    )
    assert plan.effective_permissions.tool_modes == {
        "orders.get": "mock", "orders.cancel": "mock",
    }
    with pytest.raises(InjectionError) as error:
        select_for_execution(plan, {"orders.cancel": "real"})
    assert error.value.code == "INJECTION_TOOL_MODE_ESCALATION"
    # 收紧是允许的：平台要求 mock，执行请求 deny 也通过。
    assert select_for_execution(plan, {"orders.cancel": "deny"})["tool_modes"][
        "orders.cancel"
    ] == "mock"


def test_a_skill_cannot_widen_paths_or_open_network():
    from pydantic import ValidationError

    # 第一道防线在契约层：绝对路径与开放网络根本不能被"申请"。
    with pytest.raises(ValidationError, match="must be relative"):
        skill(requested_permissions={"filesystem_read": ["/etc"]})
    with pytest.raises(ValidationError):
        skill(requested_permissions={"network": "allowed"})

    # 第二道防线在交集层：相对路径也只能被收窄，绝不能扩大。
    plan = compile_injection(
        selected=[skill(requested_permissions={"filesystem_read": ["work", "secrets"]})],
        policies={
            "platform": {"tools": ["orders.get"], "filesystem_read": ["work"]},
            "scenario": {"tools": ["orders.get"], "filesystem_read": ["work", "secrets"]},
            "target": {"tools": ["orders.get"], "filesystem_read": ["work", "secrets"]},
            "skill": {"filesystem_read": ["work", "secrets"]},
        },
    )
    assert plan.effective_permissions.filesystem_read == ("work",)
    assert plan.effective_permissions.network == "none"


def test_permission_intersection_needs_every_source_to_agree():
    plan = compile_injection(
        selected=[skill()],
        policies={
            "platform": {"tools": ["orders.get", "orders.cancel"]},
            "scenario": {"tools": ["orders.get"]},
            "target": {"tools": ["orders.cancel"]},
        },
    )
    assert plan.effective_permissions.tools == ()


@pytest.mark.parametrize("policies_kwargs", [{"nonsense": {}}])
def test_unknown_permission_sources_are_refused(policies_kwargs):
    with pytest.raises(InjectionError, match="unknown permission sources"):
        intersect_permissions(policies_kwargs)


def test_validation_scope_is_labelled_honestly():
    instruction = compile_injection(selected=[skill()], policies=policies())
    assert instruction.validation_scope == "static"
    assert instruction.observability == "complete"

    executable = skill(
        kind="executable",
        instruction="运行受控入口",
        entrypoint={"interpreter": "python3", "argv": ["run.py"], "cwd": "."},
        input_schema={"type": "object"}, output_schema={"type": "object"},
        resource_manifest=[{"path": "run.py", "sha256": "sha256:" + "d" * 64,
                            "size_bytes": 12}],
    )
    plan = compile_injection(selected=[executable], policies=policies())
    assert plan.validation_scope == "executable-fixture"

    behaviour = compile_injection(
        selected=[skill()], policies=policies(), behaviour_tested=True,
    )
    assert behaviour.validation_scope == "agent-behaviour"


def test_native_loader_injection_is_only_partially_observable():
    plan = compile_injection(
        selected=[skill(injection_mode="native-loader")], policies=policies(),
    )
    assert plan.observability == "partial"
    assert plan.entries[0].adapter == "platform-native-loader"


def test_mixed_injection_modes_are_reported_as_a_conflict():
    plan = compile_injection(
        selected=[skill("alpha", injection_mode="context-section"),
                  skill("beta", injection_mode="native-loader")],
        policies=policies(),
    )
    assert any("INJECTION_MODE_MIXED" in item for item in plan.conflicts)


def test_unresolved_rendering_is_refused_not_silently_blank():
    with pytest.raises(InjectionError) as error:
        compile_injection(
            selected=[skill(instruction="用 {{missing_key}} 取消")], policies=policies(),
        )
    assert error.value.code == "INJECTION_UNRESOLVED_PLACEHOLDER"
    rendered = compile_injection(
        selected=[skill(instruction="用 {{order_id}} 取消")], policies=policies(),
        render_context={"order_id": "order-1"},
    )
    assert rendered.entries[0].rendered == "用 order-1 取消"
    assert "{{" not in rendered.entries[0].rendered


def test_instruction_ref_without_resolved_bytes_is_refused():
    from pydantic import ValidationError

    # 未声明的 instruction_ref 在契约层就被拒绝；声明了但没解析出字节时，
    # 注入编译具名拒绝，绝不静默注入空指令。
    with pytest.raises(ValidationError, match="declared resource"):
        skill(instruction=None, instruction_ref="skill.md")
    declared = skill(
        kind="instruction_with_resources",
        instruction=None,
        instruction_ref="skill.md",
        resource_manifest=[{"path": "skill.md", "sha256": "sha256:" + "c" * 64,
                            "size_bytes": 10}],
    )
    with pytest.raises(InjectionError, match="without resolved bytes"):
        compile_injection(selected=[declared], policies=policies())


def test_instruction_token_overhead_is_marked_estimated_and_not_double_counted():
    plan = compile_injection(selected=[skill()], policies=policies())
    overhead = plan.entries[0].instruction_tokens
    assert overhead["measured"] is False
    assert overhead["method"]
    assert overhead["estimate"] > 0
    assert overhead["source"] == "skill-instruction"


def test_resource_hash_drift_is_refused_by_the_publisher_contract():
    import hashlib

    payload = b"abcd"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    class Store:
        def __init__(self, content):
            self.content = content

        def get(self, ref):
            return self.content

    entries = [SkillResource(path="skill.md", sha256=digest, size_bytes=len(payload))]
    verified = verify_resources(Store(payload), entries)
    assert verified[0].sha256 == digest
    # 字节漂移（内容不同或长度不同）都必须拒绝，绝不"按 hash 声明"放行。
    with pytest.raises(Exception):
        verify_resources(Store(b"entirely different bytes"), entries)
    with pytest.raises(Exception):
        verify_resources(Store(b"ab"), entries)


def test_plan_is_serialisable_and_records_no_raw_secret():
    plan = compile_injection(selected=[skill()], policies=policies())
    payload = plan.as_dict()
    assert payload["plan_hash"] == plan.plan_hash
    assert "sk-" not in str(payload)
