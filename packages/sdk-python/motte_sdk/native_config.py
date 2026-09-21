"""Controlled CLI configuration/auth boundary; evidence never includes credentials.

Native managed/system configuration is not fully observable. Isolation and candidate
hashes improve reproducibility, but never prove which resources a native CLI loaded.
Strict runners therefore reject these partial snapshots before process startup.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from motte_harness.process import minimal_env

from .execution_backends import ExecutionBackendError

_AUTH = {'claude-cli': {'ANTHROPIC_API_KEY'}, 'codex-cli': {'CODEX_API_KEY'},
         'codex-app-server': {'CODEX_API_KEY'}}


def validate_native_policy(backend: str, refs: list[str]) -> None:
    if not isinstance(refs, list) or any(ref not in _AUTH.get(backend, set()) for ref in refs):
        raise ExecutionBackendError('RUNTIME_CREDENTIALS_UNRESOLVED',
                                    f'{backend}: unsupported credential reference')
    policy = os.environ.get('MOTTE_RUNTIME_REPRODUCIBILITY', 'partial')
    if policy not in ('partial', 'strict'):
        raise ExecutionBackendError('RUNTIME_REPRODUCIBILITY_INVALID', 'unknown reproducibility policy')
    if policy == 'strict':
        raise ExecutionBackendError('RUNTIME_REPRODUCIBILITY_PARTIAL',
                                    'strict reproducibility rejects unobserved native managed configuration')


def resolve_credential_env(backend: str, refs: list[str]) -> dict[str, str]:
    validate_native_policy(backend, refs)
    values = {}
    for ref in refs:
        value = os.environ.get(ref)
        if not value or not value.strip():
            raise ExecutionBackendError('RUNTIME_CREDENTIALS_UNRESOLVED',
                                        f'{backend}: credential reference {ref} is unavailable')
        values[ref] = value
    return values


def _file_hash(path: Path, *, max_bytes: int = 1_000_000, timeout: float = 1) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        expires = monotonic() + timeout
        seen = 0
        with path.open('rb') as stream:
            while chunk := stream.read(65536):
                seen += len(chunk)
                if seen > max_bytes or monotonic() > expires:
                    return None
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _discovery(workspace: Path) -> tuple[list[dict[str, Any]], bool]:
    files: dict[str, dict[str, Any]] = {}
    truncated = False
    visits = 0
    expires = monotonic() + 2
    hashed_bytes = 0
    for directory in [workspace, *workspace.parents]:
        candidates = [directory / name for name in ('AGENTS.md', 'CLAUDE.md', '.mcp.json')]
        for name in ('.codex', '.claude', '.agents'):
            root = directory / name
            if root.is_symlink() or not root.is_dir():
                continue
            # Only known configuration/resource trees, never unrelated home files.
            pending = [(root, 0)]
            while pending and not truncated:
                base, depth = pending.pop()
                try:
                    with os.scandir(base) as entries:
                        for entry in entries:
                            visits += 1
                            if visits > 2048 or monotonic() > expires:
                                truncated = True
                                break
                            path = Path(entry.path)
                            if path.is_symlink() or path.is_junction():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if depth < 8 and entry.name not in ('sessions', 'logs', 'projects', 'cache', 'plugins'):
                                    pending.append((path, depth + 1))
                                continue
                            if entry.name in ('auth.json', '.credentials.json') or path.suffix not in ('.md', '.json', '.toml', '.rules'):
                                continue
                            candidates.append(path)
                            if len(candidates) >= 512:
                                truncated = True
                                break
                except OSError:
                    truncated = True
        for path in candidates:
            if monotonic() > expires or hashed_bytes >= 4_000_000:
                return list(files.values()), True
            if path.is_symlink():
                files[str(path)] = {'path': str(path), 'sha256': None, 'reason': 'symlink_not_followed'}
            elif path.is_file():
                if len(files) >= 512:
                    return list(files.values()), True
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 1_000_001
                digest = _file_hash(path, max_bytes=min(1_000_000, 4_000_000 - hashed_bytes))
                hashed_bytes += min(size, 1_000_000)
                files[str(path)] = {'path': str(path), 'sha256': digest}
                truncated = truncated or digest is None
        if truncated:
            return list(files.values()), True
    return list(files.values()), truncated


def _git_state(workspace: Path) -> dict[str, Any]:
    from motte_harness.supervisor import SupervisedLimits, SupervisedProcess

    def read(args: list[str]) -> bytes | None:
        try:
            env = minimal_env({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                               'GIT_ATTR_NOSYSTEM': '1', 'GIT_OPTIONAL_LOCKS': '0'})
            env.pop('HOME', None)
            process = SupervisedProcess(['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                                         '-C', str(workspace), *args], cwd=str(workspace), env=env,
                limits=SupervisedLimits(total_timeout=5, idle_timeout=5,
                                       max_line_bytes=65536, max_total_bytes=262144))
            process.start()
            result = process.wait()
            return result.stdout.encode('utf-8') if (
                result.status == 'exited' and result.exit_code == 0
                and not result.truncated and not result.residual_pids
            ) else None
        except (OSError, RuntimeError):
            return None

    revision = read(['rev-parse', 'HEAD'])
    names = read(['config', '--includes', '--list', '--name-only']) if revision else None
    # Working-tree diff/status can run clean/process filters even with external
    # diff and textconv disabled. No config output also means unknown, not safe.
    safe_worktree = names is not None and not any(
        key.lower().startswith(b'filter.') for key in names.splitlines()
    )
    patch = read(['diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--']) if revision and safe_worktree else None
    status = read(['status', '--porcelain', '--untracked-files=all']) if revision and safe_worktree else None
    return {'revision': revision.decode('ascii', errors='replace').strip() if revision else None,
            'dirty_patch_sha256': hashlib.sha256(patch).hexdigest() if patch is not None else None,
            'status_sha256': hashlib.sha256(status).hexdigest() if status is not None else None,
            'worktree_hash_unavailable': None if safe_worktree else 'git_filters_or_config_unknown',
            'untracked_contents_captured': False}


@dataclass
class NativeEnvironment:
    env: dict[str, str] = field(repr=False)
    evidence: dict[str, Any]
    _owned_home: Any = field(repr=False)

    def close(self) -> None:
        self._owned_home.cleanup()

    def retain(self) -> None:
        # Unconfirmed process ownership: do not delete a still-used home at GC.
        self._owned_home._finalizer.detach()

    def __enter__(self) -> NativeEnvironment:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def prepare_native_environment(backend: str, refs: list[str], *, workspace: Path,
                               binary: str, settings: dict[str, Any]) -> NativeEnvironment:
    auth = resolve_credential_env(backend, refs)
    owner = tempfile.TemporaryDirectory(prefix='motte-native-')
    home = Path(owner.name)
    config = home / ('claude' if backend == 'claude-cli' else 'codex')
    config.mkdir()
    env = minimal_env({**auth, 'HOME': str(home), 'USERPROFILE': str(home),
                       'XDG_CONFIG_HOME': str(home / 'xdg'),
                       'CLAUDE_CONFIG_DIR': str(config), 'CODEX_HOME': str(config)})
    resolved = Path(shutil.which(binary) or binary).absolute()
    discovery, truncated = _discovery(workspace)
    evidence = {
        'schema_version': 1, 'backend': backend, 'cwd': str(workspace),
        'native_home': str(home), 'home_isolated': True,
        'binary': {'path': str(resolved), 'sha256': _file_hash(resolved, max_bytes=512_000_000, timeout=3)},
        'requested_settings_sha256': _settings_hash(settings),
        'credentials': [{'ref': ref, 'present': True} for ref in refs],
        'env_allowlist': sorted(env), 'git': _git_state(workspace),
        'discovery_candidates': discovery, 'discovery_truncated': truncated,
        'discovery_loaded': None, 'reproducibility': 'partial',
        'unknown': ['native_managed_settings', 'loaded_rules_skills_mcp_plugins',
                    'launcher_indirect_binary', 'git_untracked_contents'],
    }
    return NativeEnvironment(env, evidence, owner)


def _settings_hash(settings: dict[str, Any]) -> str:
    import json

    from motte_trace.redaction import redact_secrets

    return hashlib.sha256(json.dumps(redact_secrets(settings), sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()
