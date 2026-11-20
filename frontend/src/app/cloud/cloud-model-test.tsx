"use client";

import { useEffect, useRef, useState } from "react";
import { cloudErrorMessage, cloudRequest } from "./cloud-api";
import type { Conversation, Job, ModelRoute, RequestRecord, Workspace } from "./cloud-types";
import { modelAuthenticationConfigured, terminalJob } from "./cloud-types";
import { Loading, Metric, Modal, number, Status } from "./cloud-ui";

export default function ModelConnectionTest({ model, onClose }: { model: ModelRoute; onClose: () => void }) {
  const [job, setJob] = useState<Job | null>(null);
  const [records, setRecords] = useState<RequestRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [monitoring, setMonitoring] = useState(false);
  const [error, setError] = useState("");
  const [pollRevision, setPollRevision] = useState(0);
  const attempt = useRef<{ conversationId: string; key: string } | null>(null);
  const submitting = useRef(false);
  const mounted = useRef(true);
  const submissionController = useRef<AbortController | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; submissionController.current?.abort(); }; }, []);
  const jobId = job?.id;
  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    setMonitoring(true); setError("");
    async function poll() {
      try {
        const current = await cloudRequest<Job>(`/jobs/${jobId}`, { signal: controller.signal });
        if (stopped) return;
        setJob(current);
        if (terminalJob(current.state)) {
          const ledger = await cloudRequest<RequestRecord[]>("/usage/requests", { signal: controller.signal });
          if (!stopped) { setRecords(ledger.filter(record => record.job_id === jobId)); setMonitoring(false); }
          return;
        }
        timer = setTimeout(() => void poll(), 1000);
      } catch (cause) {
        if (!stopped) { /* 恢复查询会继续读取原任务，不会再次调用模型。 */ setError(`查询中断：${cloudErrorMessage(cause)}`); setMonitoring(false); }
      }
    }
    void poll();
    return () => { stopped = true; controller.abort(); if (timer) clearTimeout(timer); };
  }, [jobId, pollRevision]);
  async function start() {
    if (submitting.current || monitoring || (job && !terminalJob(job.state))) return;
    if (!model.enabled || !modelAuthenticationConfigured(model) || model.provider === "mock") { setError("请先启用已配置认证方式的模型路由。"); return; }
    submitting.current = true; setBusy(true); setError(""); setRecords([]);
    if (job) { attempt.current = null; setJob(null); }
    const controller = new AbortController();
    submissionController.current = controller;
    const signal = controller.signal;
    try {
      if (!attempt.current) {
        const workspaces = await cloudRequest<Workspace[]>("/workspaces", { signal });
        let workspace = workspaces.find(item => item.name === "模型连接测试");
        if (!workspace) workspace = await cloudRequest<Workspace>("/workspaces", { method: "POST", body: JSON.stringify({ name: "模型连接测试" }), signal });
        const conversation = await cloudRequest<Conversation>("/conversations", { method: "POST", body: JSON.stringify({ workspace_id: workspace.id, title: `连接测试 · ${model.name}` }), signal });
        attempt.current = { conversationId: conversation.id, key: crypto.randomUUID() };
      }
      const result = await cloudRequest<Job>("/jobs", { method: "POST", body: JSON.stringify({ conversation_id: attempt.current.conversationId, prompt: "请只回答“连接正常”", model_id: model.id, max_output_tokens: 128, file_ids: [], idempotency_key: attempt.current.key }), signal });
      if (mounted.current) setJob(result);
    } catch (cause) {
      if (mounted.current) /* 可重试提交；同一次测试会复用幂等凭证。 */ setError(cloudErrorMessage(cause));
    } finally {
      submitting.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  const settled = records.filter(record => record.state === "settled");
  return <Modal title={`连接测试 · ${model.name}`} onClose={onClose}>
    {/* 将使用当前账户自己的额度，通过服务端任务调用真实模型，最多输出 128 Token。测试会消耗实际 Token，并写入用量账本。 */}
    {/* 关闭窗口不会取消已提交的任务；可在“模型连接测试”工作区和任务记录中继续查看。 */}
    {job && <div className="cloud-card"><div className="cloud-card-heading"><h3>测试任务</h3><Status value={job.state} /></div><div className="cloud-card-body"><p className="cloud-muted">{job.answer || (job.state === "completed" ? "模型未返回文本，请查看任务记录。" : job.error || (terminalJob(job.state) ? "任务已结束，未返回模型回答。" : "等待任务执行与模型回答…"))}</p>{job.error && job.answer && <p className="cloud-inline-error">{job.error}</p>}<small className="cloud-table-sub">任务 {job.id}</small></div></div>}
    {monitoring && <Loading label="正在读取任务与用量账本…" />}
    {job && terminalJob(job.state) && !monitoring && <>{/* 原注脚：`${settled.length} 个已结算模型请求`。请求数量作为业务指标单独显示。 */}<Metric label="已结算 Token" value={number(settled.reduce((total, record) => total + record.total_tokens, 0))} /><Metric label="已结算请求" value={number(settled.length)} />{/* 请在“用量与核查”中处理。 */}{records.some(record => record.state === "unknown") && <p className="cloud-inline-warning">实际用量待核查，对应预留额度暂时保留。</p>}{!records.length && !error && <p>没有已发生的模型请求记录。</p>}</>}
    {error && <p className="cloud-inline-error" role="alert">{error}</p>}
    <div className="cloud-form-actions"><button className="cloud-button-secondary" onClick={onClose}>关闭</button>{job && error && <button className="cloud-button-secondary" disabled={monitoring} onClick={() => setPollRevision(value => value + 1)}>恢复查询</button>}{(!job || terminalJob(job.state)) && <button className="cloud-button" disabled={busy || monitoring} onClick={() => void start()}>{busy ? "提交中…" : job ? "再次测试（消耗 Token）" : "开始连接测试"}</button>}</div>
  </Modal>;
}

