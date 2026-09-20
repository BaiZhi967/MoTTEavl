"""任务自带 Docker Compose 的策略检查（M3 review R02，M3-T05/A13）。

固定版 Harbor 0.23.0（``harbor/environments/definition.py``）把任务目录下的
``environment/docker-compose.yaml``（``COMPOSE_FILE_NAME``）作为 overlay 加进
``docker compose -f ...`` 命令行（``docker.py::_docker_compose_paths``），因此
任务可以借它注入额外服务、宿主 bind mount、``privileged``、host network、
危险 capability 等——这些**不在**平台声明的 ``mounts``/``env`` 参数里，
只检查声明参数等于没检查真实执行配置。

本模块只读地解析任务实际的 compose 文件，逐项产生具名 reason code：

- 危险执行模式：``privileged``、host network、pid/ipc/userns host namespace、
  危险 capability、device 暴露、seccomp/apparmor/label 被关闭；
- 越过任务目录的引用：宿主 bind mount 源、build context、env_file，
  以及 ``include``/``extends`` 指向外部文件（无法证明安全就拒绝）；
- 凭据插值：``${SOMETHING_SECRET}`` 之类会被宿主环境展开的变量名；
- 命中既有 ``FORBIDDEN_HOST_PATHS``/docker.sock 时复用
  ``TASK_HOST_PATH_EXPOSED``/``TASK_DOCKER_SOCKET_EXPOSED``；
- **fail closed**：``pyyaml`` 不可用、文件读不到、解析不了、结构不是对象，
  一律 ``TASK_COMPOSE_UNINSPECTABLE``——绝不"读不到就放行"。

检查过程零执行：不 docker build、不启动容器、不调用模型；读取走
``TrustedDir``（拒 symlink、fd 锚定、大小限额）。
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from motte_benchmark.harbor.tasks import (
    TASK_ENVIRONMENT_DIR,
    HarborTaskError,
    normalize_relative_path,
)
from motte_benchmark.trusted import TrustedDir, TrustedPathError
from motte_contracts.trial import canonical_hash

#: Harbor 固定版本真正加载的任务 compose 文件名（``COMPOSE_FILE_NAME``）。
COMPOSE_FILE_NAME = "docker-compose.yaml"
#: 一并检查的 compose 常见变体：Harbor 当前只读 ``.yaml``，但改名/升级后
#: 同一份危险内容不该因为扩展名不同而被放行。
COMPOSE_FILE_NAMES: tuple[str, ...] = (
    COMPOSE_FILE_NAME, "docker-compose.yml", "compose.yaml", "compose.yml",
)

#: 单个 compose 文件的读取限额（超过即视为不可检查）。
MAX_COMPOSE_BYTES = 1024 * 1024
POLICY_SCHEMA = "motte-task-compose-policy@1"

#: 任务容器绝对不允许出现的宿主路径（挂载或构建上下文都不行）。
FORBIDDEN_HOST_PATHS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/etc/shadow",
    "/etc/sudoers",
    "~/.ssh",
    "~/.aws",
    "~/.config/gcloud",
    "~/.docker",
    "~/.kube",
)
#: 任务容器不允许继承的宿主环境变量（平台内部变量，按前缀/精确名匹配）。
FORBIDDEN_ENV_NAMES: tuple[str, ...] = (
    "MOTTE_PG_DSN", "DATABASE_URL", "ARTIFACT_ROOT", "MOTTE_DB_PATH",
    "MOTTE_DB_PATH", "MOTTE_RUNNER_PYTHON", "MOTTE_LAUNCH_TOKEN",
)
FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_", "OPENAI_", "ANTHROPIC_", "AZURE_", "GOOGLE_", "HF_", "MOTTE_",
)
#: 凭据类变量名的通用特征（未知厂商也要被识别，不能只查已知前缀）。
CREDENTIAL_NAME_MARKERS: tuple[str, ...] = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
    "CREDENTIAL", "ACCESS_KEY", "PRIVATE_KEY", "_KEY", "LICENSE_KEY",
)

#: 任务 compose 里不允许的 capability（提权到宿主或绕过审计）。
DANGEROUS_CAPABILITIES: frozenset[str] = frozenset({
    "ALL", "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "NET_ADMIN",
    "DAC_READ_SEARCH", "DAC_OVERRIDE", "SYS_RAWIO", "SYS_BOOT", "SYS_TIME",
    "BPF", "PERFMON", "AUDIT_CONTROL", "AUDIT_READ", "MAC_ADMIN", "MAC_OVERRIDE",
    "NET_RAW", "SYS_CHROOT", "SYSLOG", "WAKE_ALARM", "MKNOD", "SETFCAP",
    "BLOCK_SUSPEND", "CHECKPOINT_RESTORE", "IPC_LOCK", "SYS_RESOURCE",
})
#: 关闭默认隔离的 security_opt 取值（去空格变小写后比较）。
_DISABLING_SECURITY_OPTS: frozenset[str] = frozenset({
    "seccomp=unconfined", "seccomp:unconfined", "apparmor=unconfined",
    "apparmor:unconfined", "label=disable", "label:disable",
    "no-new-privileges=false",
})
#: 共享宿主/其他容器命名空间的键（值非 host/container 不拒绝）。
_HOST_NAMESPACE_KEYS: tuple[str, ...] = ("pid", "ipc", "userns_mode", "uts", "cgroup")
#: compose 变量插值：``${NAME}`` / ``${NAME:-default}`` / ``$NAME``。
_INTERPOLATION = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::?[-?][^}]*)?\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)",
)


def looks_like_credential(name: str) -> bool:
    """变量名是否像凭据（大小写不敏感，覆盖未登记的厂商）。"""
    upper = name.upper()
    return upper in FORBIDDEN_ENV_NAMES or upper.startswith(
        FORBIDDEN_ENV_PREFIXES,
    ) or any(marker in upper for marker in CREDENTIAL_NAME_MARKERS)


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _violation(code: str, *, file: str, detail: str, service: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "file": file, "detail": detail}
    if service is not None:
        item["service"] = service
    return item


def _forbidden_hits(source: str) -> list[str]:
    """宿主路径命中的禁用条目（字面路径、解析后别名，以及覆盖禁用路径的父目录）。

    ``/:/host``、``~:/host``、``/etc:/etc`` 这类写法把禁用路径**装在里面**，
    因此"挂载源是禁用条目的祖先"同样算命中——否则整个宿主根目录可以合法
    挂进任务容器。
    """
    banned = [os.path.normpath(os.path.expanduser(item)) for item in FORBIDDEN_HOST_PATHS]
    candidates = {os.path.normpath(os.path.expanduser(source))}
    try:
        candidates.add(str(Path(source).expanduser().resolve()))
    except OSError:  # pragma: no cover - 宿主路径异常
        pass
    hits: set[str] = set()
    for candidate in candidates:
        if candidate == os.sep:
            hits.add(os.sep)
            continue
        for entry in banned:
            if candidate == entry or candidate.startswith(entry + os.sep):
                hits.add(entry)
            elif entry.startswith(candidate + os.sep):
                hits.add(entry)
    return sorted(hits)


def _resolve_against_compose(compose_dir: Path, source: str) -> Path:
    """compose 里的路径写法 → 宿主绝对路径。

    Harbor 用 ``--project-directory <environment 目录>`` 运行 compose，因此
    相对路径的基准是**任务自己的 environment 目录**。
    """
    expanded = os.path.expanduser(str(source))
    candidate = Path(expanded)
    if candidate.is_absolute():
        return Path(os.path.normpath(expanded))
    return Path(os.path.normpath(str(compose_dir / expanded)))


def _inside(compose_dir: Path, source: str) -> bool:
    resolved = _resolve_against_compose(compose_dir, source)
    return resolved == compose_dir or compose_dir in resolved.parents


def _volume_source(entry: Any) -> tuple[str | None, bool]:
    """卷条目的 ``(source, is_bind)``。

    按 compose 语义：源以 ``/``、``.``、``~`` 开头才是 bind mount，否则是
    命名卷（命名卷不暴露宿主路径）；单段条目是容器内路径（匿名卷）。
    """
    if isinstance(entry, str):
        parts = entry.split(":")
        if len(parts) == 1:
            return (None, False)
        source = parts[0]
        return (source, source.startswith(("/", ".", "~")))
    if isinstance(entry, Mapping):
        source = entry.get("source")
        if not isinstance(source, str):
            return (None, False)
        kind = entry.get("type")
        is_bind = str(kind) == "bind" or (
            kind is None and source.startswith(("/", ".", "~"))
        )
        return (source, is_bind)
    return (None, False)


def _check_volume(
    compose_dir: Path, entry: Any, *, file: str, service: str,
    violations: list[dict[str, Any]],
) -> None:
    source, is_bind = _volume_source(entry)
    if not source or not is_bind:
        return
    if "docker.sock" in source:
        violations.append(_violation(
            "TASK_DOCKER_SOCKET_EXPOSED", file=file, service=service,
            detail=f"任务 compose 把 Docker socket 挂进任务容器: {source}",
        ))
    hits = _forbidden_hits(source)
    if hits:
        violations.append(_violation(
            "TASK_HOST_PATH_EXPOSED", file=file, service=service,
            detail=f"任务 compose 挂载了宿主敏感路径 {source}（命中 {hits}）",
        ))
    if not _inside(compose_dir, source):
        violations.append(_violation(
            "TASK_COMPOSE_EXTERNAL_BIND", file=file, service=service,
            detail=f"bind mount 源不在该任务自己的 environment 目录内: {source}",
        ))


def _check_build(
    compose_dir: Path, build: Any, *, file: str, service: str,
    violations: list[dict[str, Any]],
) -> None:
    if isinstance(build, str):
        context: Any = build
    elif isinstance(build, Mapping):
        context = build.get("context")
        dockerfile = build.get("dockerfile")
        if isinstance(dockerfile, str) and not _inside(compose_dir, dockerfile):
            violations.append(_violation(
                "TASK_COMPOSE_EXTERNAL_BUILD_CONTEXT", file=file, service=service,
                detail=f"build.dockerfile 不在任务目录内: {dockerfile}",
            ))
        if build.get("additional_contexts"):
            violations.append(_violation(
                "TASK_COMPOSE_EXTERNAL_BUILD_CONTEXT", file=file, service=service,
                detail="additional_contexts 无法证明只引用任务自己的目录",
            ))
    else:
        return
    if isinstance(context, str) and context and not _inside(compose_dir, context):
        violations.append(_violation(
            "TASK_COMPOSE_EXTERNAL_BUILD_CONTEXT", file=file, service=service,
            detail=f"build context 不在任务目录内: {context}",
        ))


def _check_env_file(
    compose_dir: Path, env_file: Any, *, file: str, service: str,
    violations: list[dict[str, Any]],
) -> None:
    entries = (
        list(env_file)
        if isinstance(env_file, Sequence) and not isinstance(env_file, str)
        else [env_file]
    )
    for item in entries:
        if isinstance(item, Mapping):
            item = item.get("path")
        if not isinstance(item, str) or not item:
            continue
        if not _inside(compose_dir, item):
            violations.append(_violation(
                "TASK_COMPOSE_EXTERNAL_ENV_FILE", file=file, service=service,
                detail=f"env_file 不在任务目录内: {item}",
            ))


def _check_credentials(
    environment: Any, *, file: str, service: str, violations: list[dict[str, Any]],
) -> None:
    """``${SOMETHING_SECRET}`` 之类插值会把宿主环境里的凭据塞进任务容器。"""
    values: list[str] = []
    if isinstance(environment, Mapping):
        values = [str(value) for value in environment.values()]
    elif isinstance(environment, Sequence) and not isinstance(environment, str):
        values = [str(item) for item in environment]
    for value in values:
        for match in _INTERPOLATION.finditer(value):
            name = match.group("braced") or match.group("bare") or ""
            if name and looks_like_credential(name):
                violations.append(_violation(
                    "TASK_COMPOSE_CREDENTIAL_FORWARD", file=file, service=service,
                    detail=f"任务 compose 插值了凭据类变量 ${{{name}}}",
                ))
                break


def _check_service(
    compose_dir: Path, name: str, service: Any, *, file: str,
    violations: list[dict[str, Any]],
) -> None:
    if not isinstance(service, Mapping):
        violations.append(_violation(
            "TASK_COMPOSE_UNINSPECTABLE", file=file, service=name,
            detail="service 定义不是对象，无法证明安全",
        ))
        return
    if service.get("privileged") is True:
        violations.append(_violation(
            "TASK_COMPOSE_PRIVILEGED", file=file, service=name,
            detail="privileged 容器等同于宿主 root",
        ))
    if str(service.get("network_mode") or "").lower() in ("host", "container"):
        violations.append(_violation(
            "TASK_COMPOSE_HOST_NETWORK", file=file, service=name,
            detail=f"network_mode: {service['network_mode']} 使用宿主/其他容器网络",
        ))
    if service.get("pid_mode") and str(service["pid_mode"]).lower() in ("host", "container"):
        violations.append(_violation(
            "TASK_COMPOSE_HOST_NAMESPACE", file=file, service=name,
            detail=f"pid_mode: {service['pid_mode']} 共享宿主 PID 命名空间",
        ))
    for key in _HOST_NAMESPACE_KEYS:
        value = service.get(key)
        if isinstance(value, str) and value.lower() in ("host", "container"):
            violations.append(_violation(
                "TASK_COMPOSE_HOST_NAMESPACE", file=file, service=name,
                detail=f"{key}: {value} 共享宿主/其他容器命名空间",
            ))
    cap_add = service.get("cap_add") or []
    if isinstance(cap_add, Sequence) and not isinstance(cap_add, str):
        dangerous = sorted(
            str(cap).upper() for cap in cap_add
            if str(cap).upper() in DANGEROUS_CAPABILITIES
        )
        if dangerous:
            violations.append(_violation(
                "TASK_COMPOSE_DANGEROUS_CAPABILITY", file=file, service=name,
                detail=f"cap_add 授予危险 capability: {dangerous}",
            ))
    for key in ("devices", "device_cgroup_rules"):
        if service.get(key):
            violations.append(_violation(
                "TASK_COMPOSE_DEVICE_EXPOSED", file=file, service=name,
                detail=f"{key} 把宿主设备暴露给任务容器: {service[key]}",
            ))
    security_opt = service.get("security_opt") or []
    if isinstance(security_opt, Sequence) and not isinstance(security_opt, str):
        disabled = sorted(
            str(item).replace(" ", "").lower() for item in security_opt
            if str(item).replace(" ", "").lower() in _DISABLING_SECURITY_OPTS
        )
        if disabled:
            violations.append(_violation(
                "TASK_COMPOSE_SECURITY_OPT_DISABLED", file=file, service=name,
                detail=f"security_opt 关闭了默认隔离: {disabled}",
            ))
    volumes = service.get("volumes") or []
    if isinstance(volumes, Sequence) and not isinstance(volumes, str):
        for entry in volumes:
            _check_volume(compose_dir, entry, file=file, service=name, violations=violations)
    if service.get("build") is not None:
        _check_build(compose_dir, service["build"], file=file, service=name, violations=violations)
    if service.get("env_file") is not None:
        _check_env_file(
            compose_dir, service["env_file"], file=file, service=name, violations=violations,
        )
    _check_credentials(service.get("environment"), file=file, service=name, violations=violations)
    extends = service.get("extends")
    if isinstance(extends, Mapping) and extends.get("file"):
        violations.append(_violation(
            "TASK_COMPOSE_INCLUDE_UNSUPPORTED", file=file, service=name,
            detail=f"extends.file 引用外部文件 {extends['file']}，无法证明其内容安全",
        ))


def _parse_compose(data: bytes) -> tuple[Mapping[str, Any] | None, str | None]:
    """解析 compose 文档；返回 ``(document, error_detail)``（不向调用方抛异常）。"""
    try:
        import yaml  # noqa: PLC0415 - 只在需要解析时导入
    except ImportError as error:  # pragma: no cover - 依赖缺失路径
        return (None, f"pyyaml 不可用，无法检查任务 compose: {error}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return (None, "compose 文件不是 UTF-8 文本")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return (None, f"compose 文件无法解析: {error}")
    if document is None:
        return ({}, None)
    if not isinstance(document, Mapping):
        return (None, f"compose 顶层必须是对象，实际是 {type(document).__name__}")
    return (document, None)


def task_compose_file_hashes(file_hashes: Mapping[str, str]) -> dict[str, str]:
    """冻结文件清单里该任务的 compose 文件（相对任务的 POSIX 路径 → sha256）。"""
    out: dict[str, str] = {}
    for relative, digest in file_hashes.items():
        parts = PurePosixPath(str(relative)).parts
        if len(parts) == 2 and parts[0] == TASK_ENVIRONMENT_DIR and parts[1] in COMPOSE_FILE_NAMES:
            out[str(relative)] = str(digest)
    return dict(sorted(out.items()))


def has_task_compose(file_hashes: Mapping[str, str]) -> bool:
    """任务是否自带 Harbor 会加载的 compose（``environment/docker-compose.yaml``）。"""
    return f"{TASK_ENVIRONMENT_DIR}/{COMPOSE_FILE_NAME}" in file_hashes


def _policy(files: list[dict[str, Any]], services: list[str], violations: list[dict[str, Any]]) -> dict[str, Any]:
    policy = {
        "schema": POLICY_SCHEMA,
        "harbor_compose_file_name": COMPOSE_FILE_NAME,
        "files": sorted(files, key=lambda item: item["relative_path"]),
        "services": sorted(set(services)),
        "violations": violations,
    }
    return {**policy, "policy_hash": canonical_hash(policy)}


def uninspectable_fact(detail: str) -> dict[str, Any]:
    """无法完成检查时的失败事实（供 prepare 捕获异常后如实登记）。"""
    return _policy([], [], [_violation("TASK_COMPOSE_UNINSPECTABLE", file="environment", detail=detail)])


def inspect_task_compose(
    root: Path | str,
    normalized_relative_path: str,
    *,
    file_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """只读检查一个任务实际提供的 compose，返回可序列化的策略事实。

    ``file_hashes`` 是冻结清单（``{相对任务的路径: sha256}``）：给了就以清单
    为准（清单说没有 compose 而目录里多出一份，不会因为"没列进去"而被跳过，
    因为 Harbor 是照目录读的），并就地核对读到的字节与冻结 hash 一致。
    """
    relative_dir = normalize_relative_path(normalized_relative_path)
    task_dir = Path(root).resolve().joinpath(*relative_dir.split("/"))
    compose_dir = task_dir / TASK_ENVIRONMENT_DIR
    frozen = dict(file_hashes or {})
    declared = sorted(task_compose_file_hashes(frozen))
    candidates = declared or sorted(
        f"{TASK_ENVIRONMENT_DIR}/{name}" for name in COMPOSE_FILE_NAMES
    )
    present: list[str] = []
    try:
        with TrustedDir(root, error_factory=_root_error) as trusted:
            present = [
                rel for rel in candidates
                if _regular_file_present(trusted, f"{relative_dir}/{rel}")
            ]
    except (TrustedPathError, HarborTaskError) as error:
        return uninspectable_fact(f"受控根不可读，无法检查任务 compose: {error}")

    violations: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    services: list[str] = []
    if candidates and not present:
        # 冻结清单/目录声明了 compose，但受控读取拿不到：不可检查，拒绝。
        return uninspectable_fact(
            f"任务 compose 存在但不可受控读取（symlink/超限/缺失）: {candidates}",
        )
    for rel in present:
        data = _read_task_file(root, f"{relative_dir}/{rel}", violations, rel)
        if data is None:
            continue
        digest = _hash_bytes(data)
        expected = frozen.get(rel)
        if expected is not None and expected != digest:
            violations.append(_violation(
                "TASK_COMPOSE_UNINSPECTABLE", file=rel,
                detail=(
                    "compose 字节与冻结清单不一致，检查结果不可信: "
                    f"frozen={expected} observed={digest}"
                ),
            ))
            continue
        files.append({
            "relative_path": rel, "sha256": digest, "size_bytes": len(data),
            "loaded_by_harbor": rel == f"{TASK_ENVIRONMENT_DIR}/{COMPOSE_FILE_NAME}",
        })
        document, error = _parse_compose(data)
        if error is not None or document is None:
            violations.append(_violation(
                "TASK_COMPOSE_UNINSPECTABLE", file=rel, detail=error or "无法解析",
            ))
            continue
        if document.get("include"):
            violations.append(_violation(
                "TASK_COMPOSE_INCLUDE_UNSUPPORTED", file=rel,
                detail="顶层 include 引用外部 compose 文件，无法证明其内容安全",
            ))
        if "services" not in document:
            violations.append(_violation(
                "TASK_COMPOSE_UNINSPECTABLE", file=rel,
                detail="compose 没有 services 段，无法确认任务容器的真实配置",
            ))
            continue
        declared_services = document.get("services")
        if not isinstance(declared_services, Mapping):
            violations.append(_violation(
                "TASK_COMPOSE_UNINSPECTABLE", file=rel, detail="services 段必须是对象",
            ))
            continue
        services.extend(str(name) for name in declared_services)
        for name in sorted(declared_services, key=str):
            _check_service(
                compose_dir, str(name), declared_services[name], file=rel, violations=violations,
            )
        networks = document.get("networks")
        if isinstance(networks, Mapping) and "host" in {str(key) for key in networks}:
            violations.append(_violation(
                "TASK_COMPOSE_HOST_NETWORK", file=rel,
                detail="compose 声明了 host network（external host network）",
            ))
    return _policy(files, services, violations)


def compose_violation_codes(facts: Mapping[str, Any] | None) -> list[str]:
    """``facts.compose`` 里的原因码（保持出现顺序，去重）。"""
    codes: list[str] = []
    for item in (facts or {}).get("violations") or []:
        code = str(item.get("code") if isinstance(item, Mapping) else item)
        if code and code not in codes:
            codes.append(code)
    return codes


def _root_error(message: str) -> Exception:
    return HarborTaskError("TASK_PATH_ESCAPE", message)


def _regular_file_present(trusted: TrustedDir, rel: str) -> bool:
    """受控根下是否存在该常规文件（symlink/目录/缺失都返回 False）。"""
    try:
        trusted.read_bytes(rel, max_bytes=MAX_COMPOSE_BYTES)
    except TrustedPathError:
        return False
    return True


def _read_task_file(
    root: Path | str, rel: str, violations: list[dict[str, Any]], display: str,
) -> bytes | None:
    try:
        with TrustedDir(root, error_factory=_root_error) as trusted:
            return trusted.read_bytes(rel, max_bytes=MAX_COMPOSE_BYTES)
    except (TrustedPathError, HarborTaskError) as error:
        violations.append(_violation(
            "TASK_COMPOSE_UNINSPECTABLE", file=display,
            detail=f"compose 文件不可受控读取（symlink/超限/缺失）: {error}",
        ))
        return None
