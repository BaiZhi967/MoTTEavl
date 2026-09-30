"""Read-only Experiment Cell assembly, using the standalone Run preparation path.

This boundary does not publish a Spec, create a Run, enqueue a Job or execute a
Provider. New suite branches stay fail-closed until their execution bound is
proved. Call-bound evidence is an internal output, never a public input.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Sequence

from motte_contracts.experiment import (
    CevalExperimentConfig, ExperimentSpec, FactorAssignment, ScenarioExperimentConfig, SkillExperimentConfig,
)
from motte_contracts.hashing import canonical_hash

if TYPE_CHECKING:
    from .skill_ablation import AblationPlan


@dataclass(frozen=True)
class CallBound:
    """Bound emitted only after a supported software execution path compiles."""

    max_calls: int
    execution_boundary: str
    controlled_retries: dict[str, int]


@dataclass(frozen=True)
class AssembledExperimentCell:
    scenario_version: str
    requested_manifest: dict[str, Any]
    manifest: dict[str, Any]
    case_ids: tuple[str, ...]
    resource_hashes: dict[str, str]
    call_bound: CallBound


@dataclass(frozen=True)
class SkillFactorArm:
    arm_id: str
    skill_ref: str | None
    snapshot: dict[str, Any] | None


@dataclass(frozen=True)
class SkillGroupInputs:
    """Compile-time shared non-Skill inputs; not an ExperimentSpec field.

    One frozen no-Skill baseline and one full ablation plan per non-Skill
    factor group; each Cell selects one unprepared requested manifest.
    """

    model_profile: str
    scenario_version: str
    requested_manifest: dict[str, Any]
    manifest: dict[str, Any]
    case_ids: tuple[str, ...]
    resource_hashes: dict[str, str]
    call_bound: CallBound
    plan: AblationPlan
    arm_id_by_value: dict[str, str]
    requested_manifests: dict[str, dict[str, Any]]


def resolve_skill_axis(spec: ExperimentSpec, *, resources: Any) -> tuple[SkillFactorArm, ...]:
    """Resolve the exact three-value axis, retaining nonempty declaration order."""
    from motte_skill.versions import SkillError, select_skills

    from .experiments import ExperimentError

    if not isinstance(spec.suite_config, SkillExperimentConfig):
        raise ExperimentError("SKILL_CONFIG_REQUIRED", "Skill axis requires typed Skill config")
    if resources is None or getattr(resources, "skills", None) is None:
        raise ExperimentError("RESOURCE_UNRESOLVED", "Skill resources are unavailable")
    arms = [SkillFactorArm("no-skill", None, None)]
    # The literal is handled before ordinary resources; it is never a Skill ref.
    references = [ref for ref in spec.factors["skill_version"] if ref != "no-skill"]
    for arm_id, reference in zip(("skill-v1", "skill-v2"), references, strict=True):
        name, _separator, version = reference.rpartition("@")
        try:
            selected = select_skills(resources.skills, [{"skill_id": name, "version": version}])
        except (SkillError, ValueError) as error:
            raise ExperimentError("SKILL_NOT_PUBLISHED", f"{reference}: {error}") from error
        skill = selected[0]
        if skill.injection_mode == "native-loader" or skill.kind == "executable":
            raise ExperimentError(
                "SKILL_EXECUTION_UNSUPPORTED",
                f"{reference}: builtin Target only delivers frozen context instructions",
            )
        arms.append(SkillFactorArm(arm_id, reference, skill.model_dump(mode="json")))
    return tuple(arms)


def validate_suite_assembly_ready(spec: ExperimentSpec, *, resources: Any) -> None:
    """Reject any unimplemented suite before an executable object can be written."""
    from .experiments import ExperimentError

    config = spec.suite_config
    # Static support only: recovery/retry must not consult current resources.
    if config is None or isinstance(config, (ScenarioExperimentConfig, SkillExperimentConfig)):
        return
    if isinstance(config, CevalExperimentConfig):
        from motte_benchmark.opencompass.execution import freeze_execution_profile

        try:
            freeze_execution_profile(config.execution_profile)
        except ValueError as error:
            raise ExperimentError("EXPERIMENT_BUDGET_UNPROVABLE", str(error)) from error
        return
    raise ExperimentError(
        "SUITE_ASSEMBLER_UNIMPLEMENTED",
        f"{config.kind} experiment assembly has no proved executable implementation yet",
    )


def build_legacy_requested_manifest(
    spec: ExperimentSpec, assignment: FactorAssignment,
) -> dict[str, Any]:
    """Original Direct/GSM/Agent request shape, without a second budget formula."""
    from .experiments import ExperimentError, _validate_supported_suite

    _validate_supported_suite(spec)
    if spec.suite_config is not None:
        raise ExperimentError(
            "SUITE_ASSEMBLER_UNIMPLEMENTED", "typed suite cannot use a legacy manifest builder",
        )
    values = assignment.as_dict()
    suite = spec.task_ref["suite"]
    manifest: dict[str, Any] = {"model": values["model_profile"]}
    if suite == "agent-tasks":
        manifest["agent"] = {"mode": spec.controlled_conditions.get("agent_mode") or "legacy-json"}
    if values.get("reasoning_level") is not None:
        manifest["reasoning_level"] = values["reasoning_level"]
    for key, value in spec.controlled_conditions.items():
        if value is not None and key == "max_output_tokens":
            manifest["parameters"] = {"max_output_tokens": value}
    if spec.selected_case_keys:
        manifest["case_selection"] = {"mode": "ids", "case_ids": list(spec.selected_case_keys)}
    return manifest


def validate_legacy_execution_boundary(spec: ExperimentSpec, manifest: dict[str, Any]) -> None:
    """External evidence cannot borrow a native suite's call formula on replay.

    This is a rejection guard, not a new positive profile or a stronger promise
    for historical native Cells. Time, turns, Trial counts and SDK stream counts
    do not prove the transport attempts inside an opaque Runtime or task script.
    """
    from .experiments import ExperimentError

    execution = manifest.get("execution") or {}
    expected = "builtin-agent" if spec.task_ref["suite"] == "agent-tasks" else "direct-llm"
    agent = manifest.get("agent")
    if (
        any(manifest.get(key) is not None for key in (
            "runtime", "runtime_profile", "runtime_snapshot", "external_benchmark",
        ))
        or (isinstance(agent, str) and agent != "builtin-agent@1")
        or (execution and (execution.get("backend_id"), execution.get("backend_version"))
            != (expected, "1"))
    ):
        raise ExperimentError(
            "EXPERIMENT_BUDGET_UNPROVABLE",
            "external Runtime/Harbor profile has no verified transport bound; "
            "native suite labels, time/turn/budget/Trial/stream limits are not proof",
        )


def _legacy_call_bound(spec: ExperimentSpec, manifest: dict[str, Any], cases: tuple[str, ...]):
    from .experiments import ExperimentError

    validate_legacy_execution_boundary(spec, manifest)
    provider = manifest.get("provider") or {}
    # Managed standalone preparation explicitly pins its provider retry limit.
    retries = provider.get("max_retries") if isinstance(provider, dict) else None
    if type(retries) is not int or retries < 0:
        raise ExperimentError("BUDGET_UNRESOLVED", "provider retry limit is unknown")
    model_steps = 1
    if spec.task_ref["suite"] == "agent-tasks":
        agent_budget = (manifest.get("agent_config") or {}).get("budget") or {}
        model_steps = agent_budget.get("max_steps")
        if type(model_steps) is not int or model_steps < 1:
            raise ExperimentError("BUDGET_UNRESOLVED", "agent model step limit is unknown")
    execution = manifest["execution"]
    return CallBound(
        max_calls=len(cases) * model_steps * (1 + retries),
        execution_boundary=execution["backend_id"] + "@" + execution["backend_version"],
        controlled_retries={"provider": retries},
    )


def _resource_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    """Historical legacy resource identities; typed Workflows use full-value digests."""
    hashes = {}
    for name, snapshot in (manifest.get("resource_snapshots") or {}).items():
        if isinstance(snapshot, dict):
            hashes[name] = snapshot.get("content_hash") or canonical_hash(snapshot)
    for name in ("benchmark_snapshot", "benchmark_provenance", "cases",
                 "fixture_snapshot", "target_snapshot", "evaluation"):
        if manifest.get(name) is not None:
            hashes[name] = canonical_hash(manifest[name])
    return hashes


def _workflow_resource_hashes(manifest: dict[str, Any]) -> dict[str, str]:
    """Hash actual frozen values, never a digest asserted by their own payload.

    Resource identity hashes and executable copies are different things. The
    latter are consumed by Worker and must be covered even when a resource also
    has a metadata-only snapshot or a duplicate Workflow representation.
    """
    return {
        **{name: canonical_hash(snapshot)
           for name, snapshot in (manifest.get("resource_snapshots") or {}).items()},
        **{name: canonical_hash(manifest[name]) for name in (
            "provider", "workflow_snapshot", "fixture_snapshot", "target_snapshot",
            "evaluation", "cases",
        ) if name in manifest},
        "resolved_manifest": canonical_hash(manifest),
    }


def _validate_workflow_snapshot_links(manifest: dict[str, Any]) -> None:
    from motte_contracts.workflow import WorkflowVersion
    from motte_skill.injection import DECLARATION_SCHEMA_VERSION, plan_from_declaration

    snapshots = manifest["resource_snapshots"]
    workflow = WorkflowVersion.model_validate(manifest["workflow_snapshot"])
    workflow.effective_content_hash()
    if (manifest["workflow_snapshot"] != snapshots["workflow"]
            or f"{workflow.workflow_id}@{workflow.version}" != manifest["workflow"]
            or workflow.target_requirements.model_dump(mode="json") != manifest["target_snapshot"]):
        raise ValueError("executable Workflow differs from its pinned resource identity")
    provider = manifest["provider"]
    profile = snapshots["model_profile"]
    if (profile["id"] != manifest["model"]
            or provider.get("model_profile_id") != profile["id"]
            or provider.get("model_profile_hash") != (profile.get("profile_hash") or profile["content_hash"])
            or provider.get("name") != snapshots["provider_connection"]["name"]):
        raise ValueError("executable Provider differs from its pinned model/connection identity")
    refs = manifest.get("skills") or []
    injection = snapshots.get("skill_injection")
    if not refs:
        if injection is not None:
            raise ValueError("no-Skill Cell contains an injection declaration")
        return
    fields = {"schema_version", "refs", "plan_hash", "injection_digest", "declaration",
              "render_context_sha256", "executables"}
    if (not isinstance(injection, dict) or set(injection) != fields
            or injection["schema_version"] != DECLARATION_SCHEMA_VERSION
            or injection["refs"] != refs or injection["executables"]):
        raise ValueError("Skill injection shape/refs differ from the Cell's fixed Skill")
    plan = plan_from_declaration(injection)
    if (injection["declaration"] != plan.as_dict()
            or injection["plan_hash"] != plan.plan_hash
            or [f"{entry.skill_id}@{entry.version}" for entry in plan.entries] != refs
            or any(not entry.content_hash for entry in plan.entries)):
        raise ValueError("Skill injection entries differ from the Cell's pinned Skill identity")


def skill_group_key(assignment: FactorAssignment) -> str:
    """Non-Skill factor identity; repeats never introduce another plan axis."""
    non_skill = FactorAssignment(values=tuple(
        value for value in assignment.values if value.factor != "skill_version"
    ))
    return canonical_hash(non_skill.canonical_payload())


def _validate_assignment(spec: ExperimentSpec, assignment: FactorAssignment) -> None:
    from .experiments import ExperimentError, _validate_supported_suite

    _validate_supported_suite(spec)
    values = assignment.as_dict()
    if set(values) != set(spec.factors) or any(
        value not in spec.factors[factor] for factor, value in values.items()
    ):
        raise ExperimentError("FACTOR_ASSIGNMENT_INVALID", "assignment is outside the declared matrix")


def _workflow_requested_manifest(
    spec: ExperimentSpec, assignment: FactorAssignment,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    from .experiments import ExperimentError

    _validate_assignment(spec, assignment)
    config = spec.suite_config
    if not isinstance(config, (ScenarioExperimentConfig, SkillExperimentConfig)):
        raise ExperimentError("WORKFLOW_CONFIG_REQUIRED", "typed Workflow config is required")
    if spec.selected_case_keys:
        raise ExperimentError("CASE_SELECTION_CONFLICT", "Workflow cases come from suite_config.cases")
    budget = config.execution_budget.model_dump(exclude_none=True)
    cases = {
        case if isinstance(case, str) else case.case_id:
        {} if isinstance(case, str) or case.business_id is None else {"business_id": case.business_id}
        for case in config.cases
    }
    requested = {
        "model": assignment.as_dict()["model_profile"], "workflow": config.workflow_ref,
        "agent": "builtin-agent@1", "agent_config": {"mode": config.agent_mode, "budget": budget},
        "budget": deepcopy(budget), "cases": cases,
    }
    if budget.get("max_output_tokens") is not None:
        requested["parameters"] = {"max_output_tokens": budget["max_output_tokens"]}
    return requested, tuple(cases)


def _workflow_call_bound(manifest: dict[str, Any], cases: tuple[str, ...]) -> CallBound:
    from .experiments import ExperimentError

    # One ScenarioCaseExecutor owns exactly one BuiltinTargetSession. The DSL has
    # no target fan-out node. Its session budget is cumulative across sends.
    if manifest.get("agent") != "builtin-agent@1" or manifest.get("runtime") is not None:
        raise ExperimentError("TARGET_UNSUPPORTED", "only builtin-agent@1 has a proved session bound")
    execution = manifest.get("execution") or {}
    if (execution.get("backend_id"), execution.get("backend_version")) != ("scenario", "1"):
        raise ExperimentError("TARGET_UNSUPPORTED", "Workflow cells require scenario@1 execution")
    provider = manifest.get("provider") or {}
    # These built-in adapters perform one HTTPTransport request per complete;
    # custom/upstream retry behavior has no proof and is not inferred as zero.
    if provider.get("kind") not in {"openai_compatible", "openai_responses", "anthropic_messages"}:
        raise ExperimentError("PROVIDER_BOUND_UNSUPPORTED", "provider has no proved transport bound")
    retries = provider.get("max_retries")
    steps = (manifest.get("agent_config") or {}).get("budget", {}).get("max_steps")
    if type(retries) is not int or retries < 0 or type(steps) is not int or steps < 1:
        raise ExperimentError("BUDGET_UNRESOLVED", "explicit session steps and provider retries required")
    return CallBound(len(cases) * steps * (1 + retries), "scenario@1", {"provider": retries})


def _validate_fixture_hashes(manifest: dict[str, Any]) -> None:
    from motte_contracts.fixture import FixtureSpec, fixture_content_hash
    from .experiments import ExperimentError

    for reference, snapshot in (manifest.get("fixture_snapshot") or {}).items():
        record = snapshot.get("record") or {}
        actual = fixture_content_hash(FixtureSpec.model_validate(record))
        if actual != snapshot.get("content_hash") or actual != record.get("content_hash"):
            raise ExperimentError("FIXTURE_CONTENT_HASH_MISMATCH", reference)


def _prepare_workflow_cell(
    spec: ExperimentSpec, requested: dict[str, Any], case_ids: tuple[str, ...], *, resources: Any,
) -> AssembledExperimentCell:
    from .experiments import ExperimentError
    from .resolve import ManifestResolutionError, prepare_run
    from .benchmark_plugins import plugin_for_scenario

    if resources is None:
        raise ExperimentError("RESOURCE_UNRESOLVED", "experiment resources are unavailable")
    scenario_version = spec.task_ref.get("scenario_version", "")
    name, sep, version = scenario_version.rpartition("@")
    scenario = resources.scenarios.get(name, version) if sep else None
    if scenario is None:
        raise ExperimentError("SCENARIO_NOT_FOUND", scenario_version)
    if plugin_for_scenario(scenario) is not None:
        raise ExperimentError("SUITE_MISMATCH", "typed Workflow cells require a Scenario resource")
    try:
        manifest, resolved_cases = prepare_run(scenario_version, requested, list(case_ids), resources)
    except ManifestResolutionError as error:
        raise ExperimentError(error.code, str(error)) from error
    cases = tuple(resolved_cases)
    _validate_fixture_hashes(manifest)
    try:
        _validate_workflow_snapshot_links(manifest)
    except (KeyError, TypeError, ValueError) as error:
        raise ExperimentError("FROZEN_INPUT_INVALID", str(error)) from error
    return AssembledExperimentCell(
        scenario_version, deepcopy(requested), deepcopy(manifest), cases,
        _workflow_resource_hashes(manifest), _workflow_call_bound(manifest, cases),
    )


def assemble_scenario_cell(
    spec: ExperimentSpec, assignment: FactorAssignment, *, resources: Any, store: Any,
) -> AssembledExperimentCell:
    from .experiments import ExperimentError

    if not isinstance(spec.suite_config, ScenarioExperimentConfig):
        raise ExperimentError("SCENARIO_CONFIG_REQUIRED", "Scenario assembler requires Scenario config")
    requested, cases = _workflow_requested_manifest(spec, assignment)
    return _prepare_workflow_cell(spec, requested, cases, resources=resources)


def prepare_skill_groups(
    spec: ExperimentSpec, assignments: Sequence[FactorAssignment], *, resources: Any, store: Any,
) -> dict[str, SkillGroupInputs]:
    """One full standalone three-arm plan per non-Skill group, per compilation."""
    from .skill_ablation import ArmSpec, arm_manifests, plan_skill_ablation

    arms = resolve_skill_axis(spec, resources=resources)
    mapping = {arm.skill_ref or "no-skill": arm.arm_id for arm in arms}
    groups = {}
    for assignment in assignments:
        key = skill_group_key(assignment)
        if key in groups:
            continue
        base, case_ids = _workflow_requested_manifest(spec, assignment)
        plan = plan_skill_ablation(
            base_manifest=base, experiment_ref=f"{spec.experiment_id}@{spec.version}/{key}",
            arms=[ArmSpec(arm.arm_id, (arm.skill_ref,) if arm.skill_ref else (), arm.snapshot)
                  for arm in arms], budget_policy=spec.suite_config.budget_policy, case_keys=case_ids,
        )
        requested = arm_manifests(base, plan)
        baseline = _prepare_workflow_cell(spec, requested["no-skill"], case_ids, resources=resources)
        groups[key] = SkillGroupInputs(
            assignment.as_dict()["model_profile"], baseline.scenario_version,
            baseline.requested_manifest, baseline.manifest, baseline.case_ids,
            baseline.resource_hashes, baseline.call_bound, plan, dict(mapping), requested,
        )
    return groups


def _non_skill_conditions(manifest: dict[str, Any]) -> dict[str, Any]:
    conditions = deepcopy(manifest)
    conditions.pop("skills", None)
    conditions.pop("skill_arm", None)
    conditions.get("resource_snapshots", {}).pop("skill_injection", None)
    return conditions


def assemble_skill_cell(
    spec: ExperimentSpec, assignment: FactorAssignment, *, resources: Any, store: Any,
    skill_group: SkillGroupInputs,
) -> AssembledExperimentCell:
    from .experiments import ExperimentError

    _validate_assignment(spec, assignment)
    values = assignment.as_dict()
    expected_ref = f"{spec.experiment_id}@{spec.version}/{skill_group_key(assignment)}"
    if (values["model_profile"] != skill_group.model_profile
            or skill_group.plan.experiment_ref != expected_ref):
        raise ExperimentError("SKILL_GROUP_MISMATCH", "Cell does not belong to the compiled group")
    arm_id = skill_group.arm_id_by_value[values["skill_version"]]
    assembled = _prepare_workflow_cell(
        spec, skill_group.requested_manifests[arm_id], skill_group.case_ids, resources=resources,
    )
    if (assembled.case_ids != skill_group.case_ids
            or _non_skill_conditions(assembled.manifest) != _non_skill_conditions(skill_group.manifest)
            or assembled.call_bound != skill_group.call_bound):
        raise ExperimentError("SKILL_CONDITION_DRIFT", "resolved non-Skill conditions changed in group")
    arm = next(arm for arm in skill_group.plan.arms if arm.arm_id == arm_id)
    declaration = assembled.manifest.get("resource_snapshots", {}).get("skill_injection")
    entries = (declaration or {}).get("declaration", {}).get("entries", [])
    expected = arm.skill_snapshot
    if expected is not None and (
        len(entries) != 1 or any(entries[0].get(key) != expected.get(key)
                                for key in ("skill_id", "version", "content_hash"))
    ):
        raise ExperimentError("SKILL_CONTENT_DRIFT", "Skill content changed after the group plan")
    return assembled


def validate_frozen_workflow_cell(
    spec: ExperimentSpec, assignment: FactorAssignment, frozen: dict[str, Any] | None,
) -> None:
    """Validate persisted typed inputs in place, without current resource lookups.

    Typed Workflow Cells have never had a legacy selector-only representation.
    A missing or incompatible snapshot must not fall back to rebuilding it.
    """
    from .experiments import ExperimentError

    if isinstance(spec.suite_config, CevalExperimentConfig):
        return validate_frozen_ceval_cell(spec, assignment, frozen)
    if not isinstance(spec.suite_config, (ScenarioExperimentConfig, SkillExperimentConfig)):
        return
    if not isinstance(frozen, dict):
        raise ExperimentError("FROZEN_INPUT_REQUIRED", "typed cells require frozen prepared_run")
    expected, case_ids = _workflow_requested_manifest(spec, assignment)
    if isinstance(spec.suite_config, SkillExperimentConfig):
        value = assignment.as_dict()["skill_version"]
        refs = [ref for ref in spec.factors["skill_version"] if ref != "no-skill"]
        expected.update(
            skills=[] if value == "no-skill" else [value],
            skill_arm="no-skill" if value == "no-skill" else f"skill-v{refs.index(value) + 1}",
        )
        expected["budget"]["policy"] = spec.suite_config.budget_policy
    try:
        manifest = frozen["manifest"]
        _validate_fixture_hashes(manifest)
        _validate_workflow_snapshot_links(manifest)
        if (frozen["requested_manifest"] != expected
                or tuple(frozen["case_ids"]) != case_ids
                or frozen["scenario_version"] != spec.task_ref["scenario_version"]
                or any(manifest.get(key) != expected[key] for key in (
                    "model", "workflow", "agent", "agent_config", "cases",
                ))
                or any(manifest.get(key) != expected.get(key) for key in ("skills", "skill_arm"))
                or {key: value for key, value in manifest.get("budget", {}).items()
                    if key != "context_preflight"} != expected["budget"]
                or frozen["resource_hashes"] != _workflow_resource_hashes(manifest)
                or frozen["call_bound"] != asdict(_workflow_call_bound(manifest, case_ids))):
            raise ValueError("frozen conditions do not match the typed Cell")
    except (KeyError, TypeError, ValueError) as error:
        raise ExperimentError("FROZEN_INPUT_INVALID", str(error)) from error


def assemble_experiment_cell(
    spec: ExperimentSpec, assignment: FactorAssignment, *, resources: Any, store: Any,
    skill_group: SkillGroupInputs | None = None,
) -> AssembledExperimentCell:
    """Compile one read-only frozen Cell input; no externally supplied manifest."""
    from .experiments import ExperimentError, _validate_task_resource
    from .resolve import ManifestResolutionError, prepare_run

    _validate_assignment(spec, assignment)
    if isinstance(spec.suite_config, SkillExperimentConfig) and skill_group is None:
        raise ExperimentError("SKILL_GROUP_REQUIRED", "Skill Cell needs compiled SkillGroupInputs")
    validate_suite_assembly_ready(spec, resources=resources)
    if resources is None:
        raise ExperimentError("RESOURCE_UNRESOLVED", "experiment resources are unavailable")
    if isinstance(spec.suite_config, CevalExperimentConfig):
        return assemble_ceval_cell(spec, assignment, resources=resources, store=store)
    if isinstance(spec.suite_config, ScenarioExperimentConfig):
        return assemble_scenario_cell(spec, assignment, resources=resources, store=store)
    if isinstance(spec.suite_config, SkillExperimentConfig):
        return assemble_skill_cell(
            spec, assignment, resources=resources, store=store, skill_group=skill_group,
        )
    _validate_task_resource(spec, resources)
    if spec.task_ref["suite"] == "gsm8k":
        from motte_contracts.gsm8k import PRESET

        cap = spec.controlled_conditions.get("max_output_tokens")
        fixed_cap = PRESET["max_output_tokens"]
        if cap is not None and cap != fixed_cap:
            raise ExperimentError(
                "CONTROLLED_CONDITION_UNSUPPORTED",
                f"gsm8k fixes max_output_tokens at {fixed_cap}; received {cap}",
            )
    requested = build_legacy_requested_manifest(spec, assignment)
    scenario = spec.task_ref["scenario_version"]
    try:
        manifest, case_ids = prepare_run(scenario, requested, [], resources)
    except ManifestResolutionError as error:
        raise ExperimentError(error.code, str(error)) from error
    ordered_cases = tuple(case_ids)
    return AssembledExperimentCell(
        scenario_version=scenario, requested_manifest=deepcopy(requested),
        manifest=deepcopy(manifest), case_ids=ordered_cases,
        resource_hashes=_resource_hashes(manifest),
        call_bound=_legacy_call_bound(spec, manifest, ordered_cases),
    )


def _ceval_request(spec: ExperimentSpec, assignment: FactorAssignment) -> dict[str, Any]:
    from .experiments import ExperimentError

    _validate_assignment(spec, assignment)
    config = spec.suite_config
    if spec.task_ref.get("scenario_version") != "ceval-external@1":
        raise ExperimentError("SUITE_MISMATCH", "C-Eval requires ceval-external@1")
    return {"model": assignment.as_dict()["model_profile"],
            **config.model_dump(mode="json", exclude={"kind"}),
            "case_ids": list(spec.selected_case_keys) or None}


def _ceval_call_bound(manifest: dict[str, Any], cases: tuple[str, ...]) -> CallBound:
    from motte_benchmark.opencompass.execution import validate_execution_config

    external = manifest["external_benchmark"]
    config = external["runner_config"]
    profile = validate_execution_config(config)
    if (tuple(case["case_id"] for case in config["cases"]) != cases
            or external["adapter_id"] != "ceval-opencompass"
            or external["adapter_version"] != "1"
            or external["parser_version"] != "ceval-opencompass-parser@2"
            or external.get("retry_policy") != profile["retry"]
            or external.get("environment_digest") != canonical_hash(profile)
            or external.get("profile", {}).get("environment_digest") != canonical_hash(profile)
            or manifest.get("execution") != {"backend_id": "external-benchmark", "backend_version": "1"}):
        raise ValueError("EXECUTION_PROFILE_INVALID: frozen C-Eval identity differs")
    return CallBound(len(cases) * (1 + profile["retry"]["provider_transport"]),
                     profile["profile_id"], dict(profile["retry"]))


def assemble_ceval_cell(
    spec: ExperimentSpec, assignment: FactorAssignment, *, resources: Any, store: Any,
) -> AssembledExperimentCell:
    """Reuse the standalone frozen builder; never send its reserved fields to prepare_run."""
    from .benchmark_catalog import (
        dataset_from_payload, prepare_external_run_inputs, validate_external_run_request,
    )
    from .experiments import ExperimentError

    requested = _ceval_request(spec, assignment)
    record = store.benchmark_datasets.get("ceval", requested["dataset_revision"])
    if record is None:
        raise ExperimentError("DATASET_UNPREPARED", "fixed C-Eval revision is missing")
    dataset = dataset_from_payload(record)
    if dataset.benchmark_id != "ceval" or dataset.dataset_revision != requested["dataset_revision"]:
        raise ExperimentError("DATASET_REVISION_MISMATCH", "stored dataset identity differs")
    model = resources.models.get(requested["model"])
    common = {key: requested[key] for key in ("scope", "split", "few_shot", "few_shot_split", "case_ids")}
    reasons = validate_external_run_request(dataset, benchmark_id="ceval", model_record=model, **common)
    if reasons:
        raise ExperimentError("RUN_REQUEST_INVALID", "; ".join(reasons))
    try:
        inputs = prepare_external_run_inputs(
            dataset, benchmark_id="ceval", model_id=requested["model"], model_record=model,
            seed=requested["seed"], execution_profile=requested["execution_profile"], **common,
        )
        manifest = inputs["manifest"]
        cases = tuple(inputs["case_ids"])
        bound = _ceval_call_bound(manifest, cases)
    except ValueError as error:
        raise ExperimentError("EXPERIMENT_BUDGET_UNPROVABLE", str(error)) from error
    return AssembledExperimentCell(inputs["scenario_version"], deepcopy(requested), deepcopy(manifest),
                                   cases, {"resolved_manifest": canonical_hash(manifest)}, bound)


def validate_frozen_ceval_cell(spec, assignment, frozen):
    """Only frozen Cell inputs; no mutable dataset/model/provider repository reads."""
    from .experiments import ExperimentError

    if not isinstance(frozen, dict):
        raise ExperimentError("FROZEN_INPUT_REQUIRED", "C-Eval requires frozen prepared_run")
    try:
        expected = _ceval_request(spec, assignment)
        manifest = frozen["manifest"]
        config = manifest["external_benchmark"]["runner_config"]
        cases = tuple(frozen["case_ids"])
        bound = _ceval_call_bound(manifest, cases)
        if (frozen["requested_manifest"] != expected
                or manifest["model"] != expected["model"]
                or frozen["scenario_version"] != spec.task_ref["scenario_version"]
                or any(config.get(key) != expected[key] for key in ("dataset_revision", "scope", "split", "seed"))
                or config["few_shot"] != {"count": expected["few_shot"], "source_split": expected["few_shot_split"]}
                or config["execution_profile"]["profile_id"] != expected["execution_profile"]
                or (expected["case_ids"] is not None and list(cases) != expected["case_ids"])
                or frozen["resource_hashes"] != {"resolved_manifest": canonical_hash(manifest)}
                or frozen["call_bound"] != asdict(bound)):
            raise ValueError("frozen C-Eval conditions differ from typed Cell")
    except (KeyError, TypeError, ValueError) as error:
        raise ExperimentError("FROZEN_INPUT_INVALID", str(error)) from error
