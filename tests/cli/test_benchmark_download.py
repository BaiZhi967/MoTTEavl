"""CLI `benchmark download`：网络用假 urlopen 注入，落盘与导入都指向 tmp 路径。

按 1319 行（官方 test split 的真实规模）构造合成源，验证默认（解析最新 commit + 全量）与
高级路径（显式 revision / scope / version）的题数、落盘文件名与幂等行为，不产生真实网络。
"""
import hashlib
import json
import urllib.request

from motte_cli.main import main
from motte_storage.resource_store import SQLiteResourceStore

REVISION = "b" * 40
LATEST = "c" * 40
COMMITS_URL = "https://api.github.com/repos/openai/grade-school-math/commits"
ROWS = "\n".join(
    json.dumps({"question": f"SYNTHETIC {i}: compute one.", "answer": f"reason\n#### {i}"})
    for i in range(1319)
).encode()


def _fake_urlopen(request, timeout):
    url = request.full_url
    if url.startswith(COMMITS_URL):
        payload = json.dumps([{"sha": LATEST}]).encode()
    else:
        # 默认路径用解析出的 LATEST，显式 --revision 用 REVISION
        assert url in (f"https://raw.githubusercontent.com/openai/grade-school-math/{revision}"
                       "/grade_school_math/data/test.jsonl" for revision in (REVISION, LATEST))
        assert request.method == "GET"
        payload = ROWS

    class Response:
        status = 200

        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Response()


def _stderr_json(capsys):
    """stderr 上可能先有下载进度行，机器可读的错误 JSON 始终是最后一行。"""
    return json.loads(capsys.readouterr().err.strip().splitlines()[-1])


def _download_args(tmp_path, **overrides):
    args = ["benchmark", "download", "--license", "MIT",
            "--db", str(tmp_path / "runs.db"), "--source-dir", str(tmp_path / "datasets")]
    for key, value in overrides.items():
        args += [f"--{key.replace('_', '-')}", value]
    return args


def test_download_defaults_to_latest_full(tmp_path, monkeypatch, capsys):
    """不传 --revision / --version / --scope：解析官方最新 commit + 全量 + 自动版本号。"""
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert main(_download_args(tmp_path)) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["revision"] == LATEST  # 解析出的 sha 固定进 provenance
    assert receipt["imported"] == "gsm8k-test@1"
    assert receipt["scenario"] == "gsm8k-test-full@1"
    assert receipt["scope"] == "full" and receipt["benchmark"] == "gsm8k-full"
    assert receipt["cases"] == 1319
    assert receipt["source_sha256"] == hashlib.sha256(ROWS).hexdigest()
    # 源文件按解析出的 revision 落盘，可事后核对题面来源
    assert (tmp_path / "datasets" / f"test-{LATEST}.jsonl").read_bytes() == ROWS
    resources = SQLiteResourceStore(str(tmp_path / "runs.db"))
    dataset = resources.datasets.get("gsm8k-test", "1")
    assert len(dataset["cases"]) == 1319
    assert dataset["benchmark"]["id"] == "gsm8k-full"
    assert dataset["benchmark"]["selected_count"] == 1319
    assert dataset["cases"][-1]["case_id"] == "gsm8k-test-1318"
    assert dataset["provenance"]["revision"] == LATEST
    scenario = resources.scenarios.get("gsm8k-test-full", "1")
    assert scenario["dataset"] == "gsm8k-test@1"
    assert scenario["benchmark"]["selected_count"] == 1319
    # 再跑一次默认命令：内容相同 → 复用版本 1，不会攒出新版本
    assert main(_download_args(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == receipt
    assert [d["version"] for d in resources.datasets.list()] == ["1"]


def test_download_smoke_scope_goes_to_the_next_free_version(tmp_path, monkeypatch, capsys):
    """高级路径：先默认全量@1，再 --scope smoke → 内容不同，自动落到 @2。"""
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert main(_download_args(tmp_path)) == 0
    capsys.readouterr()
    assert main(_download_args(tmp_path, scope="smoke")) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["imported"] == "gsm8k-test@2"
    assert receipt["scenario"] == "gsm8k-test-smoke@2"
    assert receipt["scope"] == "smoke" and receipt["benchmark"] == "gsm8k-20"
    assert receipt["cases"] == 20
    resources = SQLiteResourceStore(str(tmp_path / "runs.db"))
    assert [(d["version"], d["benchmark"]["id"]) for d in resources.datasets.list()] == [
        ("1", "gsm8k-full"), ("2", "gsm8k-20")]


def test_explicit_revision_and_version_still_honored(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert main(_download_args(tmp_path, revision=REVISION, version="7", scope="smoke")) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["revision"] == REVISION
    assert receipt["imported"] == "gsm8k-test@7" and receipt["cases"] == 20
    assert (tmp_path / "datasets" / f"test-{REVISION}.jsonl").exists()


def test_same_version_scope_conflict_asks_for_new_version(tmp_path, monkeypatch, capsys):
    """显式钉住同一版本号时两个 scope 才冲突：提示换版本，省略版本即自动避让。"""
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert main(_download_args(tmp_path, scope="smoke", version="1")) == 0
    capsys.readouterr()
    assert main(_download_args(tmp_path, scope="full", version="1")) == 2
    failure = _stderr_json(capsys)
    assert failure["error"]["code"] == "RESOURCE_CONFLICT"
    assert "new version" in failure["error"]["message"]
    # 省略版本号 → 自动取下一个空号
    assert main(_download_args(tmp_path, scope="full")) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["scenario"] == "gsm8k-test-full@2" and receipt["cases"] == 1319


def test_download_rejects_unpinned_revision_before_network(tmp_path, monkeypatch, capsys):
    def fail_on_network(*args, **kwargs):
        raise AssertionError("malformed revision must not reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", fail_on_network)
    assert main(_download_args(tmp_path, revision="main")) == 2
    failure = _stderr_json(capsys)
    assert failure["error"]["code"] == "CONTRACT_INVALID"


def test_download_reports_unresolvable_latest_with_hint(tmp_path, monkeypatch, capsys):
    def unavailable(request, timeout):
        raise OSError("proxy refused")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    assert main(_download_args(tmp_path)) == 2
    failure = _stderr_json(capsys)
    assert failure["error"]["code"] == "SOURCE_UNAVAILABLE"
    assert "explicit 40-character commit" in failure["error"]["message"]
    assert not (tmp_path / "runs.db").exists()
