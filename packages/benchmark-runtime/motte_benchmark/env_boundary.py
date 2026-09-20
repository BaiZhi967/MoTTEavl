"""Runner 进程环境的显式边界（M3 review R2-05）。

问题（review 原文）：Compose 的 ``environment: [KEY]`` / ``KEY: null`` 是"把
compose 进程的宿主同名变量传进容器"；Harbor 0.23.0 的
``_run_docker_compose_command`` 用 ``_compose_env_vars(include_os_env=True)``
把 **Runner 的整个 os.environ** 交给 ``docker compose``，而平台的
``ProcessJobAdapter`` 又把宿主环境整体继承给 Runner。两层都不设边界时，
"宿主里任何名字能被任务猜到的变量"都可能在任务容器里出现。

本模块把"哪些变量会下发"变成显式、可核验的事实，分两层：

1. **platform → Runner**（``plan_process_env``）：子进程只继承
   - ``RUNNER_ENV_ALLOWLIST``/``RUNNER_ENV_PREFIXES``：Runner 自己运行必需的
     非秘密变量（PATH/HOME/DOCKER_*/代理/CA/Python 解释器等）；
   - 冻结配置里声明的凭据引用（``{"ref": "env:NAME"}``，profile 或 runner
     配置里都算）：**可信 Agent 凭据**——真实 Agent 需要它，平台允许它存在；
   - 平台显式注入项（受控 ``extra_env`` + 每次启动换发的桥接身份
     ``MOTTE_JOB_ID``/``MOTTE_RUN_ID``/``MOTTE_LAUNCH_TOKEN`` 等）。
   其余宿主变量（平台数据库 DSN、别的厂商 key、任意宿主秘密）一律不下发。
2. **Runner → compose**（观察记录，见 ``harbor/entry.py``）：Runner 在启动
   Harbor 之前把实际可见的变量名与声明的凭据名写进
   ``harbor/env-boundary.json``，平台把该记录当证据冻结（``env_boundary``）。

判定原则：凭据类变量名（``looks_like_credential``）**必须**能追溯到一份声明
（``env:NAME`` 引用）；追溯不到的凭据类变量视为边界失效，启动前具名拒绝
（``RUNNER_ENV_BOUNDARY_VIOLATION``），而不是"发下去再说"。

记录只包含变量名与来源，绝不包含值。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from motte_benchmark.protocol import BenchmarkRuntimeError

ENV_BOUNDARY_SCHEMA = "motte-runner-env-boundary@1"
#: 平台→Runner 边界的名字（记录里区分两层）。
PLATFORM_BOUNDARY = "platform-to-runner"
#: Runner→compose 边界的名字。
COMPOSE_BOUNDARY = "runner-to-compose"

#: 允许从平台进程环境继承给 Runner 子进程的非秘密变量（其余一律不下发）。
RUNNER_ENV_ALLOWLIST: tuple[str, ...] = (
    # 进程运行必需。
    "PATH", "HOME", "SHELL", "USER", "LOGNAME", "TERM", "TZ", "PWD",
    "TMPDIR", "TEMP", "TMP",
    # Python 解释器（Runner 与它的 wrapper 都是 python 进程）。
    "PYTHONPATH", "PYTHONHOME", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED", "VIRTUAL_ENV",
    # Docker/容器运行时客户端（Runner 必须能连 daemon、构建镜像、拉取基础镜像）。
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY", "DOCKER_API_VERSION",
    # 代理与 CA（拉镜像、装 Agent CLI 需要；这些值在部署上是网络配置，不是凭据）。
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy", "ftp_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # XDG/SSH 运行时目录（docker CLI 与 git 任务来源）。
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "SSH_AUTH_SOCK",
)
#: 允许继承的变量名前缀（locale 一族；不含 MOTTE_/厂商前缀）。
RUNNER_ENV_PREFIXES: tuple[str, ...] = ("LC_",)
#: 记录里"其他被丢弃变量"的计数键（不逐个列出宿主变量名，避免把宿主环境铺进证据）。
DROPPED_OTHER_COUNT = "dropped_other_count"


def looks_like_credential(name: str) -> bool:
    """变量名是否像凭据（与 ``harbor.compose`` 同一判定，这里避免硬依赖）。"""
    from motte_benchmark.harbor.compose import looks_like_credential as _looks  # noqa: PLC0415

    return bool(_looks(name))


def looks_like_host_secret(name: str) -> bool:
    """宿主秘密的判定口径（比"凭据"更贴近本边界要防的东西）。

    除了厂商前缀与凭据类标记，还包括显式登记的平台内部变量名
    （``MOTTE_PG_DSN``/``DATABASE_URL``/``ARTIFACT_ROOT`` 等）。**不**把
    ``MOTTE_`` 这个泛前缀算作凭据：``MOTTE_*`` 是平台自己的命名空间，平台
    受控注入的非秘密配置（如 fake runner 模式）不该因此被判定为凭据通道。
    """
    from motte_benchmark.harbor.compose import (  # noqa: PLC0415 - 避免模块级耦合
        CREDENTIAL_NAME_MARKERS,
        FORBIDDEN_ENV_NAMES,
        FORBIDDEN_ENV_PREFIXES,
    )

    upper = name.upper()
    if upper in FORBIDDEN_ENV_NAMES:
        return True
    if any(
        upper.startswith(prefix) for prefix in FORBIDDEN_ENV_PREFIXES if prefix != "MOTTE_"
    ):
        return True
    return any(marker in upper for marker in CREDENTIAL_NAME_MARKERS)


def declared_env_refs(*payloads: Any, max_depth: int = 12) -> tuple[str, ...]:
    """从冻结配置里收集 ``env:NAME`` 凭据引用（不 import 具体 benchmark 的 schema）。

    profile 与 runner 配置都可能带引用：Harbor 用 ``{"credentials": {"k":
    {"ref": "env:NAME"}}}``，OpenCompass 把引用写在 runner 配置里，冻结的
    Harbor 配置里则是 ``{"k": "env:NAME"}}`` 字符串。两种形态这里都按
    "任意字符串以 ``env:`` 开头即引用" 收集，值本身永远不会被读取。
    """
    names: set[str] = set()

    def _walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, str):
            if node.startswith("env:") and len(node) > len("env:"):
                names.add(node[len("env:"):])
            return
        if isinstance(node, Mapping):
            for value in node.values():
                _walk(value, depth + 1)
            return
        if isinstance(node, (list, tuple)):
            for value in node:
                _walk(value, depth + 1)

    for payload in payloads:
        _walk(payload, 0)
    return tuple(sorted(names))


def plan_process_env(
    environ: Mapping[str, str], *,
    declared: Iterable[str] = (),
    injected: Mapping[str, str] | None = None,
    identity: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """平台进程环境 → Runner 子进程环境 ``(child_env, record)``。

    ``declared`` 是凭据引用解析出的变量名；``injected`` 是平台受控注入
    （``extra_env``，按契约只放非秘密值）；``identity`` 是每次启动换发的桥接
    身份（job/run/work_dir/launch token——平台自己生成，不是宿主凭据）。
    返回的记录只含变量名与来源分类。
    """
    declared_names = tuple(sorted({str(name) for name in declared if name}))
    declared_set = set(declared_names)
    injected_values = {str(key): str(value) for key, value in (injected or {}).items()}
    identity_values = {str(key): str(value) for key, value in (identity or {}).items()}

    child: dict[str, str] = {}
    forwarded: dict[str, str] = {}
    dropped_credentials: list[str] = []
    dropped_other = 0
    for name, value in environ.items():
        name = str(name)
        if name in RUNNER_ENV_ALLOWLIST:
            child[name] = value
            forwarded[name] = "allowlist"
        elif any(name.startswith(prefix) for prefix in RUNNER_ENV_PREFIXES):
            child[name] = value
            forwarded[name] = "allowlist-prefix"
        elif name in declared_set:
            child[name] = value
            forwarded[name] = "declared-credential"
        elif looks_like_host_secret(name):
            # 宿主秘密，但没有声明：不下发，并如实登记名字供审计。
            dropped_credentials.append(name)
        else:
            dropped_other += 1
    for name, value in injected_values.items():
        child[name] = value
        forwarded[name] = "injected"
    for name, value in identity_values.items():
        child[name] = value
        forwarded[name] = "bridge-identity"

    record: dict[str, Any] = {
        "schema": ENV_BOUNDARY_SCHEMA,
        "boundary": PLATFORM_BOUNDARY,
        "declared_credentials": list(declared_names),
        "forwarded": dict(sorted(forwarded.items())),
        "dropped_credentials": sorted(dropped_credentials),
        DROPPED_OTHER_COUNT: dropped_other,
        "compose_env_source": "runner-os-environ",
        "note": (
            "Harbor 0.23.0 把 Runner 的 os.environ 原样交给 docker compose"
            "（_compose_env_vars(include_os_env=True)），因此这份名单就是 compose"
            "插值/透传能看到的变量全集"
        ),
    }
    record["violations"] = env_boundary_violations(record)
    return (child, record)


def env_boundary_violations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """边界记录里"无法解释的凭据类变量"（应当为空；非空即拒绝启动）。

    允许的来源：白名单（非秘密）、声明的凭据引用、平台本次换发的桥接身份。
    ``extra_env`` 注入同样必须能追溯到声明——否则等于绕过引用机制把凭据塞进
    Runner 环境（``extra_env`` 的契约是只放非秘密值）。
    """
    declared = {str(name) for name in record.get("declared_credentials") or []}
    forwarded = record.get("forwarded")
    forwarded = forwarded if isinstance(forwarded, Mapping) else {}
    violations: list[dict[str, Any]] = []
    for name, source in sorted(forwarded.items()):
        name = str(name)
        source = str(source)
        if source in ("allowlist", "allowlist-prefix"):
            # 白名单是固定常量；这里再断言一次，防止未来编辑把它变成凭据通道。
            if looks_like_host_secret(name):
                violations.append({
                    "code": "RUNNER_ENV_BOUNDARY_ALLOWLIST_UNEXPECTED",
                    "name": name,
                    "source": source,
                    "detail": f"白名单变量 {name} 形似凭据，边界常量不应包含它",
                })
            continue
        if source == "bridge-identity":
            # 桥接身份由平台本次启动生成（含换发的 launch token），不是宿主凭据。
            continue
        if name not in declared and looks_like_host_secret(name):
            violations.append({
                "code": "RUNNER_ENV_UNDECLARED_CREDENTIAL",
                "name": name,
                "source": source,
                "detail": (
                    f"凭据类变量 {name} 以 {source} 方式进入 Runner 环境，"
                    "但冻结配置里没有对应的 env:NAME 引用"
                ),
            })
    return violations


def verify_process_env_boundary(record: Mapping[str, Any]) -> None:
    """边界记录不合法时具名拒绝（启动前调用，绝不产生执行副作用）。"""
    violations = list(record.get("violations") or [])
    if not violations:
        return
    detail = "; ".join(
        f"{item.get('code')}: {item.get('detail')}" for item in violations
    )
    raise BenchmarkRuntimeError(
        "RUNNER_ENV_BOUNDARY_VIOLATION",
        f"runner environment boundary refused: {detail}",
    )


def runner_env_outside_boundary(record: Mapping[str, Any]) -> list[str]:
    """Runner 上报的可见变量名里，平台下发名单之外的名字（信息性，不判失败）。

    真实启动链上还会有 wrapper 自己产生的变量（``SHLVL``/``PWD``/脚本局部变量）：
    它们由启动脚本而非平台下发，是否安全由下面的
    ``runner_env_observation_violations`` 判定——边界要拒绝的是"未声明的凭据类
    变量"，而不是"多了一个无害的 shell 变量"。
    """
    declared = {str(name) for name in record.get("declared_credentials") or []}
    bridge = {str(name) for name in record.get("bridge_env_names") or []}
    outside: list[str] = []
    for raw in record.get("runner_env_names") or []:
        name = str(raw)
        if name in RUNNER_ENV_ALLOWLIST or name in declared or name in bridge:
            continue
        if any(name.startswith(prefix) for prefix in RUNNER_ENV_PREFIXES):
            continue
        outside.append(name)
    return sorted(outside)


def runner_env_observation_violations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Runner 上报的"compose 可见变量名"里是否出现未声明的凭据类变量。

    ``harbor/env-boundary.json`` 由 Runner 在启动 Harbor 之前写出；平台把它
    当证据冻结。判定只看安全相关的那一类：**凭据类变量名必须能追溯到声明**
    （``env:NAME`` 引用或平台本次换发的桥接身份）。
    """
    if record.get("boundary") != COMPOSE_BOUNDARY:
        return [{
            "code": "RUNNER_ENV_BOUNDARY_SCHEMA",
            "detail": f"unexpected boundary record: {record.get('boundary')!r}",
        }]
    declared = {str(name) for name in record.get("declared_credentials") or []}
    bridge = {str(name) for name in record.get("bridge_env_names") or []}
    violations: list[dict[str, Any]] = []
    for name in runner_env_outside_boundary(record):
        if name in declared or name in bridge or not looks_like_host_secret(name):
            continue
        violations.append({
            "code": "RUNNER_ENV_UNDECLARED_CREDENTIAL",
            "name": name,
            "detail": (
                f"Runner 进程环境里的 {name} 形似凭据，但冻结配置里没有对应的"
                " env:NAME 引用，也不是平台本次注入的桥接身份"
            ),
        })
    return violations
