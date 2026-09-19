"""固定版 OpenCompass Runner 桥接入口（review R2-01）。

由 ``scripts/runner/opencompass-entry`` 以**固定环境解释器**调用：

```
python -m motte_benchmark.opencompass.entry
```

职责（生命周期身份在本层消费，绝不透传给上游 CLI）：

1. 读取 ``$MOTTE_WORK_DIR/runner-config.json``（平台冻结配置：模型快照、
   逐题 prompt/题干、few-shot、凭据**引用**、config_hash）；
2. 按学科导出本地数据文件 ``data/<subject>.jsonl``（题干/选项/gold）；
3. 渲染 OpenCompass **0.4.2** 形态的 Python 配置（``models``/``datasets``；
   凭据引用在配置内经 ``os.environ`` 解析，明文不落盘）；
4. 以 **0.4.2 的位置参数**调用上游 CLI（``opencompass.cli.main
   <config> --work-dir <outputs>``；不带 ``--config/--launch-token`` 等
   该版本不存在的参数）；
5. 定位本次执行的时间戳实验目录并写 ``outputs/experiment.json`` 指针
   （review R2-02：解析侧据此选择唯一实验，绝不混入其他运行）；
6. 原子写完成标记 ``.motte-job-complete``（review R2-10 契约）。

固定环境要求（部署清单见 docs/operations/ceval.md）：opencompass==0.4.2
与 motte-benchmark-runtime 安装在同一 pinned 解释器环境。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

CONFIG_NAME = "runner-config.json"
GENERATED_CONFIG = "opencompass-config.py"
DATA_DIR = "data"
EXPERIMENT_POINTER = "experiment.json"
COMPLETION_MARKER = ".motte-job-complete"
DEFAULT_PINNED_PYTHON = "/opt/motte-runner/bin/python"


def _read_platform_config(work: Path) -> dict[str, Any]:
    config = json.loads(
        (work / CONFIG_NAME).read_text(encoding="utf-8"),
    )
    if not isinstance(config, dict) or not isinstance(config.get("cases"), list):
        raise SystemExit(f"{CONFIG_NAME} has no cases; cannot bridge to opencompass")
    return config


def export_subject_files(work: Path, config: dict[str, Any]) -> dict[str, Path]:
    """按学科导出本地 JSONL（OpenCompass 数据集的数据源，review R3-02）。

    消费生产 ``build_opencompass_config`` 的结构化字段（question/options），
    不再从空缺字段兜底；题目、选项与选样顺序与冻结 runner-config 同源。
    目标样本 gold 不在 cases 中，导出行不含答案（gold 与 subject 输入隔离）。
    few-shot 示例内容另存 ``data/few_shot.json``（含示例 gold——few-shot 示
    例带答案是合法且必需的，与目标样本不同）。
    """
    data_dir = work / DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for case in config.get("cases") or []:
        if not isinstance(case, dict):
            continue
        options = case.get("options") if isinstance(case.get("options"), dict) else {}
        by_subject.setdefault(str(case.get("subject") or ""), []).append({
            "id": case.get("case_id"),
            "question": case.get("question", ""),
            "A": options.get("A", ""),
            "B": options.get("B", ""),
            "C": options.get("C", ""),
            "D": options.get("D", ""),
        })
    exported: dict[str, Path] = {}
    for subject, cases in sorted(by_subject.items()):
        target = data_dir / f"{subject or 'default'}.jsonl"
        target.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in cases) + "\n",
            encoding="utf-8",
        )
        exported[subject] = target
    examples = config.get("few_shot_examples")
    if isinstance(examples, list) and examples:
        (data_dir / "few_shot.json").write_text(
            json.dumps(examples, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        exported["__few_shot__"] = data_dir / "few_shot.json"
    return exported


def _credential_lines(credentials: dict[str, Any]) -> list[str]:
    """凭据引用 → 生成配置内的 os.environ 解析（明文不落盘）。"""
    lines: list[str] = []
    for name, ref in sorted((credentials or {}).items()):
        env_name = str(ref).removeprefix("env:")
        lines.append(
            f'_creds[{name!r}] = os.environ.get({env_name!r}, "")'
            f"  # ref: {ref}"
        )
    return lines


def render_opencompass_config_source(
    config: dict[str, Any], data_files: dict[str, Path],
) -> str:
    """平台冻结配置 → OpenCompass 0.4.2 Python 配置源码（review R3-01）。

    0.4.2 契约（见 opencompass/datasets/custom.py 与 models/openai_api.py
    @ 0.4.2）：数据集/模型项用 ``type=<类对象>``（无 type 会被当作 custom
    dataset 且要求 path 为文件路径）；OpenAI 兼容端点参数是
    ``openai_api_base``（不是 ``url``）。本地数据集在本配置内定义并导入，
    生成源可直接被 exec/加载。凭据引用在运行时经 os.environ 解析。
    """
    model = config.get("model") or {}
    creds_lines = _credential_lines(config.get("credentials") or {})
    model_path = str(model.get("model") or model.get("id") or "")
    model_params = model.get("parameters") or {}
    max_out = int(
        model_params.get("max_output_tokens")
        or config.get("max_output_tokens")
        or 1024
    )
    dataset_entries = []
    few_shot_file = data_files.get("__few_shot__")
    for subject, path in sorted(data_files.items()):
        if subject == "__few_shot__":
            continue
        dataset_entries.append(
            "    dict(\n"
            "        type=LocalMCQDataset,\n"
            f"        name={subject!r},\n"
            f"        data_file={str(path)!r},\n"
            f"        few_shot_file={str(few_shot_file)!r},\n"
            "    ),"
        )
    param_lines = "\n".join(
        f"        {key}={value!r}," for key, value in sorted(model_params.items())
        if key != "max_output_tokens"
    )
    return f'''"""Auto-generated by motte_benchmark OpenCompass bridge (DO NOT EDIT).

