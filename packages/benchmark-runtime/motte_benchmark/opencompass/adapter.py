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
from .parser import PARSER_VERSION, RUNNER_VERSION_PIN, parse_opencompass_files

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

    @property
    def parser_version(self) -> str:
        """Actual parser identity, including dataset namespace (recovery guard)."""
        return PARSER_VERSION.replace("ceval-", f"{self.dataset}-", 1)

    @property
    def default_limits(self) -> dict[str, Any]:
        """监督层读取有效 Job limits 的入口（review R2-09：默认超时生效）。"""
        return self._process.default_limits

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

    @staticmethod
    def _evidence_rels(rel: str) -> bool:
        """证据白名单（review R3-05）：只收集结果/预测/实验指针/冻结映射。

        上游会把**解析后的配置**（含运行时解析的密钥）dump 到实验目录
        ``configs/*.py``；无差别归档会把密钥写进永久 raw bundle。此处只
        允许：``outputs/**/results/**``、``outputs/**/predictions/**``、
        ``outputs/experiment.json`` 与 ``runner-config.json``（映射随输入
        一并冻结，R3-06）。
        """
        parts = rel.split("/")
        if rel == RUNNER_CONFIG_NAME:
            return True
        if not rel.startswith("outputs/"):
            return False
        if rel == "outputs/experiment.json":
            return True
        return any(part in ("results", "predictions") for part in parts[:-1])

    def read_output_files(self, handle: ExternalJobHandle) -> dict[str, str]:
        """fd 锚定读取证据白名单文件（review R3-04/R3-05）。

        沿已打开的工作目录 fd 逐组件 ``O_NOFOLLOW`` 打开（复用 Parser 的
        ``_TrustedDir``）：父目录/根目录在读取间被替换成 symlink 时，
        fd 链上的打开直接失败——不存在"先字符串检查再 os.open"的竞态。
        symlink 出现在输出边界内立即拒绝。返回 ``{相对路径: 文本}`` 供
        持久层在**解析之前**冻结完整输入字节（review R2-06/R3-06）。
        """
        from .parser import _TrustedDir

        work_root = Path(handle.work_dir)
        limit = int(self._process.default_limits["max_result_bytes"])
        try:
            with _TrustedDir(work_root) as trusted:
                files, symlinks = trusted.list_files()
                for link in symlinks:
                    if link == "outputs" or link.startswith("outputs/") or link == RUNNER_CONFIG_NAME:
                        raise ValueError(
                            "symlink inside evidence boundary rejected: " + link,
                        )
                wanted = [rel for rel in files if self._evidence_rels(rel)]
                evidence: dict[str, str] = {}
                for rel in sorted(wanted):
                    evidence[rel] = trusted.read_bytes(rel, max_bytes=limit).decode(
                        "utf-8",
                    )
                return evidence
        except ValueError:
            raise
        except Exception as error:  # noqa: BLE001 - 读取失败进入证据
            raise ValueError(f"cannot read controlled outputs: {error}") from error

    def snapshot_outputs(self, handle: ExternalJobHandle) -> dict[str, Any]:
        """原始输出文件的受信清单（字节级 sha256），解析前冻结证据用（R09）。"""
        import hashlib

        try:
            files = {
                rel: hashlib.sha256(content.encode("utf-8")).hexdigest()
                for rel, content in self.read_output_files(handle).items()
            }
        except (ValueError, OSError):
            return {"complete": False, "files": {}}
        return {"complete": bool(files), "files": files}

    # ------------------------------------------------------------------ 采集

    @staticmethod
    def _case_order_from_config(config: Any) -> dict[str, list[str]]:
        if not isinstance(config, dict):
            raise ValueError("frozen runner config is not an object")
        cases = config.get("cases")
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

    @staticmethod
    def _experiment_from_files(files: dict[str, str]) -> str | None:
        """从冻结证据中读取实验指针（review R3-09：选择信息进入解析）。"""
        pointer = files.get("outputs/experiment.json")
        if pointer is None:
            return None
        try:
            payload = json.loads(pointer)
        except ValueError as error:
            raise ValueError(
                f"experiment pointer is not valid JSON: {error}",
            ) from error
        if isinstance(payload, dict) and isinstance(payload.get("experiment"), str):
            return payload["experiment"]
        return None

    @staticmethod
    def _row_index_of(sample: dict[str, Any], subject: str) -> int | None:
        """从 sample_id（``<subject>-<原始行号>``）取 Runner 明细行号。

        行号是 Runner 输出的稳定身份（R2-03）：稀疏输出/增量游标都按它
        定位冻结 Case，绝不按"本次遍历第几个"。
        """
        sample_id = str(sample.get("sample_id") or "")
        _, _, tail = sample_id.rpartition("-")
        if not tail or not tail.isdigit():
            return None
        return int(tail)

    def collect_from_files(
        self,
        handle: ExternalJobHandle,
        cursor: dict[str, Any],
        files: dict[str, str],
    ) -> tuple[list[NormalizedCaseResult], dict[str, Any]]:
        """从**冻结字节**解析 + 按原始行号映射（R2-03/R2-06/R3-06/R3-09）。

        映射来自冻结 bundle 内的 ``runner-config.json``（与结果字节同一份
        内容 hash 覆盖）——修改工作目录里的配置不改变 Case 身份；实验选择
        来自冻结的 ``outputs/experiment.json`` 指针。
        """
        cursor = dict(cursor or {})
        raw_config = files.get(RUNNER_CONFIG_NAME)
        if raw_config is None:
            raise ValueError(
                "frozen case mapping unavailable: runner-config.json is not part "
                "of the frozen evidence; refuse to attribute runner rows"
            )
        try:
            case_order = self._case_order_from_config(json.loads(raw_config))
        except ValueError as error:
            raise ValueError(f"frozen case mapping unavailable: {error}") from error
        experiment = self._experiment_from_files(files)
        parsed = parse_opencompass_files(
            files, dataset=self.dataset, base_parts=("outputs",),
            experiment=experiment,
        )
        self.last_parsed = parsed
        consumed = int(cursor.get("records_consumed", 0))
        used: set[str] = set()
        results: list[NormalizedCaseResult] = []
        for sample in parsed["samples"][consumed:]:
            subject = str(sample.get("subject") or "")
            sample_id = str(sample.get("sample_id") or "")
            row_index = self._row_index_of(sample, subject)
            ordered = case_order.get(subject)
            mapped = (
                ordered[row_index]
                if ordered is not None and row_index is not None and row_index < len(ordered)
                else None
            )
            if mapped is None:
                # 未知学科/无法解析行号/越界：隔离为 unmapped，绝不归属相邻题。
                reason = (
                    f"subject={subject!r} row_index={row_index!r}"
                    if row_index is None
                    else f"subject={subject!r} row_index={row_index} >= {len(ordered or [])}"
                )
                results.append(NormalizedCaseResult(
                    stable_case_key=f"unmapped:{sample_id}",
                    source_case_id=sample_id or f"unmapped:{subject}",
                    output={"prediction": sample.get("prediction")},
                    error={
                        "code": "EXTERNAL_CASE_MAPPING_UNKNOWN",
                        "message": f"runner row has no frozen case mapping: {reason}",
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
        cursor["parser_version"] = parsed["parser_version"]
        cursor["ceval_native"] = parsed["native"]
        cursor["ceval_diagnostic"] = parsed["diagnostic"]
        return (results, cursor)

    def collect(
        self, handle: ExternalJobHandle, cursor: dict[str, Any],
    ) -> tuple[list[NormalizedCaseResult], dict[str, Any]]:
        outputs = Path(handle.work_dir) / "outputs"
        if not outputs.is_dir():
            return ([], dict(cursor or {}))
        # 先按白名单读完整证据字节（含 runner-config 与实验指针），再从
        # 同一份内容解析（R2-06/R3-05/R3-06）；缺冻结映射先拒绝。
        files = self.read_output_files(handle)
        if RUNNER_CONFIG_NAME not in files:
            raise ValueError(
                "frozen case mapping unavailable: runner-config.json is missing "
                "in the controlled work dir"
            )
        return self.collect_from_files(handle, dict(cursor or {}), files)


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
