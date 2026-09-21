"""local 模式 ``run --request-key`` 幂等创建（协议 sdk-and-migration.md frozen@1 §1.3）。

镜像 apps/api/app/main.py ``POST /api/v1/runs`` 的幂等块，保证 local 与
server 同输入产出同一 run_id：

- canonical hash = ``canonical_sha256(body 去掉 request_key)``（原始请求体，
  不是 prepare_run 之后的 manifest）；
- 同 key 同 hash → 返回同一 Run（``idempotent_replay: true``）；
- 同 key 异 hash → ``RequestConflict``（code REQUEST_KEY_CONFLICT，退出 2）；
- run_id 由 request_key 确定性派生，runs 表主键兜底（注册表丢失仍幂等）；
- 注册表走 ``motte_storage.platform.platform_for(store).requests``，跨进程持久。
"""
from __future__ import annotations

import hashlib
from typing import Any

from motte_contracts.identity import canonical_sha256


def deterministic_run_id(request_key: str) -> str:
    return "run-" + hashlib.sha256(
        ("motte-request-key:" + request_key).encode("utf-8")
    ).hexdigest()[:32]


def create_run_idempotent(
    service: Any,
    *,
    scenario_version: str,
    manifest: dict[str, Any],
    case_ids: list[str],
    requested_manifest: dict[str, Any] | None,
    request_key: str,
    raw_case_ids: list[str] | None = None,
) -> dict[str, Any]:
    """带 request_key 的本地创建；语义与 API 幂等块逐行对齐。

    ``raw_case_ids`` 是**原始请求 body** 的 case_ids（canonical hash 必须覆盖
    原始 body，与 API 逐字节同构）；缺省回退到解析后的 case_ids。
    """
    from motte_storage.integrity import RunConflictError
    from motte_storage.platform import platform_for

    # canonical hash 覆盖**原始请求体**（scenario/manifest/case_ids），
    # 与 API 侧 body 逐字节同构——这是 local/server parity 的根基。
    body = {
        "scenario_version": scenario_version,
        "manifest": requested_manifest if requested_manifest is not None else manifest,
        "case_ids": list(raw_case_ids if raw_case_ids is not None else case_ids),
    }
    canonical_hash = canonical_sha256(body)
    registry = platform_for(service.store).requests
    existing = registry.get(request_key)
    if existing is not None and existing["canonical_hash"] != canonical_hash:
        # 同 key 异 body：与 API 同一失败语义（409 REQUEST_KEY_CONFLICT）。
        from motte_storage.platform import RequestConflict

        raise RequestConflict(
            "request key was already used with a different request body: "
            + request_key
        )
    if existing is not None:
        try:
            replayed = service.get_run(existing["run_id"])
        except KeyError:
            replayed = None  # 陈旧绑定：按确定性 id 重建同一 Run
        if replayed is not None:
            return {**replayed, "idempotent_replay": True}
    run_id = deterministic_run_id(request_key)
    try:
        created = service.create_run(
            scenario_version, manifest, case_ids,
            requested_manifest=requested_manifest, run_id=run_id,
        )
    except RunConflictError:
        # 确定性 id 已存在（并发同 key，或注册表丢失后的重放）：返回现有 Run。
        replayed = service.get_run(run_id)
        return {**replayed, "idempotent_replay": True}
    registry.bind(request_key, canonical_hash, created["id"])
    return created