Target: opencompass==0.4.2（位置参数 config + --work-dir；数据集/模型项
type=<类对象>；OpenAI 兼容端点参数 openai_api_base）。数据来自平台冻结
的本地 JSONL；凭据引用在运行时经 os.environ 解析，本文件不含明文密钥。
"""
import os

from opencompass.datasets.base import BaseDataset
from opencompass.models import OpenAI


class LocalMCQDataset(BaseDataset):
    """本地冻结 MCQ 行（平台侧导出；目标样本 gold 不进数据/prompt）。"""

    @staticmethod
    def load(path, few_shot_file=None):
        import json as _json

        with open(path, "r", encoding="utf-8") as handle:
            rows = [_json.loads(line) for line in handle if line.strip()]
        if few_shot_file:
            with open(few_shot_file, "r", encoding="utf-8") as handle:
                rows[0]["few_shot_examples"] = _json.load(handle)
        return rows


_creds = {{}}
{chr(10).join(creds_lines) or "_creds['api_key'] = os.environ.get('OPENAI_API_KEY', '')"}

models = [
    dict(
        type=OpenAI,
        path={model_path!r},
        key=_creds.get("api_key", ""),
        openai_api_base=_creds.get("base_url", ""),
        max_out_len={max_out},
{param_lines}
    ),
]

datasets = [
{chr(10).join(dataset_entries)}
]
'''


def write_experiment_pointer(work: Path, outputs: Path) -> str | None:
    """定位本次执行的时间戳实验目录并写指针（review R2-02）。

    只接受 ``outputs`` 下恰好一层、不含 results/predictions 的目录；有
    多个候选时选**最新修改**的一个并全部记录在指针里，供人工核对；
    解析侧遇多候选仍会拒绝（指针即显式选择）。
    """
    if not outputs.is_dir():
        return None
    candidates = sorted(
        (
            item for item in outputs.iterdir()
            if item.is_dir() and item.name not in ("results", "predictions")
        ),
        key=lambda item: item.stat().st_mtime,
    )
    if not candidates:
        return None
    chosen = candidates[-1]
    pointer = {
        "experiment": chosen.name,
        "candidates": [item.name for item in candidates],
        "chosen_by": "latest-mtime",
    }
    partial = outputs / (EXPERIMENT_POINTER + ".partial")
    partial.write_text(
        json.dumps(pointer, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    partial.replace(outputs / EXPERIMENT_POINTER)
    return chosen.name


def write_completion_marker(work: Path, exit_code: int) -> None:
    partial = work / (COMPLETION_MARKER + ".partial")
    partial.write_text(
        json.dumps({"exit_code": exit_code, "completed": True}), encoding="utf-8",
    )
    partial.replace(work / COMPLETION_MARKER)


IDENTITY_NAME = "launch-identity.json"


def record_launch_identity(work: Path, argv: list[str]) -> None:
    """把生命周期身份落工作目录（审计用；review R3-03）。

    ``--launch-token`` 留在本进程 argv 里（wrapper exec 时注入），跨实例
    核验按 argv 匹配；本文件额外持久化 job/run 归属，供操作员对账。
    身份绝不传给上游 OpenCompass CLI。
    """
    import socket

    payload = {
        "launch_token": os.environ.get("MOTTE_LAUNCH_TOKEN", ""),
        "job_id": os.environ.get("MOTTE_JOB_ID", ""),
        "run_id": os.environ.get("MOTTE_RUN_ID", ""),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "recorded_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (work / IDENTITY_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )


async def _run_cli(argv: list[str]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=None,
        stderr=None,
    )
    return await proc.wait()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="motte_benchmark.opencompass.entry")
    # 身份参数由 wrapper 注入并**保留在本进程 argv**（跨实例核验按 argv
    # 匹配 launch token，review R3-03）；本层消费后绝不传给上游 CLI。
    parser.add_argument("--launch-token", dest="launch_token", default=None)
    parser.add_argument("--job-id", dest="job_id", default=None)
    parser.add_argument("--run-id", dest="run_id", default=None)
    args = parser.parse_args(argv)
    if args.launch_token:
        os.environ.setdefault("MOTTE_LAUNCH_TOKEN", args.launch_token)
    if args.job_id:
        os.environ.setdefault("MOTTE_JOB_ID", args.job_id)
    if args.run_id:
        os.environ.setdefault("MOTTE_RUN_ID", args.run_id)

    work_env = os.environ.get("MOTTE_WORK_DIR", "")
    work = Path(work_env) if work_env else None
    if work is None or not work.is_dir():
        print("MOTTE_WORK_DIR must be an existing directory", file=sys.stderr)
        return 2
    try:
        record_launch_identity(work, argv if argv is not None else sys.argv)
        config = _read_platform_config(work)
        data_files = export_subject_files(work, config)
        generated = work / GENERATED_CONFIG
        generated.write_text(
            render_opencompass_config_source(config, data_files),
            encoding="utf-8",
        )
    except (OSError, ValueError, SystemExit) as error:
        print(f"bridge config generation failed: {error}", file=sys.stderr)
        write_completion_marker(work, 3)
        return 3

    outputs = work / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    pinned_python = os.environ.get("MOTTE_RUNNER_PYTHON", DEFAULT_PINNED_PYTHON)
    if not Path(pinned_python).exists():
        print(f"pinned runner environment missing: {pinned_python}", file=sys.stderr)
        write_completion_marker(work, 4)
        return 4
    # 0.4.2 CLI：config 是位置参数；身份（token/job/run）由本桥接消费，
    # 不向上游透传未知参数（review R2-01）。
    cli_argv = [
        pinned_python, "-m", "opencompass.cli.main", str(generated),
        "--work-dir", str(outputs),
    ]
    exit_code = asyncio.run(_run_cli(cli_argv))
    write_experiment_pointer(work, outputs)
    write_completion_marker(work, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
