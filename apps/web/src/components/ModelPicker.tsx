import type { ModelRecord } from "../api/client";

/** 上下文窗口徽章格式：256000 → 256K、1000000 → 1M（自 ResourcesPage 移入，全站共用）。 */
export function formatContext(contextWindow?: number | null): string | null {
  if (contextWindow == null) return null;
  if (contextWindow >= 1_000_000) {
    const millions = contextWindow / 1_000_000;
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M`;
  }
  if (contextWindow >= 1_000) {
    const thousands = contextWindow / 1_000;
    return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}K`;
  }
  return String(contextWindow);
}

export function ModelPicker({ models, selected, onToggle }: {
  models: ModelRecord[];
  selected: string[];
  onToggle: (id: string) => void;
}) {
  const groups = new Map<string, ModelRecord[]>();
  for (const model of models) {
    const key = model.provider ?? "（未关联 Provider）";
    groups.set(key, [...(groups.get(key) ?? []), model]);
  }
  return (
    <div className="model-picker" role="group" aria-label="模型多选">
      {[...groups.entries()].map(([provider, group]) => (
        <div key={provider} className="model-picker-group">
          <p className="field-label">{provider}</p>
          {group.map((model) => {
            const enabled = model.enabled !== false;
            const context = formatContext(model.context_window);
            return (
              <label key={model.id} className="model-picker-item" data-enabled={enabled ? undefined : "false"}>
                <input
                  type="checkbox"
                  checked={selected.includes(model.id)}
                  disabled={!enabled}
                  onChange={() => onToggle(model.id)}
                  aria-label={`选择模型 ${model.id}`}
                />
                <span className="mono">{model.id}</span>
                {context && <span className="status-badge status-tone-neutral model-badge">{context}</span>}
                {model.supports_tools && <span className="status-badge status-tone-neutral model-badge">工具</span>}
                {!enabled && <span className="muted">已停用</span>}
              </label>
            );
          })}
        </div>
      ))}
      {models.length === 0 && <p className="empty">暂无可用模型，先在 Provider 页注册</p>}
    </div>
  );
}
