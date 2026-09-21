import { useEffect, useRef, useState } from "react";
import {
  getRuntimeCommands, getRuntimeSessions, sendRuntimeCommand,
  ApiRequestError,
  type RuntimeApproval, type RuntimeCommand, type RuntimeCommandRequest, type RuntimeSession,
} from "../api/client";

const COMMAND_LABELS: Record<string, string> = {
  queued: "排队等待投递", delivered: "已提交，等待原生确认", acknowledged: "已收到原生确认",
  rejected: "已拒绝", expired: "已过期", delivery_unknown: "送达结果未知，请勿重复发送", failed: "投递失败",
};
const KIND_LABELS: Record<string, string> = {
  user_message: "补充消息", approve: "批准", reject: "拒绝", interrupt: "中断请求",
};
const ACK_LABELS: Record<string, string> = {
  message_accepted: "原生会话已接收消息",
  interrupt_requested: "原生中断请求已接收，仍需等待停止结果",
  request_resolved: "原始审批请求已结束，不代表动作执行成功",
};

/** Only active, bound sessions can receive commands. No automatic POST retries. */
export function RuntimeCommands({ runId, terminal }: { runId: string; terminal: boolean }) {
  return <BoundRuntimeCommands key={runId} runId={runId} terminal={terminal} />;
}

