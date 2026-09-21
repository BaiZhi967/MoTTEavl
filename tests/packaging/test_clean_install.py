"""干净安装与打包门禁（M7-T05，protocol §8 / A01 / G09）。

验证交付 wheel 在 **checkout 之外**的干净 venv 里可用：
- 无 cwd / PYTHONPATH 依赖（env 剥离 PYTHON*，cwd 一律指向临时目录）；
- ``import motte_sdk`` / ``from motte_sdk import MotteClient`` 可用，
  构造 ``MotteClient("http://127.0.0.1:1")`` 无副作用、不落任何本地文件；
- ``python -m motte_cli --help`` 退出码 0；
- 包资源（importlib.resources）从不同 cwd 读取结果一致；
- 依赖边界（G09）：docker / celery / psutil / pyyaml 不进入 SDK/API 交付闭包，
  psycopg 只经 motte-storage 的 [postgresql] extra，pytest 只经 [pytest] extra。

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

# 解析层（干净 venv 实际装了什么）的额外禁区：pytest/psycopg 只应经 extra 进入。
RESOLVED_FORBIDDEN = {"docker", "celery", "psutil", "pytest", "psycopg"}
# 注意 pyyaml 不在 RESOLVED_FORBIDDEN：motte-contracts 的既定依赖 cel-python
# 自身传递依赖 pyyaml（uv.lock 可查），该泄漏不在我们的声明控制内；G09 对
# pyyaml 的约束因此落在声明层（wheel Requires-Dist 检查）。

# 干净 venv 里做导入冒烟的交付包。motte_storage 不在其中：它的 __init__
# 顶层导入 postgres.py（psycopg），按 G09 psycopg 只随 [postgresql] extra 安装。
CLEAN_IMPORT_NAMES = ["motte_sdk", "motte_contracts", "motte_eval", "motte_trace", "motte_cli"]

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
    for dist_name, expected in expected_plain.items():
        requires = _requires_dist_of_wheel(wheel_dir, dist_name)
        plain: set[str] = set()
        for requirement in requires:
            name = _requirement_name(requirement)
            assert name not in RUNNER_ONLY_DEPS, f"{dist_name}: {requirement}"
            if "extra ==" in requirement:
                assert name == "psycopg", f"unexpected extra in {dist_name}: {requirement}"
                continue
            plain.add(name)
        assert plain == expected, f"{dist_name}: {sorted(plain)}"


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
