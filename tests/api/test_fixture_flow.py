"""验收 F-04：Fixture 版本必须有公共发布入口，且 API 与 CLI 同一契约。

真实事故：ResourceStore.publish_fixture 只被测试调用，HTTP 层没有 /api/v1/fixtures
（404），CLI 没有 fixture 命令。于是引用 fixture 的 Workflow 能发布、却不能运行：
创建 Run 时以 WORKFLOW_FIXTURE_MISSING 拒绝，M5 的"业务状态断言主流程"在产品面
完全不可达。

这里钉住两侧的接受/拒绝集合、服务端固定的 content_hash、幂等与冲突语义，以及
已发布版本不可删除。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_cli.main import main
from motte_contracts.fixture import fixture_content_hash
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

PUBLISHED_AT = "2026-09-21T00:00:00Z"


def fixture_body(**overrides):
    body = {
        "fixture_id": "order-state",
        "version": 1,
        "kind": "json",
        "description": "订单初始状态",
        "initial_data": {"order": {"id": "order-1", "status": "active"}},
        "allowed_tools": ["orders.get", "orders.cancel"],
        "published_at": PUBLISHED_AT,
    }
    body.update(overrides)
    return body


def api_client():
    resources = InMemoryResourceStore()
    return TestClient(create_app(InMemoryRunStore(), resource_store=resources)), resources


def run_cli(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_document(tmp_path, payload, name="fixture.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------- API


def test_api_publishes_a_fixture_and_pins_the_content_hash():
    client, resources = api_client()

    response = client.post("/api/v1/fixtures", json=fixture_body())

    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored["fixture_id"] == "order-state" and stored["version"] == 1
    # 服务端固定 hash，且与契约函数一致（客户端可以省略它）
    assert stored["content_hash"] == fixture_content_hash(stored)
    assert client.get("/api/v1/fixtures/order-state/1").json() == stored
    assert client.get("/api/v1/fixtures").json()["total"] == 1
    assert resources.fixtures.get("order-state", "1") == stored


def test_api_republish_is_idempotent_and_conflicts_on_different_content():
    client, _resources = api_client()
    first = client.post("/api/v1/fixtures", json=fixture_body())
    again = client.post("/api/v1/fixtures", json=fixture_body())

    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["content_hash"] == first.json()["content_hash"]

    changed = fixture_body(initial_data={"order": {"id": "order-1", "status": "cancelled"}})
    conflict = client.post("/api/v1/fixtures", json=changed)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "RESOURCE_CONFLICT"
    # 冲突不改变已发布版本
    assert client.get("/api/v1/fixtures/order-state/1").json() == first.json()


def test_api_refuses_drafts_and_client_supplied_hash_mismatch():
    client, _resources = api_client()

    draft = client.post("/api/v1/fixtures", json=fixture_body(lifecycle="draft"))
    assert draft.status_code == 422
    assert draft.json()["error"]["code"] == "FIXTURE_NOT_PUBLISHED"

    forged = client.post("/api/v1/fixtures",
                         json=fixture_body(content_hash="sha256:" + "b" * 64))
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "FIXTURE_CONTENT_HASH_MISMATCH"


def test_api_refuses_credentials_and_server_managed_fields():
    client, _resources = api_client()

    leaked = client.post("/api/v1/fixtures", json=fixture_body(api_key="sk-plaintext"))
    assert leaked.status_code == 422
    assert leaked.json()["error"]["code"] == "CREDENTIALS_REJECTED"

    managed = client.post("/api/v1/fixtures", json=fixture_body(_deleted=True))
    assert managed.status_code == 422
    assert managed.json()["error"]["code"] == "SERVER_MANAGED_FIELD"

    invalid = client.post("/api/v1/fixtures", json=fixture_body(kind="magic"))
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "FIXTURE_INVALID"


def test_published_fixture_cannot_be_deleted():
    client, _resources = api_client()
    client.post("/api/v1/fixtures", json=fixture_body())

    removed = client.delete("/api/v1/fixtures/order-state/1")
    assert removed.status_code == 409
    assert removed.json()["error"]["code"] == "PUBLISHED_RESOURCE_IMMUTABLE"
    assert client.delete("/api/v1/fixtures/order-state/9").status_code == 404


# ------------------------------------------------------------------- CLI


def test_cli_publish_and_list_match_the_api_contract(tmp_path, capsys):
    db = str(tmp_path / "runs.db")
    path = write_document(tmp_path, fixture_body())

    code, out, err = run_cli(capsys, ["fixture", "publish", path, "--db", db, "--json"])
    assert code == 0, err
    stored = json.loads(out)
    assert stored["content_hash"] == fixture_content_hash(stored)

    code, out, err = run_cli(capsys, ["fixture", "list", "--db", db, "--json"])
    assert code == 0, err
    listing = json.loads(out)
    assert listing["total"] == 1
    assert listing["items"][0]["content_hash"] == stored["content_hash"]

    # 同一份文档在 API 侧得到同一个 hash（两侧同一个契约与同一个算法）
    client, _resources = api_client()
    api_stored = client.post("/api/v1/fixtures", json=fixture_body()).json()
    assert api_stored["content_hash"] == stored["content_hash"]


def test_cli_refuses_draft_and_conflict_with_stable_codes(tmp_path, capsys):
    db = str(tmp_path / "runs.db")
    published = write_document(tmp_path, fixture_body(), "published.json")
    draft = write_document(tmp_path, fixture_body(lifecycle="draft"), "draft.json")
    changed = write_document(
        tmp_path, fixture_body(initial_data={"order": {"status": "cancelled"}}), "changed.json",
    )

    assert run_cli(capsys, ["fixture", "publish", published, "--db", db])[0] == 0

    code, _out, err = run_cli(capsys, ["fixture", "publish", draft, "--db", db])
    assert code != 0
    assert json.loads(err)["error"]["code"] == "FIXTURE_NOT_PUBLISHED"

    code, _out, err = run_cli(capsys, ["fixture", "publish", changed, "--db", db])
    assert code != 0
    assert json.loads(err)["error"]["code"] == "RESOURCE_CONFLICT"
