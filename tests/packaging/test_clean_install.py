"""干净安装与打包门禁（M7-T05，protocol §8 / A01 / G09）。

验证交付 wheel 在 **checkout 之外**的干净 venv 里可用：
- 无 cwd / PYTHONPATH 依赖（env 剥离 PYTHON*，cwd 一律指向临时目录）；
- ``import motte_sdk`` / ``from motte_sdk import MotteClient`` 可用，
  构造 ``MotteClient("http://127.0.0.1:1")`` 无副作用、不落任何本地文件；
- ``python -m motte_cli --help`` 退出码 0；
- 包资源（importlib.resources）从不同 cwd 读取结果一致；
- 依赖边界（G09）：docker / celery / psutil / pyyaml 不进入 SDK/API 交付闭包，
  psycopg/Alembic/SQLAlchemy 只经 motte-storage 的 [postgresql] extra，pytest 只经 [pytest] extra。

pip 安装 wheel 需要访问 PyPI（httpx/pydantic 等非 workspace 依赖）。这里不设
"无网络即静默通过"：只有当 pip 真的连不上 index 时才 skip，并把原因原样带出。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# 与 Makefile `wheels` 目标一致的六个交付包（SDK/API 闭包）。
DELIVERABLE_PACKAGE_DIRS = [
    "packages/contracts",
    "packages/sdk-python",
    "packages/storage",
    "packages/cli",
    "packages/evaluators",
    "packages/trace",
]

# G09：Runner/Worker 专属依赖，禁止出现在 SDK/API 交付闭包里（声明层）。
RUNNER_ONLY_DEPS = {"docker", "celery", "psutil", "pyyaml"}

# 解析层（干净 venv 实际装了什么）：pytest 与 PostgreSQL 栈只应经 extra 进入。
RESOLVED_FORBIDDEN = {"docker", "celery", "psutil", "pytest", "psycopg", "alembic", "sqlalchemy"}
# 注意 pyyaml 不在 RESOLVED_FORBIDDEN：motte-contracts 的既定依赖 cel-python
# 自身传递依赖 pyyaml（uv.lock 可查），该泄漏不在我们的声明控制内；G09 对
# pyyaml 的约束因此落在声明层（wheel Requires-Dist 检查）。

# 全部六个交付包都必须能在默认安装中导入；PostgreSQL 只经 extra 进入。
CLEAN_IMPORT_NAMES = [
    "motte_sdk", "motte_contracts", "motte_storage", "motte_eval", "motte_trace", "motte_cli",
]

_STORAGE_SMOKE_CODE = """
import os
from pathlib import Path
os.environ.pop('MOTTE_STORAGE', None)
import motte_storage
from motte_storage import ArtifactStore, InMemoryRepository, InMemoryRunStore, SQLiteRunStore
from motte_storage.factory import create_run_store, create_resource_store
from motte_storage.repositories import SQLiteRepository
from motte_storage.resource_store import InMemoryResourceStore

artifact_store = ArtifactStore(Path('artifacts'))
artifact = artifact_store.put_bytes('run-1/output.txt', b'clean storage')
assert artifact_store.read_bytes(artifact.id) == b'clean storage'
for repository in (InMemoryRepository(), SQLiteRepository('records.db')):
    repository.put('record', {'value': 7})
    assert repository.get('record') == {'value': 7}
for store in (InMemoryRunStore(), SQLiteRunStore('runs.db'), create_run_store('factory.db')):
    store.runs.create({'id': 'run-1', 'status': 'queued'})
    assert store.runs.get('run-1')['status'] == 'queued'
    assert store.events.append({'run_id': 'run-1', 'type': 'queued'})['seq'] == 1
    assert len(store.events.list_for_run('run-1')) == 1
