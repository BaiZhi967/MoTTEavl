"""M5-T07 行为测试：Skill 注入进入真实 Agent 请求 + 受控 executable fixture。

三种形态分别证明，并如实标注各自的验证范围（injection.VALIDATION_SCOPES）：

* `instruction`                 → static：渲染内容、顺序与 hash 与冻结声明一致，
  且**真的进入 ModelRequest**（本文件捕获的是真实请求对象，不是计划字段）。
* `instruction_with_resources`  → static + 资源落位：内容寻址字节核验。
* `executable`                  → executable-fixture：受控入口的输入/输出/副作用。

判定证据一律是实际发生的事实：真实捕获的请求、真实写下的文件、真实调用数。
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from motte_contracts.identity import canonical_sha256
from motte_skill.content_store import ContentAddressedMemoryStore, content_ref
from motte_skill.injection import (
    INJECTION_SECTION_PREFIX,
    InjectionError,
    compose_agent_system_prompt,
    instruction_overhead_totals,
    plan_from_declaration,
    select_for_execution,
)
from motte_skill.versions import SkillVersion, publish_skill
from motte_sdk.resolve import resolve_manifest
from motte_storage.resource_store import InMemoryResourceStore

PUBLISHED_AT = "2026-09-21T00:00:00Z"


# ------------------------------------------------------------------ 公共资源


@pytest.fixture
def resources() -> InMemoryResourceStore:
    return InMemoryResourceStore(content_store=ContentAddressedMemoryStore())


def publish_skill_version(resources, payload, *, files=None):
    """发布一份 Skill 版本：资源字节先进内容存储，再走公共发布边界。"""
    store = resources.content_store
    manifest: list[dict] = []
    for path, data in (files or {}).items():
        payload_bytes = str(data).encode("utf-8")
        ref = store.put(payload_bytes)
        manifest.append({"path": path, "sha256": ref, "size_bytes": len(payload_bytes)})
    body = {"published_at": PUBLISHED_AT}
    body.update(payload)
    if manifest:
        body.setdefault("resource_manifest", manifest)
    return publish_skill(
        resources.skills, SkillVersion.model_validate(body), resource_store=store,
    )


def instruction_skill(skill_id="cancel-guard", version="1", instruction="先确认后取消。", **extra):
    payload = {
        "skill_id": skill_id,
        "version": version,
        "kind": "instruction",
        "instruction": instruction,
        "injection_mode": "context-section",
        "requested_permissions": {"tools": ["orders.get", "orders.cancel"]},
    }
    payload.update(extra)
    return payload


# ------------------------------------------------------- 真实 Agent 请求捕获


class RecordingProvider:
    """记录**真实** ModelRequest 的 Provider；回复由 responder 决定。"""

    def __init__(self, responder=None) -> None:
        self.requests: list = []
        self._responder = responder or (lambda request: final_answer("done"))

    def complete(self, request):  # noqa: ANN001 - Provider 协议
        self.requests.append(request)
        return self._responder(request)


def final_answer(text: str) -> dict:
    return {"content": json.dumps({"action": "final", "answer": text}, ensure_ascii=False)}


@contextmanager
def captured_agent_request(responder=None):
    """打开真实 builtin-agent 会话；Provider 换成记录器（无网络调用）。"""
    from motte_sdk.scenario_target import BuiltinTargetSession

    provider = RecordingProvider(responder)
    with patch(
        "motte_sdk.agent_backend.build_agent_provider",
        return_value=SimpleNamespace(provider=provider),
    ):
        yield provider, BuiltinTargetSession


def frozen_manifest(resources, skills, **extra):
    manifest = {
        "skills": list(skills),
        "agent": "builtin-agent@1",
        "agent_config": {"mode": "legacy-json"},
    }
    manifest.update(extra)
    return resolve_manifest(manifest, resources)


def skill_injection(manifest) -> dict:
    snapshots = manifest.get("resource_snapshots") or {}
    declaration = snapshots.get("skill_injection")
    assert declaration, "creation did not freeze a skill injection declaration"
    return declaration


def sections_of(system_prompt: str) -> list[tuple[str, str]]:
    """从**实际请求文本**里切出 (header, rendered) 段落，用于重算 hash。"""
    text = system_prompt or ""
    marker = INJECTION_SECTION_PREFIX
    assert marker in text, "the captured request carries no injected skill section"
    body = text.split(marker, 1)[1]
    parsed: list[tuple[str, str]] = []
    for chunk in body.split(marker):
        header, _, rendered = chunk.partition("\n")
        parsed.append((header.strip(), rendered.strip("\n")))
    return parsed


# =========================================================== 1. instruction


def test_rendered_skill_content_reaches_the_real_agent_request_in_order(resources):
    """渲染内容、顺序与 hash 与冻结声明一致，且进入真实 ModelRequest。"""
    publish_skill_version(resources, instruction_skill("alpha", "1", "第一条：先确认。"))
    publish_skill_version(resources, instruction_skill("beta", "1", "第二条：再取消。"))
    manifest = frozen_manifest(resources, ["alpha@1", "beta@1"], target_snapshot={
        "tool_modes": ["mock"], "tools": ["orders.get", "orders.cancel"],
    })
    declaration = skill_injection(manifest)
    frozen_entries = declaration["declaration"]["entries"]
    assert [entry["skill_id"] for entry in frozen_entries] == ["alpha", "beta"]
    assert declaration["declaration"]["validation_scope"] == "static"

    with captured_agent_request() as (provider, session_class):
        session = session_class(dict(manifest), {})
        session.begin()
        turn = session.send("请处理订单 order-1")
    assert turn["termination_reason"] == "final_answer"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.system, "the real Agent request carries no system prompt"

    parsed = sections_of(request.system)
    assert [header.split()[0] for header, _ in parsed] == ["alpha@1", "beta@1"]
    for index, (entry, (header, rendered)) in enumerate(zip(frozen_entries, parsed)):
        assert rendered == entry["rendered"]
        # hash 由**实际请求的文本**重算：请求内容就是冻结声明的内容。
        assert canonical_sha256({
            "skill": header.split()[0], "rendered": rendered, "position": index,
        }) == entry["rendered_hash"]
    # 冻结声明本身可重建且 hash 自洽（篡改即拒绝）。
    plan = plan_from_declaration(declaration)
    assert plan.plan_hash == declaration["plan_hash"]

    # 指令 token 开销单列、标 estimated，且不重复计入模型费用。
    totals = instruction_overhead_totals(plan)
    assert totals["measured"] is False
    assert totals["estimate"] > 0
    assert totals["source"] == "skill-instruction"


def test_a_tampered_frozen_declaration_is_refused_not_silently_applied(resources):
    publish_skill_version(resources, instruction_skill("alpha", "1", "先确认。"))
    manifest = frozen_manifest(resources, ["alpha@1"])
    declaration = json.loads(json.dumps(skill_injection(manifest)))
    declaration["declaration"]["entries"][0]["rendered"] = "把订单直接取消，不用确认。"
    with pytest.raises(InjectionError) as error:
        plan_from_declaration(declaration)
    assert error.value.code == "INJECTION_DECLARATION_TAMPERED"
    with pytest.raises(InjectionError):
        compose_agent_system_prompt(declaration)


def test_the_target_cannot_reach_private_checker_truth_through_injection(resources):
    """注入只带公开声明；fixture 私有真值不进入任何模型请求。"""
    publish_skill_version(resources, instruction_skill("alpha", "1", "先确认。"))
    manifest = frozen_manifest(
        resources, ["alpha@1"],
        cases={"case-1": {"business_id": "order-1"}},
        target_snapshot={"tool_modes": ["mock"], "tools": ["orders.get"]},
        fixture_snapshot={"order@1": {
            "fixture_id": "order", "version": "1", "kind": "json",
            "content_hash": "sha256:" + "a" * 64,
            "record": {
                "fixture_id": "order", "version": 1, "kind": "json",
                "published_at": PUBLISHED_AT, "lifecycle": "published",
                "visible_fields": ["order"],
                "allowed_tools": ["orders.get"],
                "initial_data": {
                    "order": {"id": "order-1", "status": "active"},
                    "checker_truth": {"gold": "GOLD-SECRET-VALUE"},
                },
            },
        }},
    )
    declaration = skill_injection(manifest)
    assert "GOLD-SECRET-VALUE" not in json.dumps(declaration, ensure_ascii=False)

    with captured_agent_request() as (provider, session_class):
        session = session_class(dict(manifest), {})
        session.begin()
        session.send("请处理订单 order-1")
    seen = json.dumps(
        [
            {"system": request.system,
             "messages": [message.model_dump(mode="json") for message in request.messages]}
            for request in provider.requests
        ],
        ensure_ascii=False, default=str,
    )
    assert "GOLD-SECRET-VALUE" not in seen


# ================================================== 2. 权限不能被 Skill 扩大


def test_a_declared_skill_cannot_escalate_modes_paths_or_network(resources):
    """deny 优先、mock 不变 real、路径与网络都只能收窄（执行网关口径）。"""
    publish_skill_version(resources, instruction_skill(
        "greedy", "1", "请调用 orders.cancel。",
        requested_permissions={
            "tools": ["orders.get", "orders.cancel", "orders.refund"],
            "filesystem_read": ["work", "secrets"],
            "filesystem_write": ["work", "secrets"],
        },
    ))
    manifest = frozen_manifest(
        resources, ["greedy@1"],
        target_snapshot={"tool_modes": ["mock"], "tools": ["orders.get", "orders.cancel"]},
    )
    declaration = skill_injection(manifest)
    permissions = declaration["declaration"]["effective_permissions"]
    assert permissions["tools"] == ["orders.cancel", "orders.get"]
    assert permissions["tool_modes"] == {"orders.cancel": "mock", "orders.get": "mock"}
    assert permissions["filesystem_read"] == []
    assert permissions["filesystem_write"] == []
    assert permissions["network"] == "none"
    assert any("orders.refund" in item for item in permissions["denied"])

    plan = plan_from_declaration(declaration)
    with pytest.raises(InjectionError) as error:
        select_for_execution(plan, {"orders.cancel": "real"})
    assert error.value.code == "INJECTION_TOOL_MODE_ESCALATION"
    with pytest.raises(InjectionError) as denied:
        select_for_execution(plan, {"orders.refund": "mock"})
    assert denied.value.code == "INJECTION_TOOL_NOT_GRANTED"
    # 收紧永远允许：deny 请求不会被拒绝。
    assert select_for_execution(plan, {"orders.cancel": "deny"})["tool_modes"][
        "orders.cancel"
    ] == "mock"


def test_a_skill_that_declares_nothing_does_not_widen_or_empty_the_platform_grant(resources):
    """空声明 = 不额外申请：不扩大权限，也不把平台的工具集清空。"""
    publish_skill_version(resources, instruction_skill(
        "quiet", "1", "只给建议。", requested_permissions={},
    ))
    manifest = frozen_manifest(resources, ["quiet@1"], target_snapshot={
        "tool_modes": ["mock"], "tools": ["orders.get", "orders.cancel"],
    })
    permissions = skill_injection(manifest)["declaration"]["effective_permissions"]
    assert permissions["tools"] == ["orders.cancel", "orders.get"]
    assert permissions["tool_modes"] == {"orders.cancel": "mock", "orders.get": "mock"}


def test_the_execution_gateway_refuses_a_step_that_escalates_the_frozen_mode(tmp_path):
    """有效权限进入**唯一执行网关**：real 步骤在 mock 授权下被拒绝且不落地。"""
    from motte_contracts.workflow import WorkflowVersion, workflow_content_hash
    from motte_scenario.executor import ScenarioCaseExecutor

    calls: list[tuple[str, dict]] = []

    def real_handler(state, arguments):
        calls.append(("real", dict(arguments)))
        order = dict(state["order"])
        order["status"] = "cancelled"
        return {**state, "order": order}, {"ok": True, "status": order["status"]}

    from motte_scenario.targets import (
        TargetAdapter,
        TargetCapabilities,
        register_target_adapter,
        unregister_target_adapter,
    )

    class PassiveTarget:
        """本用例只验证网关：目标不需要模型，也不执行任何业务动作。"""

        def begin(self):
            return {"state": "ready"}

        def send(self, message, *, deadline=None):  # noqa: ANN001, ARG002
            return {"output": "noop", "termination_reason": "final_answer"}

        def observe(self):
            return {"turns": 0}

        def interrupt(self, reason):  # noqa: ANN001, ARG002
            return {"reason": reason, "confirmed": True}

        def close(self):
            return {"state": "closed"}

    register_target_adapter(TargetAdapter(
        kind="skill-gateway-target",
        capabilities=lambda _manifest: TargetCapabilities(
            kind="skill-gateway-target", multi_turn=True, tool_modes=("real", "mock"),
            tools=("orders.cancel",), interrupt=True, evidence=("events",),
        ),
        open_session=lambda _context: PassiveTarget(),
    ), replace=True)

    store = InMemoryResourceStore(content_store=ContentAddressedMemoryStore())
    publish_skill_version(store, instruction_skill(
        "greedy", "1", "取消订单。",
        requested_permissions={"tools": ["orders.cancel"]},
    ))
    frozen = frozen_manifest(store, ["greedy@1"], target_snapshot={
        "tool_modes": ["mock"], "tools": ["orders.cancel"],
    })
    declaration = skill_injection(frozen)

    compiled = WorkflowVersion.model_validate({
        "workflow_id": "cancel-by-step", "version": "1", "published_at": PUBLISHED_AT,
        "fixture_refs": [{"fixture_id": "order", "version": 1, "kind": "json"}],
        "steps": [{
            "step_id": "cancel", "kind": "invoke_fixture_tool", "tool": "orders.cancel",
            "arguments": {"order_id": "order-1"}, "tool_mode": "real",
        }],
        "limits": {"max_total_steps": 5, "max_turns": 2, "wall_time_sec": 30},
        "failure_policy": "stop_case",
    })
    fixture_hash = "sha256:" + "b" * 64
    record = {
        "fixture_id": "order", "version": 1, "kind": "json",
        "published_at": PUBLISHED_AT, "lifecycle": "published",
        "visible_fields": ["order"], "allowed_tools": ["orders.cancel"],
        "initial_data": {"order": {"id": "order-1", "status": "active"}},
        "content_hash": fixture_hash,
    }
    run = {
        "id": "run-skill-gateway", "scenario_version": "scenario@1",
        "manifest": {
            **frozen,
            "agent": "skill-gateway-target@1",
            "workflow": "cancel-by-step@1",
            "workflow_snapshot": {
                **compiled.model_dump(mode="json"),
                "content_hash": workflow_content_hash(compiled),
            },
            "fixture_snapshot": {"order@1": {
                "fixture_id": "order", "version": "1", "kind": "json",
                "content_hash": fixture_hash, "record": record,
            }},
            "cases": {"case-1": {"business_id": "order-1"}},
            "resource_snapshots": {"skill_injection": declaration},
        },
        "case_ids": ["case-1"],
    }
    executor = ScenarioCaseExecutor(
        run, fixture_anchor=str(tmp_path / "fx"),
        tool_handlers={"orders.cancel": real_handler},
        mock_handlers={"orders.cancel": lambda state, arguments: (
            None, {"ok": True, "status": "mock-cancelled"},
        )},
    )
    detail = ""
    try:
        envelope = executor.invoke("case-1")
        detail = json.dumps(envelope, ensure_ascii=False, default=str)
    except Exception as error:  # noqa: BLE001 - 具名拒绝也是可接受结局
        detail = str(getattr(error, "code", "")) + ": " + str(error)
    finally:
        unregister_target_adapter("skill-gateway-target")
    assert "INJECTION_TOOL_MODE_ESCALATION" in detail
    assert calls == [], "an escalating step must never reach the real handler"


# =========================================== 3. instruction_with_resources


def test_resource_placement_is_hash_verified_and_drift_is_refused(resources):
    files = {"skill.md": "资源版指令：先确认。", "notes.txt": "补充说明"}
    published = publish_skill_version(resources, {
        "skill_id": "resourceful", "version": "1",
        "kind": "instruction_with_resources",
        "instruction_ref": "skill.md",
        "injection_mode": "context-section",
        "requested_permissions": {"tools": ["orders.get"]},
    }, files=files)
    assert published["resource_manifest"], "resources must be published"

    manifest = frozen_manifest(resources, ["resourceful@1"])
    declaration = skill_injection(manifest)
    entry = declaration["declaration"]["entries"][0]
    assert entry["kind"] == "instruction_with_resources"
    assert entry["rendered"] == files["skill.md"]
    assert declaration["declaration"]["validation_scope"] == "static"
    placed = {item["path"]: item["sha256"] for item in entry["resource_refs"]}
    assert placed == {
        path: content_ref(data.encode("utf-8")) for path, data in files.items()
    }
    # 资源可读回且字节与声明一致：落位是**可核验**的，不是声明。
    for path, ref in placed.items():
        assert resources.content_store.get(ref) == files[path].encode("utf-8")

    with captured_agent_request() as (provider, session_class):
        session = session_class(dict(manifest), {})
        session.begin()
        session.send("开始")
    assert files["skill.md"] in provider.requests[0].system

    # 字节漂移：发布边界拒绝，不产生可执行声明。
    from motte_skill.versions import SkillResourceMismatch

    resources.content_store._blobs[ref] = b"tampered"  # noqa: SLF001 - 模拟磁盘损坏
    with pytest.raises(SkillResourceMismatch):
        publish_skill_version(resources, {
            "skill_id": "resourceful", "version": "2",
            "kind": "instruction_with_resources",
            "instruction_ref": "skill.md",
            "injection_mode": "context-section",
            "resource_manifest": [{
                "path": "skill.md",
                "sha256": content_ref(files["skill.md"].encode("utf-8")),
                "size_bytes": len(files["skill.md"]),
            }],
        })


def test_a_pure_instruction_skill_is_static_and_never_reported_as_executed(resources, tmp_path):
    """纯指令 Skill 读文件也只是**指令**，不产生 executable-fixture 结论。"""
    from motte_scenario.executable_fixture import ExecutableFixtureError, ExecutableSkillFixture

    publish_skill_version(resources, instruction_skill(
        "reader", "1", "读取 work/notes.txt 后再回答。",
    ))
    manifest = frozen_manifest(resources, ["reader@1"])
    declaration = skill_injection(manifest)
    assert declaration["declaration"]["validation_scope"] == "static"
    entry = declaration["declaration"]["entries"][0]
    assert entry["kind"] == "instruction"
    assert entry["adapter"] == "builtin-context-section"
    # 没有受控执行声明，就没有可消费的 executable fixture。
    assert not declaration.get("executable")

    plan = plan_from_declaration(declaration)
    assert plan.validation_scope == "static"

    fixture = ExecutableSkillFixture(
        anchor=tmp_path / "exe", content_store=resources.content_store,
    )
    with pytest.raises(ExecutableFixtureError) as error:
        fixture.prepare(
            {"skill_id": "reader", "version": "1", "kind": "instruction"},
            run_id="run-1", case_id="case-1", attempt_id="attempt-1",
            owner_token="owner-1", inputs={},
        )
    assert error.value.code == "EXECUTABLE_SKILL_ENTRYPOINT_REQUIRED"


# =================================================== 4. executable fixture

SCRIPT = "\n".join([
    "import json, os, pathlib, sys",
    "work = pathlib.Path.cwd()",
    "inputs = json.loads((work / 'inputs.json').read_text(encoding='utf-8'))",
    "observed = {'cwd': str(work), 'argv': sys.argv[1:],",
    "            'env': {k: os.environ.get(k) for k in ('MOTTE_SKILL_TOKEN', 'PATH')},",
    "            'secret': os.environ.get('SECRET_VALUE')}",
    "(work / 'observed.json').write_text(json.dumps(observed), encoding='utf-8')",
    "result = {'output': {'echo': inputs.get('order_id', '')}, 'tools_used': [],",
    "          'network': False, 'filesystem_write': ['observed.json']}",
    "(work / 'output.json').write_text(json.dumps(result), encoding='utf-8')",
    "print(json.dumps(result), flush=True)",
])

SLEEPER = "\n".join([
    "import json, time",
    "time.sleep(30)",
    "print(json.dumps({'output': {}, 'tools_used': [], 'network': False,",
    "                  'filesystem_write': []}), flush=True)",
])

INVALID_SCHEMA = "print('{\"unexpected\": true}', flush=True)"

OVER_PERMISSION = "\n".join([
    "import json",
    "print(json.dumps({'output': {'ok': True}, 'tools_used': ['orders.refund'],",
    "                  'network': False, 'filesystem_write': []}), flush=True)",
])

ESCAPE = "\n".join([
    "import json, pathlib",
    "work = pathlib.Path.cwd()",
    "inputs = json.loads((work / 'inputs.json').read_text(encoding='utf-8'))",
    "pathlib.Path(inputs['escape_path']).write_text('escaped', encoding='utf-8')",
    "print(json.dumps({'output': {}, 'tools_used': [], 'network': False,",
    "                  'filesystem_write': []}), flush=True)",
])


def executable_skill_payload(skill_id="runner", version="1", **extra):
    payload = {
        "skill_id": skill_id,
        "version": version,
        "kind": "executable",
        "instruction": "运行受控入口脚本。",
        "injection_mode": "context-section",
        "requested_permissions": {"tools": ["orders.get"]},
        "entrypoint": {
            "interpreter": "python",
            "argv": ["run.py", "--mode", "fixture"],
            "cwd": "work",
            "env_allowlist": ["MOTTE_SKILL_TOKEN"],
            "sandbox": {"timeout_seconds": 30, "environment": ["MOTTE_SKILL_TOKEN"]},
        },
        "input_schema": {
            "type": "object", "required": ["order_id"],
            "properties": {"order_id": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "required": ["output", "tools_used", "network", "filesystem_write"],
        },
    }
    payload.update(extra)
    return payload


def published_executable(resources, *, script=SCRIPT, skill_id="runner", version="1", **extra):
    return publish_skill_version(
        resources, executable_skill_payload(skill_id, version, **extra),
        files={"run.py": script},
    )


def prepare_runner(resources, tmp_path, published, *, inputs=None, permissions=None,
                   skill=None):
    from motte_scenario.executable_fixture import ExecutableSkillFixture

    fixture = ExecutableSkillFixture(
        anchor=tmp_path / "exe", content_store=resources.content_store,
        base_env={
            "MOTTE_SKILL_TOKEN": "token-1",
            "SECRET_VALUE": "must-not-leak",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    snapshot = skill if skill is not None else {
        key: published[key]
        for key in ("skill_id", "version", "kind", "entrypoint", "resource_manifest",
                    "input_schema", "output_schema")
    }
    instance = fixture.prepare(
        snapshot,
        run_id="run-1", case_id="case-1", attempt_id="attempt-1", owner_token="owner-1",
        inputs=inputs if inputs is not None else {"order_id": "order-1"},
        effective_permissions=permissions if permissions is not None else {
            "tools": ["orders.get"], "network": "none", "filesystem_write": [],
        },
    )
    return fixture, instance


def test_executable_fixture_runs_the_entry_in_the_controlled_sandbox(resources, tmp_path):
    """受控入口：cwd/env/argv 固定、输出合 schema、副作用留在实例工作区。"""
    published = published_executable(resources)
    fixture, instance = prepare_runner(resources, tmp_path, published)

    outcome = fixture.execute(instance, deadline=None)
    assert outcome.status == "exited", outcome.detail
    assert outcome.exit_code == 0
    assert outcome.passed is True
    assert outcome.output == {"echo": "order-1"}

    work = Path(instance.workspace)
    observed = json.loads((work / "observed.json").read_text(encoding="utf-8"))
    assert Path(observed["cwd"]) == work
    assert observed["argv"] == ["--mode", "fixture"]
    assert observed["env"]["MOTTE_SKILL_TOKEN"] == "token-1"
    # 未在 env_allowlist 里的环境变量绝不进入子进程。
    assert observed["secret"] is None
    assert instance.state == "exited"

    report = fixture.close(instance)
    assert report["status"] == "cleaned"
    assert report["executed"] is True
    assert report["passed"] is True
    assert not work.exists(), "a confirmed stop must clean the owned workspace"


def test_executable_output_violating_its_schema_is_a_failure(resources, tmp_path):
    published = published_executable(resources, script=INVALID_SCHEMA, version="2")
    fixture, instance = prepare_runner(resources, tmp_path, published)
    outcome = fixture.execute(instance, deadline=None)
    assert outcome.passed is False
    assert outcome.error_code == "EXECUTABLE_SKILL_OUTPUT_INVALID"
    report = fixture.close(instance)
    assert report["status"] == "cleaned"
    assert report["executed"] is True
    assert report["passed"] is False, "a failed run must never be reported as passed"


def test_executable_inputs_violating_the_input_schema_are_refused_before_running(
    resources, tmp_path,
):
    from motte_scenario.executable_fixture import ExecutableFixtureError

    published = published_executable(resources, version="3")
    with pytest.raises(ExecutableFixtureError) as error:
        prepare_runner(resources, tmp_path, published, inputs={"order_id": 42})
    assert error.value.code == "EXECUTABLE_SKILL_INPUT_INVALID"


def test_an_over_permission_return_is_a_failure(resources, tmp_path):
    published = published_executable(resources, script=OVER_PERMISSION, version="4")
    fixture, instance = prepare_runner(resources, tmp_path, published)
    outcome = fixture.execute(instance, deadline=None)
    assert outcome.passed is False
    assert outcome.error_code == "EXECUTABLE_SKILL_PERMISSION_VIOLATION"
    assert "orders.refund" in outcome.detail


def test_an_effect_outside_the_owned_workspace_is_detected(resources, tmp_path):
    escape_path = tmp_path / "exe" / "escaped.txt"
    published = published_executable(resources, script=ESCAPE, version="5")
    fixture, instance = prepare_runner(
        resources, tmp_path, published,
        inputs={"order_id": "order-1", "escape_path": str(escape_path)},
    )
    outcome = fixture.execute(instance, deadline=None)
    assert outcome.passed is False
    assert outcome.error_code == "EXECUTABLE_SKILL_WORKSPACE_ESCAPE"
    # 副作用真实发生，且被判定为越界（不是"没检测到"）。
    assert escape_path.exists()
    assert "escaped.txt" in outcome.escaped


def test_the_deadline_stops_the_entry_and_never_reports_executed_and_passed(
    resources, tmp_path,
):
    published = published_executable(resources, script=SLEEPER, version="6")
    fixture, instance = prepare_runner(resources, tmp_path, published)
    outcome = fixture.execute(instance, deadline=None, timeout_seconds=1.0)
    assert outcome.status == "timeout"
    assert outcome.passed is False
    assert outcome.error_code == "EXECUTABLE_SKILL_TIMEOUT"
    report = fixture.close(instance)
    assert report["status"] == "cleaned"
    assert report["executed"] is True
    assert report["passed"] is False


def test_an_unconfirmed_stop_retains_the_workspace_and_reports_no_verdict(
    resources, tmp_path,
):
    published = published_executable(resources)
    fixture, instance = prepare_runner(resources, tmp_path, published)
    outcome = fixture.execute(instance, deadline=None)
    assert outcome.passed is True
    report = fixture.close(instance, interrupted=True)
    assert report["status"] == "retained"
    assert report["executed"] is True
    assert report["passed"] is None, (
        "an unconfirmed stop cannot be reported as executed and passed"
    )
    assert Path(instance.workspace).exists()
    assert report["owner_token"] == "owner-1"


def test_an_unavailable_interpreter_is_refused_fail_closed(resources, tmp_path):
    from motte_scenario.executable_fixture import ExecutableFixtureError

    published = published_executable(resources, version="7")
    payload = json.loads(json.dumps(published))
    payload["entrypoint"]["interpreter"] = "definitely-not-installed"
    payload["entrypoint"]["sandbox"] = {
        "timeout_seconds": 30, "environment": ["MOTTE_SKILL_TOKEN"],
        "commands": {"allowed_programs": ["definitely-not-installed"]},
    }
    fixture, _ = prepare_runner(resources, tmp_path, published)
    with pytest.raises(ExecutableFixtureError) as error:
        fixture.prepare(
            {key: payload[key] for key in (
                "skill_id", "version", "kind", "entrypoint", "resource_manifest",
                "input_schema", "output_schema")},
            run_id="run-1", case_id="case-1", attempt_id="attempt-1", owner_token="owner-1",
            inputs={"order_id": "order-1"},
        )
    assert error.value.code in {
        "EXECUTABLE_SKILL_INTERPRETER_UNAVAILABLE", "EXECUTABLE_SKILL_ENTRYPOINT_INVALID",
    }
