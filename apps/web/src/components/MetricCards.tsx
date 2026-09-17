export interface MetricItem {
  label: string;
  value: string;
  tone?: "success" | "error" | "neutral";
}

export function MetricCards({ items }: { items: MetricItem[] }) {
  return (
    <div className="metric-cards">
      {items.map((item) => (
        <div key={item.label} className="metric-card" data-tone={item.tone ?? "neutral"}>
          <p className="metric-value mono">{item.value}</p>
          <p className="metric-label">{item.label}</p>
        </div>
      ))}
    </div>
  );
}
