"""OpenCompass job 适配器：进程生命周期 + 迁移 Parser 的采集。

组合通用 ``ProcessJobAdapter``（受控工作目录/启动身份/中断所有权）与
迁移的 OpenCompass 结果解析。C-Eval 与 CMMLU 共享本适配器骨架，仅
``dataset`` 命名空间不同（review R16）。

- ``prepare`` 把冻结 profile **和 Runner 可消费的完整配置**
  （``spec.runner_config``：模型快照/逐题 prompt/few-shot/凭据引用/配置
  hash）写入受控目录（review R01：真实 Runner 拿得到数据与模型）。
- ``collect`` 先经 fd 锚定的受信读取解析输出树（信任边界=本工作目录，
  review R06），再按 ``runner-config.json`` 的冻结 case 顺序把
  ``(subject, 行序)`` 映射回稳定 Case ID（review R03：Runner 序号不直接
  当 Case ID）。未知/越界映射隔离为带错误的 unmapped 记录，不串题。
- ``snapshot_outputs`` 输出原始文件的 hash/大小清单，供持久层在解析前
  冻结原始证据（review R09）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from motte_contracts.external_job import (
    CaseResultStatus,
    ExternalJobHandle,
    ExternalJobSpec,
    NormalizedCaseResult,
)

from ..process import ProcessJobAdapter
from .parser import PARSER_VERSION, RUNNER_VERSION_PIN, parse_opencompass_results

ADAPTER_ID = "ceval-opencompass"
ADAPTER_VERSION = "1"

RUNNER_CONFIG_NAME = "runner-config.json"
PROFILE_NAME = "job-profile.json"


class OpenCompassJobAdapter:
    """OpenCompass 外部 Job 适配器（实现 contracts 的 ExternalJobAdapter 协议）。

    ``dataset`` 决定结果文件命名空间（ceval-*.json / cmmlu-*.json）与
    聚合口径分派；C-Eval 与 CMMLU 各自注册独立 adapter 实例。
    """

    ADAPTER_VERSION = ADAPTER_VERSION
    RUNNER_VERSION = RUNNER_VERSION_PIN

    def __init__(
        self,
        *,
        dataset: str = "ceval",
        argv: list[str] | None = None,
        module: str | None = None,
        extra_env: dict[str, str] | None = None,
        default_limits: dict[str, Any] | None = None,
    ) -> None:
        if dataset not in ("ceval", "cmmlu"):
            raise ValueError(
                f"unknown opencompass dataset namespace: {dataset!r} (ceval/cmmlu)"
            )
        self.dataset = dataset
        self.adapter_id = f"{dataset}-opencompass"
        if argv is None and module is None:
            # 真实固定环境中的 opencompass 入口（经受控 wrapper 脚本注入）。
            argv = ["/opt/motte-runner/bin/opencompass-entry"]
        self._process = ProcessJobAdapter(
            argv=argv, module=module, extra_env=extra_env, default_limits=default_limits,
        )
        self.last_parsed: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 委托

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        handle = self._process.prepare(spec)
        # 冻结 profile 与 Runner 可消费配置落入受控目录（review R01）。
        config_path = Path(handle.work_dir) / RUNNER_CONFIG_NAME
        config_path.write_text(
            json.dumps(spec.runner_config or {}, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        profile_path = Path(handle.work_dir) / PROFILE_NAME
        profile_path.write_text(
            json.dumps(spec.profile, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return handle

    def start(self, spec: ExternalJobSpec, handle: ExternalJobHandle) -> ExternalJobHandle:
        return self._process.start(spec, handle)

    def poll(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        return self._process.poll(handle)

    def interrupt(self, handle: ExternalJobHandle) -> ExternalJobHandle:
        return self._process.interrupt(handle)

    def cleanup(self, handle: ExternalJobHandle) -> dict[str, Any]:
        return self._process.cleanup(handle)

    @property
    def start_calls(self) -> list[dict[str, Any]]:
        return self._process.start_calls

    def spawned_processes(self) -> list[int]:
        return self._process.spawned_processes()

    # ------------------------------------------------------------------ 证据

    def snapshot_outputs(self, handle: ExternalJobHandle) -> dict[str, Any]:
        """原始输出文件的受信清单（字节级 sha256），解析前冻结证据用（R09）。"""
        from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError

        try:
            workspace = CaseWorkspace(
                Path(handle.work_dir), anchor=Path(handle.work_dir).parent,
            )
            snap = workspace.snapshot()
            files = {
                rel: digest for rel, digest in (snap.get("hashes") or {}).items()
                if rel.startswith("outputs/")
            }
        except (WorkspacePolicyError, OSError):
            return {"complete": False, "files": {}}
        return {"complete": bool(snap.get("complete")) and bool(files), "files": files}

    # ------------------------------------------------------------------ 采集

    def _frozen_case_order(self, handle: ExternalJobHandle) -> dict[str, list[str]]:
        """从受控目录里的冻结配置读 subject → 有序 case_id 映射（R03）。"""
        from motte_sandbox.workspace import CaseWorkspace, WorkspacePolicyError

        workspace = CaseWorkspace(
            Path(handle.work_dir), anchor=Path(handle.work_dir).parent,
        )
        raw = workspace.read_text(RUNNER_CONFIG_NAME, max_bytes=32 * 1024 * 1024)
        config = json.loads(raw)
        cases = config.get("cases") if isinstance(config, dict) else None
        if not isinstance(cases, list) or not cases:
            raise ValueError(
                "frozen runner config has no cases; cannot map runner rows to case ids"
            )
        order: dict[str, list[str]] = {}
        for case in cases:
            if not isinstance(case, dict):
                continue
            case_id = str(case.get("case_id") or "")
            subject = str(case.get("subject") or "")
            if case_id and subject:
                order.setdefault(subject, []).append(case_id)
        return order

    def collect(
        self, handle: ExternalJobHandle, cursor: dict[str, Any],
    ) -> tuple[list[NormalizedCaseResult], dict[str, Any]]:
        cursor = dict(cursor or {})
        outputs = Path(handle.work_dir) / "outputs"
        if not outputs.is_dir():
            return ([], cursor)
        parsed = parse_opencompass_results(
            outputs, dataset=self.dataset, trusted_root=Path(handle.work_dir),
        )
        self.last_parsed = parsed
        try:
            case_order = self._frozen_case_order(handle)
        except Exception as error:  # noqa: BLE001 - 缺冻结映射：拒绝采集而非串题
            raise ValueError(
                f"frozen case mapping unavailable: {error}",
            ) from error
        consumed = int(cursor.get("records_consumed", 0))
        seen: dict[str, int] = {}
        used: set[str] = set()
        results: list[NormalizedCaseResult] = []
        for sample in parsed["samples"][consumed:]:
            subject = str(sample.get("subject") or "")
            position = seen.get(subject, 0)
            seen[subject] = position + 1
            sample_id = str(sample.get("sample_id") or "")
            ordered = case_order.get(subject)
            mapped = ordered[position] if ordered is not None and position < len(ordered) else None
            if mapped is None:
                # 未知学科/越界行序：隔离为 unmapped，绝不归属到相邻题目（R03）。
                results.append(NormalizedCaseResult(
                    stable_case_key=f"unmapped:{sample_id}",
                    source_case_id=sample_id or f"unmapped:{subject}-{position}",
                    output={"prediction": sample.get("prediction")},
                    error={
                        "code": "EXTERNAL_CASE_MAPPING_UNKNOWN",
                        "message": (
                            "runner row has no frozen case mapping: "
                            f"subject={subject!r} position={position}"
                        ),
                    },
                    status=CaseResultStatus.failed,
                    evidence_coverage={
                        "subject": subject, "sample_id": sample_id,
                        "detail_source": sample.get("detail_source"),
                    },
                ))
                continue
            if mapped in used:
                results.append(NormalizedCaseResult(
                    stable_case_key=f"mapping-conflict:{mapped}:{sample_id}",
                    source_case_id=mapped,
                    output={"prediction": sample.get("prediction")},
                    error={
                        "code": "EXTERNAL_CASE_MAPPING_CONFLICT",
                        "message": (
                            "frozen case id received more than one runner row: "
                            f"case={mapped} second_row={sample_id}"
                        ),
                    },
                    status=CaseResultStatus.failed,
                    evidence_coverage={
                        "subject": subject, "sample_id": sample_id,
                        "detail_source": sample.get("detail_source"),
                    },
                ))
                continue
            used.add(mapped)
            results.append(NormalizedCaseResult(
                stable_case_key=mapped,
                source_case_id=mapped,
                output={
                    "prediction": sample.get("prediction"),
                    "gold": sample.get("gold"),
                    "extraction": sample.get("extraction"),
                    "official_prediction": sample.get("official_prediction"),
                    "raw_output": sample.get("raw_output"),
                },
                error=None,
                status=CaseResultStatus.succeeded,
                evidence_coverage={
                    "subject": subject,
                    "sample_id": sample_id,
                    "detail_source": sample.get("detail_source"),
                },
            ))
        cursor["records_consumed"] = len(parsed["samples"])
        cursor["parser_version"] = PARSER_VERSION
        cursor["ceval_native"] = parsed["native"]
        cursor["ceval_diagnostic"] = parsed["diagnostic"]
        return (results, cursor)


class CevalJobAdapter(OpenCompassJobAdapter):
    """C-Eval 命名空间实例（兼容既有引用；CMMLU 用 OpenCompassJobAdapter）。"""

    ADAPTER_ID = ADAPTER_ID

    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        default_limits: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            dataset="ceval", argv=argv, extra_env=extra_env,
            default_limits=default_limits,
        )


class CmmluJobAdapter(OpenCompassJobAdapter):
    """CMMLU 命名空间实例（独立 adapter 身份，review R16）。"""

    ADAPTER_ID = "cmmlu-opencompass"

    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        default_limits: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            dataset="cmmlu", argv=argv, extra_env=extra_env,
            default_limits=default_limits,
        )
