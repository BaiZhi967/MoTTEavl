import { Fragment, useState, type ReactNode } from "react";

/** 标识铭牌：端面标签栅格（来自球鞋档案墙的 raise）。
 *
 * 每个单元都带同一组固定属性、同一顺序——这是产品的证据机制在界面上的形状：
 * 数据集 revision / scenario 版本 / scoring pass 永远出现在同一个位置，不随页面变形。 */
export function IdentityPlate({ id, copyValue, rows }: {
  id: string;
  /** 复制用的全量值；给了才渲染复制按钮 */
  copyValue?: string;
  rows: Array<{ label: string; value: ReactNode }>;
}) {
  return (
    <dl className="plate">
      <dt>标识</dt>
      <dd>
        <span className="plate-id" title={copyValue ?? id}>{id}</span>
        {copyValue ? <CopyButton value={copyValue} /> : null}
      </dd>
      {rows.map((row) => (
        <Fragment key={row.label}>
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

function CopyButton({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="plate-copy"
      title="复制全量值"
      aria-label="复制全量值"
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setDone(true);
          window.setTimeout(() => setDone(false), 1200);
        });
      }}
    >
      {done ? "已复制" : "复制"}
    </button>
  );
}
