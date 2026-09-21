"""T05 装配层：把一个 Workflow 接进既有 CaseAttempt / Observation 链路。

一个 CaseAttempt 装配一个 FixtureInstance + 一个 TargetSession + 一个引擎
实例；流程步骤不新建调度器，也不产生伪 Trial。事件、调用日志、取消探测与
Artifact 全部走既有 RunService / ArtifactStore 通道。

真值隔离：目标只能看到 fixture 的业务可见投影（visible_state /
visible_tool_result），checker 真值、gold 与隐藏断言留在 PrivateTruth 的私有根，
永不进入事件、产物或工具结果。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4

from motte_contracts.fixture import FixtureSpec, is_hidden_field
from motte_contracts.workflow import workflow_content_hash

from .compiler import compile_workflow
from .engine import WorkflowEngine, WorkflowOutcome
from .fixtures import CleanupReport, FixtureRuntime
from .state import write_json_state
from .targets import target_adapter


class ScenarioToolError(RuntimeError):
    """fixture 工具无法执行；code 供结构化错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FixtureBinding:
    """一个已装配的 fixture：实例、所有者令牌与可见投影入口。"""

    key: str
    spec: FixtureSpec
    instance: Any
    owner_token: str
    cleanup: CleanupReport | None = None


class FixturePortAdapter:
    """把 FixtureRuntime 投影成引擎需要的 FixturePort。

    工具与事件都必须显式实现：没有实现的工具/事件具名拒绝，绝不静默返回
    成功，也绝不把 checker 真值当作业务数据返回。
    """

    def __init__(
        self,
        runtime: FixtureRuntime,
        binding: FixtureBinding,
        *,
        tools: Mapping[str, Callable[..., Any]] | None = None,
        events: Mapping[str, Callable[..., Any]] | None = None,
        tool_log: list[dict[str, Any]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.binding = binding
        self.tools = dict(tools or {})
        self.events = dict(events or {})
        self.tool_log = tool_log if tool_log is not None else []

    # ------------------------------------------------------------ 引擎接口

    @property
    def state(self) -> Mapping[str, Any]:
        return self.runtime.visible_state(
            self.binding.instance, owner_token=self.binding.owner_token,
        )

    def snapshot(self) -> tuple[dict[str, Any], str]:
        snapshot = self.runtime.snapshot(
            self.binding.instance, owner_token=self.binding.owner_token,
        )
        state = self.runtime.visible_state(
            self.binding.instance, owner_token=self.binding.owner_token,
        )
        return state, snapshot.content_hash

    def invoke_tool(self, name: str, arguments: Mapping[str, Any], mode: str) -> Any:
        if mode == "deny":
            self._log(name, mode, "denied", None)
            raise ScenarioToolError("SCENARIO_TOOL_DENIED", f"tool denied by policy: {name}")
        # allowed_tools 校验由 fixture 负责（tool_not_allowed）。
        self.runtime.visible_tool_result(
            self.binding.instance, owner_token=self.binding.owner_token, tool=name,
        )
        handler = self.tools.get(name)
        if handler is None:
            self._log(name, mode, "not_implemented", None)
            raise ScenarioToolError(
                "SCENARIO_TOOL_NOT_IMPLEMENTED",
                f"fixture tool {name!r} has no implementation in this execution environment",
            )
        visible_state = dict(self.state)
        outcome = handler(visible_state, dict(arguments))
        new_state, result = self._split_outcome(outcome)
        if new_state is not None:
            self._write_state(new_state)
        projected = self._project(name, result)
        self._log(name, mode, "succeeded", projected)
        return projected

    def apply_event(self, event: str, payload: Mapping[str, Any]) -> Any:
        handler = self.events.get(event)
        if handler is None:
            raise ScenarioToolError(
                "SCENARIO_EVENT_NOT_IMPLEMENTED",
                f"fixture event {event!r} has no implementation in this execution environment",
            )
        outcome = handler(dict(self.state), dict(payload))
        new_state, result = self._split_outcome(outcome)
        if new_state is not None:
            self._write_state(new_state)
        return self._project(event, result)

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _split_outcome(outcome: Any) -> tuple[dict[str, Any] | None, Any]:
        """handler 返回 (新状态, 结果) 时写回状态；只返回结果时状态不变。"""
        if isinstance(outcome, tuple) and len(outcome) == 2:
            new_state, result = outcome
            if new_state is not None and not isinstance(new_state, Mapping):
                raise ScenarioToolError(
                    "SCENARIO_TOOL_RESULT_INVALID", "handler returned a non-mapping state"
                )
            return (dict(new_state) if new_state is not None else None), result
        return None, outcome

    def _write_state(self, new_state: dict[str, Any]) -> None:
        if self.binding.spec.kind != "json":
            raise ScenarioToolError(
                "SCENARIO_TOOL_STATE_UNSUPPORTED",
                f"{self.binding.spec.kind} fixtures do not accept in-process state writes",
            )
        write_json_state(self.binding.instance.state_path, new_state)

    def _project(self, name: str, result: Any) -> Any:
        """工具结果只带最小业务可见字段：隐藏键一律剔除。"""
        if isinstance(result, Mapping):
            projected = {
                str(key): value for key, value in result.items()
                if not is_hidden_field(key)
            }
            if not projected:
                raise ScenarioToolError(
                    "SCENARIO_TOOL_RESULT_EMPTY",
                    f"fixture tool {name!r} returned no business-visible field",
                )
            return projected
        return result

    def _log(self, name: str, mode: str, status: str, result: Any) -> None:
        self.tool_log.append({
            "tool": name, "mode": mode, "status": status,
            "result": result if isinstance(result, (str, int, float, bool, type(None))) else None,
            "fixture": self.binding.key,
        })


@dataclass
class ScenarioCaseResult:
    workflow_ref: str
    outcome: WorkflowOutcome
    observation: dict[str, Any]
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cleanups: list[dict[str, Any]] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    target_kind: str = ""

    def envelope(self) -> dict[str, Any]:
        return {
            "scenario": {
                "workflow_ref": self.workflow_ref,
                "target_kind": self.target_kind,
                "status": self.outcome.status,
                "reason": self.outcome.reason,
                "turns": self.outcome.turns,
                "step_count": self.outcome.step_count,
                "tool_calls": self.outcome.tool_calls,
                "needs_review": self.outcome.needs_review,
                "interrupt": self.outcome.interrupt,
            },
            "observation": self.observation,
            "artifacts_captured": len(self.artifacts),
            "tool_log": self.tool_log,
            "cleanup": self.cleanups,
        }


class ScenarioCaseExecutor:
    """一个 Run 的场景执行器；每个 Case 独立装配，互不共享状态。"""

    def __init__(
        self,
        run: dict[str, Any],
        *,
        fixture_anchor: str | None = None,
        tool_handlers: Mapping[str, Callable[..., Any]] | None = None,
        event_handlers: Mapping[str, Callable[..., Any]] | None = None,
        service: Any = None,
    ) -> None:
        import os

        self.run = run
        self.manifest = run.get("manifest") or {}
        self.fixture_anchor = fixture_anchor or os.environ.get(
            "MOTTE_SCENARIO_FIXTURE_ROOT", "var/scenario-fixtures",
        )
        self.tool_handlers = dict(tool_handlers or {})
        self.event_handlers = dict(event_handlers or {})
        self._service = service

    def bind_service(self, service: Any) -> None:
        self._service = service

    # ------------------------------------------------------------ 公共入口

    def invoke(self, case_id: str) -> dict[str, Any]:
        compiled = compile_workflow(self.manifest["workflow_snapshot"])
        attempt_id = f"{self.run.get('id')}:{case_id}:1"
        owner_token = uuid4().hex
        runtime = FixtureRuntime(anchor=self.fixture_anchor)
        bindings: list[FixtureBinding] = []
        tool_log: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        try:
            bindings = self._prepare_fixtures(runtime, case_id, attempt_id, owner_token)
            primary = bindings[0]
            port = FixturePortAdapter(
                runtime, primary, tools=self.tool_handlers,
                events=self.event_handlers, tool_log=tool_log,
            )
            target = self._open_target(port)
            engine = WorkflowEngine(
                compiled, port, target,
                event_sink=lambda event: self._emit(events, case_id, event),
                cancel_check=self._cancel_check(),
                run_id=str(self.run.get("id")),
                case_id=case_id,
                attempt_id=attempt_id,
            )
            outcome = engine.run()
            observation, artifacts = self._freeze(compiled, case_id, outcome)
        finally:
            cleanups = [
                self._cleanup(runtime, binding) for binding in bindings
            ]
        result = ScenarioCaseResult(
            workflow_ref=compiled.ref,
            outcome=outcome,
            observation=observation,
            artifacts=artifacts,
            cleanups=cleanups,
            tool_log=tool_log,
            target_kind=self._target_kind(),
        )
        return result.envelope()

    # ------------------------------------------------------------ 装配

    def _target_kind(self) -> str:
        from .target_identity import target_kind_of

        return target_kind_of(self.manifest)

    def _open_target(self, port: FixturePortAdapter) -> Any:
        """目标拿到的是**受控工具桥**：每一次调用都经过 fixture 的权限与投影。"""
        kind = self._target_kind()
        adapter = target_adapter(kind)

        def bridge(name: str) -> Any:
            return lambda arguments: port.invoke_tool(name, arguments, "real")

        return adapter.open_session({
            "manifest": self.manifest,
            "run": self.run,
            "tools": {name: bridge(name) for name in sorted(port.tools)},
        })

    def _prepare_fixtures(
        self, runtime: FixtureRuntime, case_id: str, attempt_id: str, owner_token: str,
    ) -> list[FixtureBinding]:
        snapshots = self.manifest.get("fixture_snapshot") or {}
        if not snapshots:
            raise ScenarioToolError(
                "SCENARIO_FIXTURE_REQUIRED", "scenario runs require pinned fixture snapshots"
            )
        bindings: list[FixtureBinding] = []
        for key, record in sorted(snapshots.items()):
            spec = FixtureSpec.model_validate(record["record"])
            instance = runtime.prepare(
                spec,
                run_id=str(self.run.get("id")),
                case_id=case_id,
                attempt_id=attempt_id,
                owner_token=owner_token,
                business_id=self._business_id(case_id, spec),
            )
            bindings.append(FixtureBinding(
                key=key, spec=spec, instance=instance, owner_token=owner_token,
            ))
        return bindings

    def _business_id(self, case_id: str, spec: FixtureSpec) -> str | None:
        """业务 ID 只作为 fixture 内部标签；实例根已按 run/case/attempt 隔离，
        因此两个 Case 用同一业务 ID 也不会共享状态。"""
        cases = self.manifest.get("cases") or {}
        case = cases.get(case_id) if isinstance(cases, dict) else None
        if isinstance(case, Mapping):
            business = case.get("business_id")
            if isinstance(business, str) and business:
                return business
        return spec.fixture_id

    def _cleanup(self, runtime: FixtureRuntime, binding: FixtureBinding) -> dict[str, Any]:
        try:
            report = runtime.cleanup(binding.instance, owner_token=binding.owner_token)
            binding.cleanup = report
            return report.to_record()
        except BaseException as error:  # noqa: BLE001 - 清理失败如实上报
            return {
                "instance_id": binding.instance.instance_id,
                "status": "unknown",
                "error": f"{type(error).__name__}: {error}",
            }

    # ------------------------------------------------------------ 证据

    def _emit(self, sink: list[dict[str, Any]], case_id: str, event: dict[str, Any]) -> None:
        sink.append(event)
        service = self._service
        if service is None:
            return
        payload = {key: value for key, value in event.items() if key != "type"}
        try:
            service.emit_run_event(
                self.run["id"], str(event.get("type") or "scenario_event"),
                {"case_id": case_id, **payload},
            )
        except Exception:  # noqa: BLE001 - 证据通道故障不阻断执行
            pass

    def _cancel_check(self) -> Callable[[], bool]:
        service = self._service

        def check() -> bool:
            if service is None:
                return False
            try:
                run = service.store.runs.get(self.run["id"])
            except Exception:  # noqa: BLE001 - 探测失败按未取消处理
                return False
            return bool(run and run.get("cancellation"))

        return check

    def _freeze(
        self, compiled: Any, case_id: str, outcome: WorkflowOutcome,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """冻结 Observation：checkpoint 以 Artifact + hash + owner + step 落盘。"""
        artifacts: list[dict[str, Any]] = []
        store = self._artifact_store()
        for step_id, payload in sorted(outcome.checkpoints.items()):
            state = payload.get("state")
            data = json.dumps(
                {"state": state, "content_hash": payload.get("content_hash")},
                ensure_ascii=False, sort_keys=True,
            ).encode("utf-8")
            artifact_id = f"scenario/{self.run.get('id')}/{case_id}/{step_id}.json"
            sha256 = hashlib.sha256(data).hexdigest()
            if store is not None:
                store.put_bytes(artifact_id, data, kind="workflow-checkpoint",
                                media_type="application/json")
            artifacts.append({
                "artifact_id": artifact_id,
                "step_id": step_id,
                "label": payload.get("label"),
                "content_hash": payload.get("content_hash"),
                "sha256": sha256,
                "owner": {"run_id": self.run.get("id"), "case_id": case_id},
                "available": store is not None,
            })
        observation = {
            "schema": "workflow-observation@1",
            "workflow_ref": compiled.ref,
            "workflow_content_hash": compiled.content_hash,
            "status": outcome.status,
            "reason": outcome.reason,
            "steps": [item.as_dict() for item in outcome.steps],
            "checkpoints": {
                step_id: {"content_hash": payload.get("content_hash"),
                          "label": payload.get("label")}
                for step_id, payload in sorted(outcome.checkpoints.items())
            },
            "final_state": outcome.state,
            "final_output": outcome.final_output,
            "turns": outcome.turns,
            "step_count": outcome.step_count,
            "tool_calls": outcome.tool_calls,
            "needs_review": outcome.needs_review,
            "workflow_content_digest": workflow_content_hash(compiled.snapshot),
        }
        return observation, artifacts

    def _artifact_store(self) -> Any:
        import os

        try:
            from motte_storage.artifacts import ArtifactStore
        except ImportError:  # pragma: no cover - storage 始终可用
            return None
        return ArtifactStore(os.environ.get("ARTIFACT_ROOT", "var/artifacts"))