function BoundRuntimeCommands({ runId, terminal }: { runId: string; terminal: boolean }) {
  const [sessions, setSessions] = useState<RuntimeSession[]>([]);
  const [commands, setCommands] = useState<RuntimeCommand[]>([]);
  const [selected, setSelected] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState(false);
  const [uncertain, setUncertain] = useState<RuntimeCommandRequest | null>(null);
  const [refresh, setRefresh] = useState(0);
  const submitting = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const [sessionResult, commandResult] = await Promise.all([
          getRuntimeSessions(runId), getRuntimeCommands(runId),
        ]);
        if (!active) return;
        setSessions(sessionResult.items);
        setCommands(commandResult.items);
        setFresh(true);
        setError("");
        setSelected((value) => sessionResult.items.some((item) => item.session_id === value)
          ? value : sessionResult.items.find((item) => item.state === "active")?.session_id ?? "");
        setUncertain((value) => value && commandResult.items.some((item) => item.dedupe_key === value.dedupe_key)
          ? null : value);
      } catch (cause) {
        if (active) { setError(String(cause)); setFresh(false); }
      } finally {
        if (active) {
          setLoading(false);
          if (!terminal) timer = setTimeout(load, 2000);
        }
      }
    };
    void load();
    return () => { active = false; clearTimeout(timer); };
  }, [runId, terminal, refresh]);

  const session = sessions.find((item) => item.session_id === selected);
  const enabled = fresh && !terminal && !busy && !uncertain && session?.state === "active";

  const send = async (kind: RuntimeCommandRequest["kind"], approval?: RuntimeApproval) => {
    if (!enabled || !session || submitting.current || (kind === "user_message" && !message.trim())) return;
    const body: RuntimeCommandRequest = {
      kind, case_id: session.case_id, session_id: session.session_id,
      expected_session_revision: session.control_revision,
      dedupe_key: crypto.randomUUID(),
      ...(kind === "user_message" ? { content: message.trim() } : {}),
      ...(approval ? { payload: { approval_id: approval.approval_id, request_hash: approval.request_hash } } : {}),
    };
    submitting.current = true;
    setBusy(true);
    setNote("");
    setSubmitError("");
    try {
      const result = await sendRuntimeCommand(runId, body);
      if (!mounted.current) return;
      setNote(`${result.command_id}：${COMMAND_LABELS[result.status] ?? result.status}`);
      if (kind === "user_message") setMessage("");
    } catch (cause) {
      if (!mounted.current) return;
      // The response can be lost after the server commits. Retain the original
      // key for audit and disable further sends until GET proves its identity.
      if (!(cause instanceof ApiRequestError && cause.status < 500)) setUncertain(body);
      setSubmitError(String(cause));
    } finally {
      submitting.current = false;
      if (mounted.current) { setBusy(false); setFresh(false); setRefresh((value) => value + 1); }
    }
  };

  return <section className="embedded" aria-label="Runtime 交互">
    <h3 className="embed-title">Runtime 交互</h3>
    <p className="hint">消息、审批和中断会记录为人工干预，影响比较条件。接收请求不等于送达或任务成功。</p>
    {loading && <p className="hint">正在读取会话…</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {submitError && <p className="error" role="alert">{submitError}</p>}
    {note && <p className="hint" role="status">{note}</p>}
    {uncertain && <p className="error" role="alert">提交结果尚未确认，请刷新查询，不要重新发送。原始去重键：<span className="mono">{uncertain.dedupe_key}</span></p>}
    <button type="button" className="link" onClick={() => setRefresh((value) => value + 1)}>刷新交互状态</button>
    {sessions.length === 0 && !loading && <p className="hint">暂无可用交互会话。等待 Worker 启动，或查看运行错误。</p>}
    {sessions.length > 0 && <label className="inline-field">
      <span className="field-label">交互会话</span>
      <select className="control" value={selected} onChange={(event) => { setSelected(event.target.value); setMessage(""); }}>
        <option value="">选择会话…</option>
        {sessions.map((item) => <option key={item.session_id} value={item.session_id}>{item.case_id} · {item.session_id}</option>)}
      </select>
    </label>}
    {session && <>
      <dl className="kv">
        <dt>Case / Session</dt><dd className="mono">{session.case_id} / {session.session_id}</dd>
        <dt>原生 Thread / Turn</dt><dd className="mono">{session.native_thread_id ?? "未知"} / {session.active_turn_id ?? "未知"}</dd>
        <dt>控制版本</dt><dd>{session.control_revision}</dd>
      </dl>
      {!enabled && <p className="hint">当前不可发送：会话未激活、运行已结束、状态待刷新或上次请求仍待确认。</p>}
      <form onSubmit={(event) => { event.preventDefault(); void send("user_message"); }}>
        <label>补充消息<textarea className="control" value={message} maxLength={16000}
          onChange={(event) => setMessage(event.target.value)} disabled={!enabled} /></label>
        <div className="actions">
          <button type="submit" className="primary" disabled={!enabled || !message.trim()}>发送消息</button>
          <button type="button" disabled={!enabled} onClick={() => void send("interrupt")}>请求中断</button>
        </div>
      </form>
      {(session.pending_approvals ?? []).map((approval) => <div className="embedded" key={approval.approval_id}>
        <h4>待处理审批</h4>
        <pre className="terminal-log">{typeof approval.summary === "string" ? approval.summary : JSON.stringify(approval.summary, null, 2)}</pre>
        <dl className="kv"><dt>请求</dt><dd className="mono">{approval.approval_id}</dd>
          <dt>内容摘要</dt><dd className="mono">{approval.request_hash}</dd><dt>到期时间</dt><dd>{approval.expires_at}</dd></dl>
        <div className="actions">
          <button type="button" disabled={!enabled || approval.state !== "pending" || !(Date.parse(approval.expires_at) > Date.now())}
            onClick={() => void send("approve", approval)}>批准此请求</button>
          <button type="button" disabled={!enabled || approval.state !== "pending" || !(Date.parse(approval.expires_at) > Date.now())}
            onClick={() => void send("reject", approval)}>拒绝此请求</button>
        </div>
      </div>)}
    </>}
    {commands.length > 0 && <table><thead><tr><th>命令</th><th>Case</th><th>状态与确认范围</th></tr></thead>
      <tbody>{commands.map((command) => <tr key={command.id}>
        <td><span className="mono">{command.id}</span> · {KIND_LABELS[command.type] ?? command.type}</td>
        <td className="mono">{command.case_id ?? "未知"}</td>
        <td>{COMMAND_LABELS[command.status] ?? command.status}
          {command.ack_evidence?.kind && <p className="hint">{ACK_LABELS[command.ack_evidence.kind] ?? command.ack_evidence.kind}</p>}</td>
      </tr>)}</tbody></table>}
  </section>;
}