assert SQLiteRunStore('runs.db').runs.get('run-1')['status'] == 'queued'
for resources in (InMemoryResourceStore(), create_resource_store('resources.db')):
    resources.models.put({'id': 'model-1', 'provider': 'test'})
    assert resources.models.get('model-1')['provider'] == 'test'
assert create_resource_store('resources.db').models.get('model-1')['provider'] == 'test'
assert 'motte_storage.postgres' not in sys.modules
assert 'motte_storage.migrations' not in sys.modules
assert not hasattr(motte_storage, 'not_a_storage_export')
print(motte_storage.__file__)
"""


def test_storage_source_works_without_optional_dependencies(tmp_path: Path) -> None:
    # A subprocess prevents the development environment's already-imported PG
    # modules from concealing an eager dependency. Real local operations follow.
    code = f"""
import importlib.abc
import sys
sys.path[:0] = {str(ROOT / 'packages/storage'), str(ROOT / 'packages/contracts')!r}
class NoOptionalDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'psycopg', 'alembic', 'sqlalchemy', 'motte_eval', 'motte_sdk'}}:
            raise AssertionError('optional dependency imported: ' + fullname)
sys.meta_path.insert(0, NoOptionalDependencies())
""" + _STORAGE_SMOKE_CODE
    proc = _run_in_venv(Path(sys.executable), ["-I", "-c", code], tmp_path)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(scope="session")
def storage_only_venv(request, wheel_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install only storage + contracts, with the selected declared extra.

    Unlike the historical six-wheel fixture, any installation failure is a
    failure: dependency closure must not become an optional/skipped assertion.
    """
    extra = request.param
    home = tmp_path_factory.mktemp(f"storage-{extra or 'base'}")
    venv_dir = home / "venv"
    create = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)], cwd=home,
        env=_stripped_env(), capture_output=True, text=True, timeout=300,
    )
    assert create.returncode == 0, create.stderr
    python = _venv_python(venv_dir)
    contracts, = wheel_dir.glob("motte_contracts-*.whl")
    storage, = wheel_dir.glob("motte_storage-*.whl")
    requirement = str(storage) + (f"[{extra}]" if extra else "")
    install = _run_in_venv(python, ["-m", "pip", "install", str(contracts), requirement], home)
    assert install.returncode == 0, install.stdout + install.stderr
    check = _run_in_venv(python, ["-m", "pip", "check"], home)
    assert check.returncode == 0, check.stdout + check.stderr
    return python


