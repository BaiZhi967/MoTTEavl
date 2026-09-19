"""C-Eval job 适配器：进程生命周期 + 迁移 Parser 的采集（M2-T07）。

组合通用 ``ProcessJobAdapter``（受控工作目录/启动身份/中断所有权）与
迁移的 OpenCompass 结果解析：``prepare`` 把冻结 profile 落入受控目录，
``collect`` 经 ``parse_opencompass_results`` 解析输出树并产出
NormalizedCaseResult；native/diagnostic 汇总随 cursor 返回给应用层。

真实运行时 argv 指向独立固定环境里的 ``opencompass`` CLI（版本钉在
parser.RUNNER_VERSION_PIN）；测试/离线验收可用假 Runner argv 替换。
"""
from __future__ import annotations

import json
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


class CevalJobAdapter:
    """C-Eval 外部 Job 适配器（实现 contracts 的 ExternalJobAdapter 协议）。"""

    ADAPTER_ID = ADAPTER_ID
    ADAPTER_VERSION = ADAPTER_VERSION
    RUNNER_VERSION = RUNNER_VERSION_PIN

    def __init__(
        self,
        *,
        argv: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
        default_limits: dict[str, Any] | None = None,
    ) -> None:
        if argv is None:
            # 真实固定环境中的 opencompass 入口（经受控 wrapper 脚本注入）。
            argv = ["/opt/motte-runner/bin/opencompass-entry"]
        self._process = ProcessJobAdapter(
            argv=argv, extra_env=extra_env, default_limits=default_limits,
        )
        self.last_parsed: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 委托

    def prepare(self, spec: ExternalJobSpec) -> ExternalJobHandle:
        handle = self._process.prepare(spec)
        # 冻结 profile 落入受控目录（Runner 侧按需读取）。
        from pathlib import Path

        config_path = Path(handle.work_dir) / "job-profile.json"
        config_path.write_text(
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

    # ------------------------------------------------------------------ 采集

    def collect(
        self, handle: ExternalJobHandle, cursor: dict[str, Any],
    ) -> tuple[list[NormalizedCaseResult], dict[str, Any]]:
        from pathlib import Path

        outputs = Path(handle.work_dir) / "outputs"
        cursor = dict(cursor or {})
        if not outputs.is_dir():
            return ([], cursor)
        parsed = parse_opencompass_results(outputs, dataset="ceval")
        self.last_parsed = parsed
        consumed = int(cursor.get("records_consumed", 0))
        results: list[NormalizedCaseResult] = []
        for sample in parsed["samples"][consumed:]:
            results.append(NormalizedCaseResult(
                stable_case_key=str(sample["sample_id"]),
                source_case_id=str(sample["sample_id"]),
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
                    "subject": sample.get("subject"),
                    "detail_source": sample.get("detail_source"),
                },
            ))
        cursor["records_consumed"] = len(parsed["samples"])
        cursor["parser_version"] = PARSER_VERSION
        cursor["ceval_native"] = parsed["native"]
        cursor["ceval_diagnostic"] = parsed["diagnostic"]
        return (results, cursor)
