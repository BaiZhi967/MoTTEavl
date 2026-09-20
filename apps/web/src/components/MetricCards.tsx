export interface MetricItem {
  label: string;
  value: string;
  tone?: "success" | "error" | "neutral";
  /** 可选：给指标卡一个稳定身份，便于按语义（而不是按文案）断言取值。 */
  testId?: string;
}

export function MetricCards({ items }: { items: MetricItem[] }) {
  return (
    <div className="metric-cards">
      {items.map((item) => (
        <div
          key={item.label}
          className="metric-card"
          data-tone={item.tone ?? "neutral"}
          data-testid={item.testId}
        >
          <p className="metric-value mono">{item.value}</p>
          <p className="metric-label">{item.label}</p>
        </div>
      ))}
    </div>
  );
}
