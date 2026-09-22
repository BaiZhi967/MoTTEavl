import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Button, Input, Select, SideSheet, Table, Tabs } from "@douyinfe/semi-ui";

afterEach(cleanup);

/* 第 0 步探针（REDESIGN-PLAN.md §3.1）：Semi Design 在 React 19 下能否真实渲染。
 * 这条测试是长期的回归护栏：升级 React 或 Semi 时先跑它。 */
describe("设计系统探针：Semi Design × React 19", () => {
  it("Button / Input / Select / Table / Tabs / SideSheet 都能渲染", () => {
    render(
      <div>
        <Button theme="solid" type="primary">签发</Button>
        <Input placeholder="run id" />
        <Select optionList={[{ value: "all", label: "全部" }]} value="all" />
        <Table
          columns={[
            { title: "ID", dataIndex: "id" },
            { title: "状态", dataIndex: "status" },
          ]}
          dataSource={[{ id: "run-1", status: "已完成" }]}
          pagination={false}
        />
        <Tabs tabList={[{ itemKey: "op", tab: "操作" }]} />
        <SideSheet visible title="详情" onCancel={() => undefined}>
          <p>内容</p>
        </SideSheet>
      </div>
    );
    expect(screen.getByRole("button", { name: "签发" })).toBeTruthy();
    expect(screen.getByText("run-1")).toBeTruthy();
    expect(screen.getByText("已完成")).toBeTruthy();
  });
});
