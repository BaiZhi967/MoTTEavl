# AGENTS.md — MoTTEavl 代理工作约定

## UI / 前端（`apps/web`）设计治理

任何涉及 `apps/web` 的界面改动，开工前必读 **`apps/web/DESIGN.md`**（设计宪法）。

1. **token 纪律**：样式取值只允许 `var(--…)`（定义在 `apps/web/src/index.css` 的 `:root`）。
   需要新颜色 / 字号 / 间距时，先在 DESIGN.md 登记，再加进 `:root`，同一个提交内完成；
   禁止在组件或 CSS 规则里直接写十六进制色值。
2. **状态语义唯一来源**：运行状态的中文标签与语气只从 `apps/web/src/components/statusMeta.ts`
   的 `STATUS_META` 取值；新增状态先登记，徽章 / 时间线 / 过滤下拉自动继承。
3. **skill 路由**（已安装在项目 `.agents/skills/`）：
   - 新增页面或全新界面区块 → 按 `design-taste-frontend` 流程（拨盘已锁定 `3/2/7`，见 DESIGN.md 第 0 节）
   - 修改现有界面 → 按 `redesign-existing-projects` 流程（先审计列问题，增量改，不重写）
   - 审美争议 → 以 `minimalist-ui` 协议为准裁决
4. **组件与图标**：交互原语用 Radix UI，常备集已装（`tabs` / `dialog` / `dropdown-menu` /
   `select` / `switch`），其余按需装 `@radix-ui/react-*`；图标用 `@phosphor-icons/react`
   （Bold 字重，导入用带 `Icon` 后缀的导出名）。禁止引入 Ant Design / MUI / shadcn 等
   带视觉主见的组件库，禁止 Lucide / Feather / Heroicons，禁止 emoji 当图标。
5. **完工门禁**：`pnpm --dir apps/web test` 与 `pnpm --dir apps/web build` 必须全绿。

## 通用约定

- 完整开发命令见 `README.md`；提交前跑 `make check`（与 CI 同款门禁：lint + test + web build + compose config）。
- Python 侧：ruff 已配置（`make lint`），测试用 `uv run pytest -q -m "not live"`。
- 文档与设计规范修改与对应代码放在同一个提交，保持两者不脱节。
