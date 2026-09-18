import { describe, expect, it } from "vitest";
import { formatClock, formatDuration, formatTimestamp, shortRunId } from "../src/components/runFormat";

describe("shortRunId", () => {
  it("短 ID 原样返回", () => {
    expect(shortRunId("run-42")).toBe("run-42");
  });

  it("长 ID 截断为首 13 + … + 尾 4", () => {
    const id = `run-${"a".repeat(36)}`;
    expect(shortRunId(id)).toBe(`run-${"a".repeat(9)}…${"a".repeat(4)}`);
  });
});

describe("formatTimestamp", () => {
  it("输出 MM-DD HH:mm", () => {
    expect(formatTimestamp("2026-09-19T08:30:00")).toBe("09-19 08:30");
  });

  it("空值与非法值返回 null", () => {
    expect(formatTimestamp(null)).toBeNull();
    expect(formatTimestamp("not-a-date")).toBeNull();
  });
});

describe("formatClock", () => {
  it("输出 MM-DD HH:mm:ss", () => {
    expect(formatClock("2026-09-19T08:30:05")).toBe("09-19 08:30:05");
  });
});

describe("formatDuration", () => {
  it("按秒/分/时分级格式化", () => {
    expect(formatDuration("2026-09-19T08:00:00", "2026-09-19T08:00:45")).toBe("45s");
    expect(formatDuration("2026-09-19T08:00:00", "2026-09-19T08:03:12")).toBe("3m12s");
    expect(formatDuration("2026-09-19T07:00:00", "2026-09-19T08:04:00")).toBe("1h04m");
  });

  it("缺起止时间返回 null（进行中）", () => {
    expect(formatDuration("2026-09-19T08:00:00", null)).toBeNull();
  });
});
