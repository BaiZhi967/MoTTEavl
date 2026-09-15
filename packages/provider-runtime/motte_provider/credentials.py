"""本地凭据文件：~/.motte/credentials.toml（0600），密钥绝不进入 DB/trace/报告。

解析优先级：显式传参 > 凭据文件 profile > 环境变量（api_key_env，legacy 回退）。
读取用 stdlib tomllib；写入只处理本模块管理的扁平结构（[profile] + 字符串值），
路径可用 MOTTE_CREDENTIALS_PATH 覆盖（容器场景挂载卷）。
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

_ENV_OVERRIDE = "MOTTE_CREDENTIALS_PATH"


def default_credentials_path() -> Path:
    return Path.home() / ".motte" / "credentials.toml"


def credentials_path() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    return Path(override).expanduser() if override else default_credentials_path()


def load_credentials(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """解析凭据文件；权限宽于属主读写时向 stderr 警告（不阻断读取）。"""
    target = path or credentials_path()
    if not target.exists():
        return {}
    _warn_insecure_permissions(target)
    with target.open("rb") as handle:
        data = tomllib.load(handle)
    return {name: dict(section) for name, section in data.items() if isinstance(section, dict)}


def resolve_api_key(
    profile: str | None = None,
    *,
    explicit: str | None = None,
    env_name: str | None = None,
    path: Path | None = None,
) -> str | None:
    """密钥解析链：explicit > 凭据文件 [profile].api_key > 环境变量。"""
    if explicit is not None:
        return explicit or None
    if profile:
        section = load_credentials(path).get(profile) or {}
        key = section.get("api_key")
        if isinstance(key, str) and key:
            return key
    if env_name:
        return os.environ.get(env_name) or None
    return None


def save_api_key(profile: str, api_key: str, path: Path | None = None) -> Path:
    if not profile or not api_key:
        raise ValueError("profile and api_key are required")
    target = path or credentials_path()
    profiles = load_credentials(target)
    profiles.setdefault(profile, {})["api_key"] = api_key
    _write_toml(target, profiles)
    return target


def remove_profile(profile: str, path: Path | None = None) -> bool:
    target = path or credentials_path()
    profiles = load_credentials(target)
    if profile not in profiles:
        return False
    del profiles[profile]
    _write_toml(target, profiles)
    return True


def mask(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"


def _write_toml(target: Path, profiles: dict[str, dict[str, Any]]) -> None:
    """原子写出模块自管的扁平 TOML，并收紧为 0600。"""
    lines = ["# managed by: python -m motte_cli credentials", ""]
    for name in sorted(profiles):
        lines.append(f"[{_toml_string(name)}]")
        for key, value in profiles[name].items():
            lines.append(f"{_toml_string(str(key))} = {_toml_string(str(value))}")
        lines.append("")
    tmp = target.with_name(target.name + ".tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _warn_insecure_permissions(target: Path) -> None:
    if os.name == "nt":
        return
    mode = target.stat().st_mode & 0o777
    if mode & 0o077:
        print(
            f"警告：{target} 权限为 {oct(mode)}，其他用户可读；建议 chmod 600",
            file=sys.stderr,
        )
