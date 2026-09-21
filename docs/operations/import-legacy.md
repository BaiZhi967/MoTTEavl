# 旧平台历史导入操作手册（import-legacy）

> M7-T06..T08 交付。协议：`docs/protocols/sdk-and-migration.md` §5；能力映射见
> `docs/migration/legacy-capability-map.md`。真实旧导出 apply 需要**单独授权**；
> 本文命令对合成/授权导出包同样适用。CLI 接线（`motte import` 子命令）随 T03
> 批次提供，当前先经 Python API。

## 1. 来源包格式

操作者显式提供的**受控目录**（不接受服务端读任意宿主路径）：

```
<export-root>/
  manifest.json          # ImportManifest：import_id/importer_version/transform_version/
                         # source_system/source_revision/source_schema_version/
                         # exported_at/record_counts/artifact_manifest_sha256/scope
  records/<type>/<id>.json   # provider_connection/model_profile/dataset/case/
                             # scenario/run/run_summary/score/artifact/baseline
  artifacts/<sha>/...    # 产物文件（内容寻址）
```

加载即校验（`load_source_package`）：路径穿越/绝对路径/UNC/symlink（含中间目录）
拒绝；配额 ≤20k 文件、≤512MiB 总量、单 record ≤4MiB、≤10k record；manifest 解析
fail-closed；license 缺失或未知 → restricted + 警告；secret 形状字段（api_key/
token/secret/password/authorization 的非引用值）→ 整条 record 拒绝（诊断只带字段
名，不带值）。

## 2. dry-run（零目标修改）

```python
from motte_storage.factory import create_run_store
from motte_sdk.migration.sources import load_source_package
from motte_sdk.migration.plan import plan_import

store = create_run_store()
source = load_source_package("path/to/export")
report = plan_import(store, source, operator="you@example")
```

输出 `ImportReport`：planned/created(reused)/conflicted/rejected 计数、
unknown_fields、missing_artifacts、credentials_to_rebind（**只列引用名**）、
counts/hash。对目标 DB 业务数据/凭据/状态零修改（唯一写入是平台账本的审计行）。

## 3. apply / resume

```python
from motte_sdk.migration.apply import apply_import

result = apply_import(store, "var/artifacts", source, operator="you@example")
```

- apply 前**重算**来源 `content_sha256`；与计划记录不一致 → `SourceChangedError`
  （旧计划作废，必须重新 dry-run，不能沿用旧批准）。
- 次序：provider/model 引用 → dataset/case → scenario → 历史 run/score/artifact →
  baseline；每条记录一个事务单元（store 写入 + 账本 put_mapping）。
- crash/resume：重跑 `apply_import` 自动跳过已提交 mapping_key（含崩溃窗口内
  store 已写但账本未写的单元，按 `import_source.content_hash` 回填），不重复、
  不重复关联。
- imported run：终态 + `origin:"imported"` + `distributable:false` + import_source；
  **永不 queued**，Dispatcher 对带 import_source 的 run 拒领。
- 保真：缺 model/gold/pass/price → 显式 `"unknown"`（绝不从当前配置补历史）；
  聚合 summary 只存 `legacy_summary`（不伪造逐题分）；进行中旧任务在 dry-run 即
  rejected（`in_flight_job_not_importable`）；旧 baseline 只注记在 run payload，
  不进入新平台 baseline_store/默认指针。
- artifact：声明 sha256 校验 + 读回校验通过后才提交引用；不符 → 该 record
  rejected（`artifact_hash_mismatch`）。

## 4. 冲突与回退

- 同 mapping_key 同内容 → reused；同 key 异内容 → conflicted（该单元停止、保留
  诊断，批次继续）。
- `rollback_import(store, artifacts_root, import_id, operator=..., confirm=True)`：
  只处理本批次创建的对象；artifact 被其他批次/任意 run 引用 → blocked（不删）；
  run 行不做物理删除（存储层不可变），整批标 `rolled_back` + run CAS 注记
  `rolled_back_import`，审计保留在账本与 tombstone。

## 5. 对账与验收

apply 后核对：planned == created+reused+rejected+conflicted；artifact hash；
引用完整性；分数/状态分布。命令演练：
`uv run pytest -q tests/migration`。真实迁移的授权、批次记录与回退窗口见
`docs/release/cutover.md`。
