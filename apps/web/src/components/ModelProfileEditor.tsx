export function ModelProfileEditor({ profile }: { profile: any }) {
  return (
    <section className="panel">
      <h2>模型能力</h2>
      <dl className="kv">
        <dt>模型</dt>
        <dd>{profile.id ?? "未知"}</dd>
        <dt>Provider</dt>
        <dd>{profile.provider ?? "未知"}</dd>
        <dt>输入模态</dt>
        <dd>{(profile.input_modalities ?? ["text"]).join("、")}</dd>
        <dt>上下文上限</dt>
        <dd>{profile.context_window ?? "未知"}</dd>
        <dt>输出上限</dt>
        <dd>{profile.max_output_tokens ?? "未知"}</dd>
        <dt>支持工具</dt>
        <dd>{profile.supports_tools ? "是" : "否"}</dd>
      </dl>
    </section>
  );
}
