import json

import pytest

from motte_sdk.execution_backends import ExecutionBackendError


def test_only_explicit_allowed_auth_reference_enters_isolated_child(tmp_path, monkeypatch):
    from motte_sdk.native_config import prepare_native_environment

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-provider-secret')
    monkeypatch.setenv('UNRELATED_SECRET', 'never-inherit')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with prepare_native_environment('claude-cli', ['ANTHROPIC_API_KEY'], workspace=workspace,
                                    binary=__file__, settings={}) as native:
        assert native.env['ANTHROPIC_API_KEY'] == 'synthetic-provider-secret'
        assert 'UNRELATED_SECRET' not in native.env
        assert native.env['HOME'] != str(tmp_path)
        assert native.env['CLAUDE_CONFIG_DIR'].startswith(native.env['HOME'])
        assert 'synthetic-provider-secret' not in json.dumps(native.evidence)
        assert native.evidence['binary']['sha256']
        assert native.evidence['reproducibility'] == 'partial'
        assert native.evidence['credentials'] == [{'ref': 'ANTHROPIC_API_KEY', 'present': True}]


def test_unknown_missing_auth_and_strict_policy_fail_before_spawn(tmp_path, monkeypatch):
    from motte_sdk.native_config import prepare_native_environment, validate_native_policy

    with pytest.raises(ExecutionBackendError, match='credential'):
        validate_native_policy('codex-cli', ['UNRELATED_SECRET'])
    monkeypatch.delenv('CODEX_API_KEY', raising=False)
    with pytest.raises(ExecutionBackendError, match='credential'):
        prepare_native_environment('codex-cli', ['CODEX_API_KEY'], workspace=tmp_path,
                                   binary=__file__, settings={})
    monkeypatch.setenv('MOTTE_RUNTIME_REPRODUCIBILITY', 'strict')
    with pytest.raises(ExecutionBackendError, match='reproducibility'):
        validate_native_policy('codex-cli', [])


def test_workspace_discovery_hash_changes_without_disclosing_contents(tmp_path):
    from motte_sdk.native_config import prepare_native_environment

    rule = tmp_path / 'AGENTS.md'
    rule.write_text('private instruction', encoding='utf-8')
    with prepare_native_environment('codex-cli', [], workspace=tmp_path,
                                    binary=__file__, settings={}) as native:
        first = native.evidence
    rule.write_text('changed instruction', encoding='utf-8')
    with prepare_native_environment('codex-cli', [], workspace=tmp_path,
                                    binary=__file__, settings={}) as native:
        second = native.evidence
    assert first['discovery_candidates'] != second['discovery_candidates']
    assert 'private instruction' not in json.dumps(first)
    assert first['discovery_loaded'] is None


@pytest.mark.parametrize('backend,ref', [('claude-cli', 'ANTHROPIC_API_KEY'),
                                        ('codex-cli', 'CODEX_API_KEY')])
def test_dispatch_consumes_auth_and_freezes_redacted_effective_config(tmp_path, monkeypatch, backend, ref):
    from motte_storage.artifacts import ArtifactStore
    from tests.integration.test_external_runtime_slice import _cli_manifest, _dispatch, _fake_cli

    secret = 'synthetic-credential-never-persist'
    monkeypatch.setenv(ref, secret)
    monkeypatch.setenv('UNRELATED_SECRET', 'never-inherit')
    binary = _fake_cli(tmp_path, backend)
    binary.write_text(binary.read_text(encoding='utf-8') + (
        '\nimport os\n'
        f"assert os.environ[{ref!r}] == {secret!r}\n"
        "assert 'UNRELATED_SECRET' not in os.environ\n"
        "assert os.environ['HOME'] != str(pathlib.Path.home().parent)\n"
        f"pathlib.Path('credential-echo.txt').write_text(os.environ[{ref!r}], encoding='utf-8')\n"
        f"pathlib.Path(os.environ[{ref!r}]).write_text('secret filename', encoding='utf-8')\n"
    ), encoding='utf-8')
    manifest = _cli_manifest(backend, binary)
    manifest['runtime_profile']['credential_refs'] = [ref]
    service, run = _dispatch(tmp_path, monkeypatch, backend, manifest, 'auth-boundary')
    assert run['status'] == 'completed'
    result = service.store.case_runs.list_for_run(run['id'])[0]['result']
    snapshot = result['agent']['runtime']['config_snapshot']
    assert snapshot['credentials'] == [{'ref': ref, 'present': True}]
    assert snapshot['home_isolated'] is True
    assert snapshot['binary']['sha256']
    assert secret not in json.dumps(result)
    artifact = next(item for item in result['observation']['artifact_refs'] if item['path'] == 'credential-echo.txt')
    assert artifact['redacted'] is True
    assert secret.encode() not in ArtifactStore(tmp_path / 'artifacts').read_bytes(artifact['artifact_id'])


