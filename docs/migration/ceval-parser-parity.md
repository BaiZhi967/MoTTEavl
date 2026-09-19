# C-Eval Parser 迁移对照报告

> 状态：**迁移完成并有 golden 对照（M2-T06）**。对照测试：
> `tests/benchmarks/test_ceval_parser_parity.py::test_parser_native_diagnostic_parity`。

## 1. 迁移来源

- 旧实现：`llm_agent__evaluation_platform @ b661bcdf83e1c3dfb8d6062ee78817d249e86a4c`
  `packages/benchmark-adapters/src/evalstudio_benchmark_adapters/opencompass.py`（同作者私有项目）。
- 新实现：`packages/benchmark-runtime/motte_benchmark/opencompass/parser.py`。
- Runner 版本基线：旧适配器钉定的上游 OpenCompass `0.4.2`（升级另开兼容任务，不在迁移中切 latest）。

## 2. 迁移方法（golden 的产生方式）

fixture 的期望值不是手写，而是**旧代码本身**在固定提交上原样执行产出：

1. 合成 OpenCompass 输出目录（覆盖四大类 + Hard 学科、结论优先/官方兜底/
   空预测/predictions 兜底/仅聚合六种形态）；
2. 以 stub 隔离旧模块的控制面依赖（schemas/CLI 基类），原样执行
   `_extract_conclusion` / `_resolve_prediction` / `_extract_option` /
   `_sample_rows_from_details` / `_aggregate_ceval`；
3. 期望值（逐样本预测/提取来源/结论覆盖/对错、逐学科、宏平均聚合、
   结论覆盖数、空预测率）落盘
   `tests/fixtures/benchmarks/ceval/manifest.json`（含 provenance）。

新 Parser 必须与该 golden 逐项一致。fixture 数据为合成内容，**不是官方
C-Eval 数据**；官方许可核对是获取与分发约束，与合成对照无关。

## 3. 保留的语义（逐项）

| 旧实现 | 新实现 | 语义 |
|---|---|---|
| `_CONCLUSION_PATTERNS` + `_CONCLUSION_NEGATIVE_RE` + `_extract_conclusion` | 同名逐字迁移 | 结论句式优先，模式优先级内取最后一次出现；“答案不/没/无/未”不算结论 |
| `_ANSWER_EXPLICIT/LINE/ANY_RE` + `_extract_option` | 同名逐字迁移 | 无结论时官方 regex 链兜底（ANY 取最后一次） |
| `_resolve_prediction` | 同名逐字迁移 | 两段式；结论与官方不一致时保留官方值对账 |
| `CEVAL_SUBJECT_CATEGORIES`（52 学科→四类） | 同数据逐字迁移 | 未知学科报错，禁止静默归 Other |
| `_aggregate_ceval` | 同名逐字迁移 | 学科宏平均：总 accuracy、四大类、Hard 八学科、subject:* |
| `EMPTY_PREDICTION_WARN_RATIO=0.10` + 告警文案 | 同常量迁移 | 空预测率与告警 |
| `--dump-eval-details` details 解析 + predictions 兜底 | `parse_opencompass_results` | 同样的回退顺序与行归一化 |

## 4. 改变的语义（与理由）

| 变更 | 理由 |
|---|---|
| 指标拆成 `native.*` 与 `diagnostic.*` 两个命名空间 | M2-G10/A11：原始聚合与平台重算分开存储/命名/显示，不互相覆盖；不一致记 discrepancy 与 extractor 版本 |
| 仅聚合学科不回退任何样本，标记 `aggregate_only_native_only` | M2-A03：保留聚合与缺失标记，不伪造样本 |
| 输入安全强化：symlink/路径逃逸拒绝、单文件大小上限、半写 JSON 拒绝 | M2-A13：不能读取任意宿主文件；旧实现只做只读信任 |
| 删除控制面依赖（旧 schemas/CLI/manifest 构造） | 新平台的 job 契约（ExternalJobSpec/NormalizedCaseResult）由 T01–T03 承担；解析保持纯函数 |
| 空预测语义进入 evaluator 的 attempted/empty 统计 | M2-G11：每个 selected case 有明确处置；未尝试与空预测分开 |

## 5. 已知缺口

- 旧实现里 summary CSV 不参与解析（仅 per-subject JSON + predictions），
  新实现保持一致；CSV 解析在需要时另开任务并更新本报告。
- OpenCompass 真实 0.4.2 输出的实机回归在 T10 的"真实固定 Runner +
  本地确定性端点"层补齐（当前对照基于旧代码自证的 golden 与合成目录）。
