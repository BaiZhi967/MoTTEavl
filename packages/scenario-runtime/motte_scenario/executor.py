"""T05 装配层：把一个 Workflow 接进既有 CaseAttempt / Observation 链路。

一个 CaseAttempt 装配一个 FixtureInstance + 一个 TargetSession + 一个引擎
实例；流程步骤不新建调度器，也不产生伪 Trial。事件、调用日志、取消探测与
Artifact 全部走既有 RunService / ArtifactStore 通道。

真值隔离：目标只能看到 fixture 的业务可见投影（visible_state /
visible_tool_result），checker 真值、gold 与隐藏断言留在 PrivateTruth 的私有根，
永不进入事件、产物或工具结果。工具模式（real / mock / replay / deny）各自消费
**自己的**来源：mock 走显式模拟实现、replay 消费冻结记录，未配置支持即具名
拒绝，绝不回退到真实 handler，目标自身调用也不再把 real 写死。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4

from motte_contracts.fixture import FixtureSpec
from motte_contracts.workflow import TOOL_MODES, workflow_content_hash

from .compiler import compile_workflow
from .engine import STATUS_NEEDS_REVIEW, WorkflowEngine, WorkflowOutcome
from .fixtures import CleanupReport, FixtureRuntime
from .state import hidden_field_paths, minimal_visible_fields, project_visible_value, write_json_state
from .targets import target_adapter


def _attach_cleanups(error: BaseException, cleanups: list[dict[str, Any]]) -> None:
    """把清理结论挂到原异常上：原错误与清理结果都不丢（R1 验收要求）。"""
    try:
        setattr(error, "cleanups", cleanups)
    except Exception:  # noqa: BLE001 - 极少数异常不允许设置属性
        pass
    if cleanups:
        try:
            error.add_note(
                "scenario fixture cleanup: "
                + json.dumps(cleanups, ensure_ascii=False, default=str)
            )
        except Exception:  # noqa: BLE001 - 备注只是补充证据
            pass


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
        mock_tools: Mapping[str, Callable[..., Any]] | None = None,
        replay_records: Mapping[str, Mapping[str, Any]] | None = None,
        events: Mapping[str, Callable[..., Any]] | None = None,
        tool_log: list[dict[str, Any]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.binding = binding
        #: 真实实现；只有 mode=real 才会被调用。
        self.tools = dict(tools or {})
        #: 显式模拟实现；mock 模式只走这里，绝不回退 real。
        self.mock_tools = dict(mock_tools or {})
        #: 冻结回放记录：{tool: {"result": ..., "state": ...}}。
        self.replay_records = {str(key): dict(value) for key, value in (replay_records or {}).items()}
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
        """按 mode 分派一次工具调用；每个 mode 只消费自己的来源。"""
        if mode == "deny":
            self._log(name, mode, "denied", None)
            raise ScenarioToolError("SCENARIO_TOOL_DENIED", f"tool denied by policy: {name}")
        if mode not in TOOL_MODES:
            self._log(name, mode, "mode_unknown", None)
            raise ScenarioToolError(
                "SCENARIO_TOOL_MODE_UNKNOWN",
                f"fixture tool {name!r} was requested with unknown mode {mode!r}",
            )
        # allowed_tools 校验由 fixture 负责（tool_not_allowed）。
        self.runtime.visible_tool_result(
            self.binding.instance, owner_token=self.binding.owner_token, tool=name,
        )
        if mode == "replay":
            # replay 不重新执行业务动作：只消费冻结记录。
            return self._replay(name)
        handler = (self.tools if mode == "real" else self.mock_tools).get(name)
        if handler is None:
            self._log(name, mode, "not_implemented", None)
            raise ScenarioToolError(
                self._missing_code(mode),
                f"fixture tool {name!r} has no {mode} implementation; "
                f"{mode} mode never falls back to the real handler",
            )
        visible_state = dict(self.state)
        outcome = handler(visible_state, dict(arguments))
        new_state, result = self._split_outcome(outcome)
        if new_state is not None:
            self._write_state(new_state)
        projected = self._project(name, result)
        self._log(name, mode, "succeeded", projected)
        return projected

    @staticmethod
    def _missing_code(mode: str) -> str:
        return "SCENARIO_TOOL_NOT_IMPLEMENTED" if mode == "real" else "SCENARIO_TOOL_MOCK_MISSING"

    def _replay(self, name: str) -> Any:
        """消费一条冻结回放记录；没有记录就拒绝，绝不回退到真实 handler。"""
        record = self.replay_records.get(name)
        if record is None:
            self._log(name, "replay", "not_implemented", None)
            raise ScenarioToolError(
                "SCENARIO_TOOL_REPLAY_MISSING",
                f"fixture tool {name!r} has no frozen replay record; "
                "replay mode never re-executes the business action",
            )
        if "result" not in record:
            self._log(name, "replay", "invalid_record", None)
            raise ScenarioToolError(
                "SCENARIO_TOOL_REPLAY_INVALID",
                f"replay record for {name!r} must carry a 'result' field",
            )
        state = record.get("state")
        if state is not None:
            if not isinstance(state, Mapping):
                raise ScenarioToolError(
                    "SCENARIO_TOOL_REPLAY_INVALID",
                    f"replay record state for {name!r} must be an object",
                )
            self._write_state(dict(state))
        projected = self._project(name, record["result"])
        self._log(name, "replay", "succeeded", projected)
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
        """工具结果只带最小业务可见字段：隐藏键（含嵌套 dict/list）一律剔除。"""
        if isinstance(result, Mapping):
            projected = minimal_visible_fields(result)
            if not projected:
                raise ScenarioToolError(
                    "SCENARIO_TOOL_RESULT_EMPTY",
                    f"fixture tool {name!r} returned no business-visible field",
                )
        else:
            projected = project_visible_value(result)
        leaked = hidden_field_paths(projected)
        if leaked:
            # 纵深防御：投影之后还有隐藏键，说明有一条可见通道没按同一规则过滤。
            raise ScenarioToolError(
                "SCENARIO_HIDDEN_VALUE_LEAK",
                f"fixture tool {name!r} result still carries hidden fields: "
                + ", ".join(leaked),
            )
        return projected

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
        mock_handlers: Mapping[str, Callable[..., Any]] | None = None,
        replay_records: Mapping[str, Mapping[str, Any]] | None = None,
        event_handlers: Mapping[str, Callable[..., Any]] | None = None,
        service: Any = None,
    ) -> None:
        import os

        self.run = run
        self.manifest = run.get("manifest") or {}
        self.fixture_anchor = fixture_anchor or os.environ.get(
            "MOTTE_SCENARIO_FIXTURE_ROOT", "var/scenario-fixtures",
        )
        #: 真实 handler：只有 real 模式会用它。
        self.tool_handlers = dict(tool_handlers or {})
        #: 显式模拟实现：mock 模式专用（绝不回调真实 handler）。
        self.mock_handlers = dict(mock_handlers or {})
        #: 冻结回放记录：replay 模式专用（不重新执行业务动作）。
        self.replay_records = {
            str(key): dict(value) for key, value in (replay_records or {}).items()
        }
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
        # 逐实例移交清理所有权：每个 prepare 成功就立刻进入 bindings，后面的
        # prepare 失败时已完成实例仍然会被清理（F14），不再是"全局成功才赋值"。
        bindings: list[FixtureBinding] = []
        tool_log: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        target_started = False
        try:
            self._prepare_fixtures(runtime, case_id, attempt_id, owner_token, bindings)
            primary = bindings[0]
            port = FixturePortAdapter(
                runtime, primary, tools=self.tool_handlers,
                mock_tools=self.mock_handlers, replay_records=self.replay_records,
                events=self.event_handlers, tool_log=tool_log,
            )
            target = self._open_target(port)
            target_started = True
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
        except BaseException as error:
            # 装配/执行抛错时目标状态未知：保留现场，原错误与清理结论都要留住。
            _attach_cleanups(error, self._release(runtime, bindings, retained=target_started))
            raise
        cleanups = self._release(
            runtime, bindings, retained=self._retention_required(outcome),
        )
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

    @staticmethod
    def _retention_required(outcome: WorkflowOutcome) -> bool:
        """停止是否无法确认：needs_review / 未确认中断一律保留现场（F08）。"""
        if outcome.status == STATUS_NEEDS_REVIEW or outcome.needs_review:
            return True
        interrupt = outcome.interrupt
        return isinstance(interrupt, Mapping) and interrupt.get("confirmed") is False

    def _release(
        self, runtime: FixtureRuntime, bindings: list[FixtureBinding], *, retained: bool,
    ) -> list[dict[str, Any]]:
        return [
            self._cleanup(runtime, binding, interrupted=retained) for binding in bindings
        ]

    # ------------------------------------------------------------ 装配

    def _target_kind(self) -> str:
        from .target_identity import target_kind_of

        return target_kind_of(self.manifest)

    def _open_target(self, port: FixturePortAdapter) -> Any:
        """目标拿到的是**受控工具桥**：每一次调用都经过 fixture 的权限与投影。

        桥上的 mode 来自冻结的有效权限（manifest.target_snapshot.tool_modes），
        不再固定 real：声明里没有 real 时，目标自身调用也不允许走 real。
        """
        kind = self._target_kind()
        adapter = target_adapter(kind)
        mode = self._target_tool_mode()

        def bridge(name: str) -> Any:
            return lambda arguments: port.invoke_tool(name, arguments, mode)

        return adapter.open_session({
            "manifest": self.manifest,
            "run": self.run,
            "tools": {name: bridge(name) for name in sorted(port.tools)},
        })

    def _target_tool_mode(self) -> str:
        """目标自身工具调用的有效 mode（消费冻结声明，不猜、不写死 real）。

        判定顺序：
        1. 冻结声明包含 real → real（显式授权）；
        2. 声明里没有 real 且只有一个模式 → 用该模式（整场 mock/replay）；
        3. 声明里没有 real 又不止一个模式 → 有效 mode 无法确定，具名拒绝。
        旧 manifest 完全没有声明时沿用历史默认 real，不改变既有行为。
        """
        declared = self._declared_tool_modes()
        if not declared:
            return "real"
        unknown = sorted(set(declared) - set(TOOL_MODES))
        if unknown:
            raise ScenarioToolError(
                "SCENARIO_TOOL_MODE_UNKNOWN",
                "target_snapshot.tool_modes declares unsupported modes: "
                + ", ".join(unknown),
            )
        if "real" in declared:
            return "real"
        if len(declared) == 1:
            return declared[0]
        raise ScenarioToolError(
            "SCENARIO_TOOL_MODE_AMBIGUOUS",
            "target_snapshot does not grant real execution and declares several modes "
            f"({', '.join(declared)}); the effective target tool mode cannot be proven",
        )

    def _declared_tool_modes(self) -> tuple[str, ...]:
        snapshot = self.manifest.get("target_snapshot")
        if not isinstance(snapshot, Mapping):
            return ()
        modes = snapshot.get("tool_modes")
        if not isinstance(modes, (list, tuple, set, frozenset)):
            return ()
        ordered: list[str] = []
        for mode in modes:
            text = str(mode)
            if text not in ordered:
                ordered.append(text)
        return tuple(ordered)

    def _prepare_fixtures(
        self,
        runtime: FixtureRuntime,
        case_id: str,
        attempt_id: str,
        owner_token: str,
        bindings: list[FixtureBinding],
    ) -> None:
        """逐个 prepare 并把清理所有权立刻交给调用方（F14）。"""
        snapshots = self.manifest.get("fixture_snapshot") or {}
        if not snapshots:
            raise ScenarioToolError(
                "SCENARIO_FIXTURE_REQUIRED", "scenario runs require pinned fixture snapshots"
            )
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
            # 先登记再继续：下一个 prepare 失败时这个实例已经被接管。
            bindings.append(FixtureBinding(
                key=key, spec=spec, instance=instance, owner_token=owner_token,
            ))

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

    def _cleanup(
        self, runtime: FixtureRuntime, binding: FixtureBinding, *, interrupted: bool = False,
    ) -> dict[str, Any]:
        """清理一个实例；interrupted 表示停止未确认，此时保留现场与审计证据。"""
        try:
            report = runtime.cleanup(
                binding.instance, owner_token=binding.owner_token, interrupted=interrupted,
            )
            binding.cleanup = report
            record = report.to_record()
            record["evidence_path"] = report.evidence_path
            return record
        except BaseException as error:  # noqa: BLE001 - 清理失败如实上报
            return {
                "instance_id": binding.instance.instance_id,
                "status": "unknown",
                "interrupted": interrupted,
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
