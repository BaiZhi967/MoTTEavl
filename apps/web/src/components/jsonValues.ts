/** JSON 取值辅助：只读属性路径、数字提取与展示。
 * 缺字段一律返回 null / 「未知」，绝不补默认值或填 0。 */

export const UNKNOWN = "未知";

export function readPath(source: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((cursor, segment) => {
    if (cursor === null || typeof cursor !== "object") return undefined;
    return (cursor as Record<string, unknown>)[segment];
  }, source);
}

/** 按候选路径顺序取第一个有限数字；都没有时返回 null（未知）。 */
export function numberAt(source: unknown, paths: string[]): number | null {
  for (const path of paths) {
    const value = readPath(source, path);
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

export function displayValue(value: unknown, unknownText: string = UNKNOWN): string {
  if (value === null || value === undefined) return unknownText;
  if (typeof value === "string") return value.trim() === "" ? unknownText : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}
