# AGENTS.md — MoTTEavl 代理工作约定

## UI / 前端（`apps/web`）设计治理

任何涉及 `apps/web` 的界面改动，开工前必读 **`apps/web/DESIGN.md`**（设计宪法）。

1. **token 纪律**：取值只允许 `var(--…)`。令牌定义在两处，都在 `apps/web/src/`：
   - `tailwind.css` 的 `@theme`：板面调色板、字体、圆角、密度（Tailwind 4 CSS-first 配置）；
   - `theme.css`：令牌别名、语气三件套、骨架尺寸、Semi 语义 token 覆盖、浏览器表面。
   需要新颜色 / 字号 / 间距时，先在 DESIGN.md 登记，再加进 `@theme`，同一个提交内完成；
   **禁止在组件或 CSS 规则里直接写十六进制色值**（`theme.css` 与 `tailwind.css` 是唯一例外，
   `tests/stylesheet.test.ts` 会扫描 ts/tsx 与所有其他 css 并失败）。
   样式分三层（DESIGN.md §2.2）：`tailwind.css`（令牌）→ `ui.css`（控制台类层）→ `theme.css`（主题层），
   板面基础件在 `board/board.css`。**页面级的一次性样式写页面自己的 Tailwind 工具类，不要往 `ui.css` 里加**。
2. **状态语义唯一来源**：运行 / 步骤 / 资源 / 实验等状态的中文标签与语气只从
   `apps/web/src/components/statusMeta.ts` 的 `STATUS_META` 取值；新增状态先登记。
   **评分判定（通过 / 未通过 / 未判定）是另一套词表**，不得混进 `STATUS_META`。
3. **skill 路由**（已安装在项目 `.agents/skills/`）：
   - 新增页面或全新界面区块 → 按 `design-taste-frontend` 流程
   - 修改现有界面 → 按 `redesign-existing-projects` 流程（先审计列问题，增量改，不重写）
   - 审美争议 → 以 `minimalist-ui` 协议为准裁决
4. **技术栈（2026-09 起）**：样式用 **Tailwind CSS 4** + **Semi Design**（`@douyinfe/semi-ui`）；
   表单控件优先用 Semi 组件（`Input` / `Select` / `Button`），用深路径引入
   （`@douyinfe/semi-ui/lib/es/input`）以免整包进 bundle；图标用 `@douyinfe/semi-icons`，
   存量 `@phosphor-icons/react` 按页面逐步替换。交互原语仍可用 Radix。
   **旧的「禁止 Tailwind 与带视觉主见的组件库」条目已废除**（用户明确决定）。
   仍然禁止：Lucide / Feather / Heroicons、emoji 当图标、在组件里写十六进制色值、自造状态色。
5. **完工门禁**：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 必须全绿。
6. **迁移桥已拆除（2026-09）**：`index.css`（旧浅色纸面）已删除，控制台类层是 `ui.css`，
   取值只允许板面令牌；`.operate-card` / `.operate-grid` / `.operate-section` 整族不存在，
   操作页一律用 `FieldGrid` + `IssueBar`。**不要再引入任何"临时兼容层"**：要么改类层，要么改页面。
7. **截图即证据**：任何用来证明"界面已改好"的截图，必须满足三条——
   ① 截图时间晚于 `pnpm build`；② 服务端实际吐出的资产名与 `dist/index.html` 一致；
   ③ **图里能读到构建标识**（侧栏底部的 `__BUILD_ID__`，形如 `ac94801·09221429`）。
   拿不出这三条就别声称截图有效——本项目已经吃过两次"服务的是旧 dist"的亏。

## 通用约定

- 完整开发命令见 `README.md`；提交前跑 `make check`（与 CI 同款门禁：lint + test + web build + compose config）。
- Python 侧：ruff 已配置（`make lint`），测试用 `uv run pytest -q -m "not live"`。
- 文档与设计规范修改与对应代码放在同一个提交，保持两者不脱节。