def test_version_probe_does_not_inherit_auth_or_host_home(tmp_path, monkeypatch):
    from motte_sdk.cli_runtime import CliRuntimeCaseExecutor

    monkeypatch.setenv('CODEX_API_KEY', 'synthetic-secret')
    probe = tmp_path / 'probe.py'
    probe.write_text("import os\nassert 'CODEX_API_KEY' not in os.environ\nprint('0.155.1')\n", encoding='utf-8')
    executor = CliRuntimeCaseExecutor({'id': 'r'}, backend='codex-cli', binary=str(probe))
    assert executor._enforce_binary_version('0.155.1')['observed'] == '0.155.1'


def test_git_snapshot_does_not_execute_repository_diff_or_fsmonitor(tmp_path):
    import os
    import shlex
    import subprocess
    import sys

    from motte_sdk.native_config import _git_state

    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)

    git('init')
    git('config', 'user.name', 'test')
    git('config', 'user.email', 'test@example.invalid')
    (tmp_path / 'file.txt').write_text('before', encoding='utf-8')
    git('add', 'file.txt')
    git('commit', '-m', 'fixture')
    (tmp_path / 'file.txt').write_text('after', encoding='utf-8')
    script = tmp_path / 'external.py'
    marker = tmp_path / 'unexpected-program.txt'
    script.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n', encoding='utf-8')
    command = [sys.executable, str(script)]
    configured = subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)
    git('config', 'diff.external', configured)
    git('config', 'core.fsmonitor', configured)
    result = _git_state(tmp_path)
    assert result['revision']
    assert result['dirty_patch_sha256']
    assert not marker.exists()


def test_discovery_caps_large_files_and_large_directory_walk(tmp_path):
    from motte_sdk.native_config import _discovery

    rules = tmp_path / '.codex'
    rules.mkdir()
    (rules / 'large.md').write_bytes(b'x' * 1_000_001)
    entries, incomplete = _discovery(tmp_path)
    assert incomplete
    assert next(item for item in entries if item['path'].endswith('large.md'))['sha256'] is None
    (rules / 'large.md').unlink()
    for index in range(520):
        (rules / f'{index}.md').write_text('x', encoding='utf-8')
    entries, incomplete = _discovery(tmp_path)
    assert incomplete
    assert len(entries) <= 512


def test_git_worktree_filters_make_hash_unknown_without_execution(tmp_path):
    import subprocess
    import sys

    from motte_sdk.native_config import _git_state

    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)

    git('init')
    git('config', 'user.name', 'test')
    git('config', 'user.email', 'test@example.invalid')
    (tmp_path / 'tracked.txt').write_text('before', encoding='utf-8')
    git('add', 'tracked.txt')
    git('commit', '-m', 'fixture')
    marker = tmp_path / 'unexpected-filter.txt'
    (tmp_path / '.gitattributes').write_text('tracked.txt filter=probe', encoding='utf-8')
    git('config', 'filter.probe.clean', sys.executable)
    (tmp_path / 'tracked.txt').write_text(f'open({str(marker)!r}, "w").write("unexpected")', encoding='utf-8')
    state = _git_state(tmp_path)
    assert state['revision']
    assert state['dirty_patch_sha256'] is None
    assert state['status_sha256'] is None
    assert state['worktree_hash_unavailable'] == 'git_filters_or_config_unknown'
    assert not marker.exists()


@pytest.mark.parametrize('uncertain', [{'truncated': True}, {'residual_pids': [123]}])
def test_incomplete_git_config_never_allows_worktree_commands(tmp_path, monkeypatch, uncertain):
    from types import SimpleNamespace

    from motte_sdk.native_config import _git_state

    commands = []

    class Process:
        def __init__(self, argv, **kwargs):
            commands.append(argv)

        def start(self):
            pass

        def wait(self):
            result = dict(status='exited', exit_code=0, truncated=False, residual_pids=[],
                          stdout='a' * 40 if len(commands) == 1 else 'core.repositoryformatversion')
            if len(commands) == 2:
                result.update(uncertain)
            return SimpleNamespace(**result)

    monkeypatch.setattr('motte_harness.supervisor.SupervisedProcess', Process)
    state = _git_state(tmp_path)
    assert len(commands) == 2
    assert state['dirty_patch_sha256'] is None
    assert state['status_sha256'] is None
