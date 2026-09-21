"""验收 F-05：Skill 版本必须有公共发布入口，且 API 与 CLI 同一契约。

真实事故：apps/api/app/main.py 的注释说"发布（POST）暂不暴露：API 进程尚未接内容
存储"，但 create_resource_store() 早已装配 default_content_store()。CLI 也没有
skill 命令。结果是 Skill 只能静态校验、不能被发布，注入与三臂对照因此不可达；
Web 的 Skill 页面还提示用户"先发布"一个不存在的入口。

这里钉住两侧的接受/拒绝集合、服务端固定的 content_hash、幂等与冲突、草稿拒绝，
以及"没有内容存储时资源型 Skill 具名拒绝而不是产出一个没有字节的版本"。
"""
from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_cli.main import main
from motte_skill.content_store import ContentAddressedMemoryStore
from motte_skill.versions import skill_content_hash
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

RESOURCE_BYTES = b"# order helper\n\nconfirm before cancelling.\n"


def skill_body(**overrides):
    body = {
        "skill_id": "order-brief",
        "version": "1",
        "kind": "instruction",
        "description": "订单取消前的必要追问",
        "instruction": "先复述用户的关键约束，再给出结论；不要编造未提供的信息。",
        "published_at": "2026-09-21T00:00:00Z",
    }
    body.update(overrides)
    return body


def resource_manifest():
    return [{
        "path": "SKILL.md",
        "sha256": "sha256:" + hashlib.sha256(RESOURCE_BYTES).hexdigest(),
        "size_bytes": len(RESOURCE_BYTES),
        "media_type": "text/markdown",
    }]


def api_client(*, content_store=None):
    resources = InMemoryResourceStore(content_store=content_store)
    return TestClient(create_app(InMemoryRunStore(), resource_store=resources)), resources


def run_cli(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_document(tmp_path, payload, name="skill.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------- API


def test_api_publishes_an_instruction_skill_and_pins_the_content_hash():
    client, resources = api_client()

    response = client.post("/api/v1/skills/versions", json=skill_body())

    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored["skill_id"] == "order-brief" and stored["kind"] == "instruction"
    assert stored["content_hash"] == skill_content_hash(stored)
    assert client.get("/api/v1/skills/order-brief/versions/1").json() == stored
    assert client.get("/api/v1/skills/versions").json()["total"] == 1
    assert resources.skills.get("order-brief", "1") == stored


def test_api_republish_is_idempotent_and_conflicts_on_different_content():
    client, _resources = api_client()
    first = client.post("/api/v1/skills/versions", json=skill_body())
    again = client.post("/api/v1/skills/versions", json=skill_body())
    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["content_hash"] == first.json()["content_hash"]

    changed = client.post(
        "/api/v1/skills/versions", json=skill_body(instruction="完全不同的指令内容。")
    )
    assert changed.status_code == 409, changed.text
    assert client.get("/api/v1/skills/order-brief/versions/1").json() == first.json()


def test_api_refuses_drafts_hash_mismatch_and_credentials():
    client, _resources = api_client()

    draft = client.post("/api/v1/skills/versions", json=skill_body(lifecycle="draft"))
    assert draft.status_code == 422
    assert draft.json()["error"]["code"] == "SKILL_NOT_PUBLISHED"

    forged = client.post("/api/v1/skills/versions",
                         json=skill_body(content_hash="sha256:" + "c" * 64))
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "SKILL_INVALID"

    leaked = client.post("/api/v1/skills/versions", json=skill_body(api_key="sk-plaintext"))
    assert leaked.status_code == 422
    assert leaked.json()["error"]["code"] == "CREDENTIALS_REJECTED"

    managed = client.post("/api/v1/skills/versions", json=skill_body(_deleted=True))
    assert managed.status_code == 422
    assert managed.json()["error"]["code"] == "SERVER_MANAGED_FIELD"


def test_executable_skill_without_entrypoint_is_refused():
    client, _resources = api_client()

    response = client.post("/api/v1/skills/versions", json=skill_body(kind="executable"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SKILL_INVALID"


def test_resource_skill_without_a_content_store_is_refused_by_name():
    """没有内容存储时不许产出一个"没有字节"的已发布版本。"""
    client, _resources = api_client()

    response = client.post("/api/v1/skills/versions", json=skill_body(
        kind="instruction_with_resources", resource_manifest=resource_manifest(),
    ))

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "SKILL_RESOURCE_STORE_REQUIRED"


def test_resource_skill_publishes_when_the_bytes_are_in_the_store():
    store = ContentAddressedMemoryStore()
    store.put(RESOURCE_BYTES)
    client, _resources = api_client(content_store=store)

    response = client.post("/api/v1/skills/versions", json=skill_body(
        kind="instruction_with_resources", resource_manifest=resource_manifest(),
    ))

    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored["resource_manifest"][0]["path"] == "SKILL.md"


# ------------------------------------------------------------------- CLI


def test_cli_publish_and_list_match_the_api_contract(tmp_path, capsys):
    db = str(tmp_path / "runs.db")
    path = write_document(tmp_path, skill_body())

    code, out, err = run_cli(capsys, ["skill", "publish", path, "--db", db, "--json"])
    assert code == 0, err
    stored = json.loads(out)
    assert stored["content_hash"] == skill_content_hash(stored)

    code, out, err = run_cli(capsys, ["skill", "list", "--db", db, "--json"])
    assert code == 0, err
    listing = json.loads(out)
    assert listing["total"] == 1
    assert listing["items"][0]["content_hash"] == stored["content_hash"]

    client, _resources = api_client()
    api_stored = client.post("/api/v1/skills/versions", json=skill_body()).json()
    assert api_stored["content_hash"] == stored["content_hash"]


def test_cli_refuses_draft_with_a_stable_code(tmp_path, capsys):
    db = str(tmp_path / "runs.db")
    draft = write_document(tmp_path, skill_body(lifecycle="draft"), "draft.json")

    code, _out, err = run_cli(capsys, ["skill", "publish", draft, "--db", db])
    assert code != 0
    assert json.loads(err)["error"]["code"] == "SKILL_NOT_PUBLISHED"