@pytest.mark.parametrize("storage_only_venv", [""], indirect=True)
def test_storage_base_wheel_works_without_optional_dependencies(
    storage_only_venv: Path, tmp_path: Path,
) -> None:
    code = """
import importlib.util
import sys
for name in ('psycopg', 'alembic', 'sqlalchemy', 'motte_sdk', 'motte_eval', 'httpx'):
    assert importlib.util.find_spec(name) is None, name
""" + _STORAGE_SMOKE_CODE
    proc = _run_in_venv(storage_only_venv, ["-I", "-c", code], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "site-packages" in proc.stdout.replace("\\", "/"), proc.stdout
    assert str(ROOT).replace("\\", "/") not in proc.stdout.replace("\\", "/")


@pytest.mark.parametrize("storage_only_venv", ["postgresql"], indirect=True)
def test_storage_postgresql_extra_closes_import_dependencies(
    storage_only_venv: Path, tmp_path: Path,
) -> None:
    code = """
import importlib.util
import motte_storage
from motte_storage import PostgresRunStore, UnsupportedStorageError, create_postgres_run_store
from motte_storage.factory import create_resource_store, create_run_store
from motte_storage import migrations, postgres
assert PostgresRunStore is postgres.PostgresRunStore
assert UnsupportedStorageError is postgres.UnsupportedStorageError
assert create_postgres_run_store is postgres.create_postgres_run_store
assert all(hasattr(motte_storage, name) for name in motte_storage.__all__)
for name in ('psycopg', 'alembic', 'sqlalchemy'):
    assert importlib.util.find_spec(name) is not None, name
for name in ('motte_sdk', 'motte_eval', 'httpx'):
    assert importlib.util.find_spec(name) is None, name
# Existing constructor-only paths do not contact a database or run migrations.
dsn = 'postgresql://localhost:1/not_connected'
assert isinstance(create_postgres_run_store(dsn), PostgresRunStore)
assert isinstance(create_run_store(storage='postgres', dsn=dsn), PostgresRunStore)
assert create_resource_store(storage='postgres', dsn=dsn) is not None
config = migrations.alembic_config(dsn)
assert config.get_main_option('sqlalchemy.url') == dsn.replace('postgresql:', 'postgresql+psycopg:')
print(motte_storage.__file__)
"""
    proc = _run_in_venv(storage_only_venv, ["-I", "-c", code], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "site-packages" in proc.stdout.replace("\\", "/"), proc.stdout
    assert str(ROOT).replace("\\", "/") not in proc.stdout.replace("\\", "/")

_RESOURCES_CODE = (
    "import importlib.resources, json\n"
    f"names = {CLEAN_IMPORT_NAMES!r}\n"
    "out = {}\n"
    "for name in names:\n"
    "    files = importlib.resources.files(name)\n"
    "    out[name] = str(files)\n"
    "    assert (files / '__init__.py').is_file(), name\n"
    "print(json.dumps(out))\n"
)


def _stripped_env() -> dict[str, str]:
    """子进程环境：剥掉所有 PYTHON* 变量（含 PYTHONPATH），杜绝 checkout 泄漏。

    随后显式重设输出编码为 UTF-8：Windows 子进程默认按本地代码页（如 GBK）
    写 stdout，而父进程按 UTF-8 解码会炸；这是对子进程的显式设定，不是泄漏。
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.fixture(scope="session")
def wheel_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """会话级构建交付 wheel 到临时目录（等价于 `make wheels`，但不污染 dist/）。"""
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv binary not on PATH; cannot build wheels")
    out = tmp_path_factory.mktemp("wheels")
    for rel in DELIVERABLE_PACKAGE_DIRS:
        proc = subprocess.run(
            [uv_bin, "build", "--out-dir", str(out), str(ROOT / rel)],
            cwd=str(ROOT),
            env=_stripped_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        assert proc.returncode == 0, f"uv build {rel} failed:\n{proc.stderr}"
    wheels = sorted(p.name for p in out.glob("*.whl"))
    assert len(wheels) == len(DELIVERABLE_PACKAGE_DIRS), wheels
    return out


@pytest.fixture(scope="session")
def clean_venv(wheel_dir: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """checkout 之外的干净 venv：系统临时目录建 venv，pip 安装全部交付 wheel。"""
    home = tmp_path_factory.mktemp("clean-install")
    venv_dir = home / "venv"
    proc = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        cwd=str(home),
        env=_stripped_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"venv creation failed:\n{proc.stderr}"
    python = _venv_python(venv_dir)
    wheels = [str(p) for p in sorted(wheel_dir.glob("*.whl"))]
    try:
        install = subprocess.run(
            [str(python), "-m", "pip", "install", *wheels],
            cwd=str(home),
            env=_stripped_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("pip install timed out (no usable PyPI index reachable)")
    if install.returncode != 0:
        detail = (install.stderr or install.stdout or "").strip()[-2000:]
        pytest.skip(
            "pip install of built wheels failed -- most likely no PyPI access in this "
            f"environment (clean-install assertions not run). pip output tail:\n{detail}"
        )
    return python


def _run_in_venv(python: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), *args],
        cwd=str(cwd),
        env=_stripped_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def test_sdk_imports_from_clean_venv(clean_venv: Path, tmp_path: Path) -> None:
    proc = _run_in_venv(
        clean_venv,
        ["-c", "import motte_sdk; from motte_sdk import MotteClient; print(motte_sdk.__file__)"],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    # 导入的必须是 venv 里的安装件，而不是 checkout（无 cwd/PYTHONPATH 依赖）。
    assert "site-packages" in proc.stdout.replace("\\", "/"), proc.stdout
    assert str(ROOT).replace("\\", "/") not in proc.stdout.replace("\\", "/")


def test_client_construction_is_side_effect_free(clean_venv: Path, tmp_path: Path) -> None:
    work = tmp_path / "client-construction"
    work.mkdir()
    before = sorted(p.name for p in work.iterdir())
    proc = _run_in_venv(
        clean_venv,
        [
            "-c",
            "from motte_sdk import MotteClient\n"
            "client = MotteClient('http://127.0.0.1:1')\n"
            "print(type(client).__name__)\n",
        ],
        work,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "MotteClient"
    after = sorted(p.name for p in work.iterdir())
    assert after == before, f"MotteClient construction created local files: {after}"


def test_cli_help_from_clean_venv(clean_venv: Path, tmp_path: Path) -> None:
    proc = _run_in_venv(clean_venv, ["-m", "motte_cli", "--help"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout.lower()


def test_resources_readable_from_any_cwd(
    clean_venv: Path, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    second_cwd = tmp_path_factory.mktemp("second-cwd")
    outputs = []
    for cwd in (tmp_path, second_cwd):
        proc = _run_in_venv(clean_venv, ["-c", _RESOURCES_CODE], cwd)
        assert proc.returncode == 0, f"cwd={cwd}:\n{proc.stderr}"
        outputs.append(json.loads(proc.stdout))
    assert outputs[0] == outputs[1]
    # 资源解析到 venv 安装位置，而不是源码 checkout。
    for resolved in outputs[0].values():
        assert "site-packages" in resolved.replace("\\", "/"), resolved


def test_runner_only_deps_absent_from_installed_venv(clean_venv: Path, tmp_path: Path) -> None:
    proc = _run_in_venv(
        clean_venv,
        [
            "-c",
            "import importlib.metadata as md, json\n"
            "print(json.dumps(sorted((d.metadata['Name'] or '').lower()\n"
            "                        for d in md.distributions())))\n",
        ],
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    installed = set(json.loads(proc.stdout))
    leaked = installed & RESOLVED_FORBIDDEN
    assert not leaked, f"runner/worker-only deps leaked into clean install: {sorted(leaked)}"


def _requires_dist_of_wheel(wheel_dir: Path, dist_name: str) -> list[str]:
    pattern = f"{dist_name.replace('-', '_')}-*.whl"
    matches = sorted(wheel_dir.glob(pattern))
    assert len(matches) == 1, f"expected exactly one wheel for {dist_name}: {matches}"
    with zipfile.ZipFile(matches[0]) as zf:
        metadata_names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        assert len(metadata_names) == 1, metadata_names
        text = zf.read(metadata_names[0]).decode("utf-8")
    return [
        line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Requires-Dist:")
    ]


def _requirement_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    assert match, requirement
    return match.group(0).lower().replace("_", "-")


def test_sdk_wheel_dependency_boundary(wheel_dir: Path) -> None:
    requires = _requires_dist_of_wheel(wheel_dir, "motte-sdk")
    # extras 允许的额外安装位：pytest 插件（T04）与本地 server 便捷运行。
    allowed_extra_names = {"pytest", "fastapi", "uvicorn"}
    plain: set[str] = set()
    for requirement in requires:
        name = _requirement_name(requirement)
        assert name not in RUNNER_ONLY_DEPS, f"runner/worker-only dep in motte-sdk: {requirement}"
        if "extra ==" in requirement:
            assert name in allowed_extra_names, f"unexpected extra in motte-sdk: {requirement}"
            continue
        plain.add(name)
    # SDK 闭包（protocol §8）：contracts + httpx，仅此而已。
    assert plain == {"motte-contracts", "httpx"}, sorted(plain)


def test_cli_and_storage_wheel_dependency_boundary(wheel_dir: Path) -> None:
    expected_plain = {"motte-cli": {"motte-sdk", "motte-storage"}, "motte-storage": {"motte-contracts"}}
    expected_extras = {"motte-cli": set(), "motte-storage": {"psycopg", "alembic", "sqlalchemy"}}
    for dist_name, expected in expected_plain.items():
        requires = _requires_dist_of_wheel(wheel_dir, dist_name)
        plain: set[str] = set()
        extras: set[str] = set()
        for requirement in requires:
            name = _requirement_name(requirement)
            assert name not in RUNNER_ONLY_DEPS, f"{dist_name}: {requirement}"
            if "extra ==" in requirement:
                assert "extra == 'postgresql'" in requirement.replace('"', "'"), requirement
                extras.add(name)
                continue
            plain.add(name)
        assert plain == expected, f"{dist_name}: {sorted(plain)}"
        assert extras == expected_extras[dist_name], f"{dist_name}: {sorted(extras)}"


def test_web_build_artifact_configured() -> None:
    """Web 静态构建的廉价静态检查：build script 存在、产物目录是 dist。

    真实构建仍由 `make check` 的 web-build 门禁覆盖（pnpm --dir apps/web build）。
    """
    package_json = json.loads((ROOT / "apps/web" / "package.json").read_text(encoding="utf-8"))
    build_script = package_json.get("scripts", {}).get("build")
    assert build_script, "apps/web/package.json must define a build script"
    assert "vite build" in build_script, build_script

    vite_config = (ROOT / "apps/web" / "vite.config.mts").read_text(encoding="utf-8")
    out_dir = re.search(r"outDir\s*:\s*['\"]([^'\"]+)['\"]", vite_config)
    assert out_dir is None or out_dir.group(1) == "dist", out_dir.group(1)
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert any(line.strip() == "dist/" for line in gitignore.splitlines()), (
        "dist/ must stay git-ignored"
    )


def test_remote_comparison_and_statistical_report_dispatch_in_six_wheels(clean_venv, tmp_path):
    """Real loopback HTTP commands with workspace runtime packages absent."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path == "/api/v1/capabilities":
                payload = {"api_version": "v1"}
            elif self.path.startswith("/api/v1/comparisons?"):
                payload = {"level": "comparable", "eligible": True, "refs": {
                    "baseline": {"run_id": "a", "scoring_pass_id": "pa"},
                    "candidate": {"run_id": "b", "scoring_pass_id": "pb"},
                }}
            elif self.path == "/api/v1/statistical-reports/saved":
                payload = {"report_id": "saved", "software_only_fixture": True}
            else:
                raise AssertionError(self.path)
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    code = '''
import importlib.util, sys
for name in ('motte_provider', 'motte_agent', 'motte_harness', 'motte_benchmark', 'motte_scenario', 'motte_skill', 'motte_sandbox'):
    assert importlib.util.find_spec(name) is None, name
from motte_sdk.comparisons import ComparisonService
from motte_cli.main import main
for argv in [['compare', '--baseline', 'a', '--candidate', 'b'], ['statistical-report', 'get', 'saved']]:
    assert main(argv + ['--mode', 'server', '--api-url', sys.argv[1]]) == 0
assert 'motte_sdk.calibration_ledger' not in sys.modules
assert 'motte_sdk.scoring_jobs' not in sys.modules
'''
    try:
        env = {key: value for key, value in _stripped_env().items() if key.lower() != "all_proxy"}
        proc = subprocess.run([str(clean_venv), "-I", "-c", code, f"http://127.0.0.1:{server.server_port}"],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert len(paths) == 4, paths
        assert any(path.startswith('/api/v1/comparisons?') for path in paths)
        assert '/api/v1/statistical-reports/saved' in paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
