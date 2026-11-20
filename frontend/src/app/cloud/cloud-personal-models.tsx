"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Pencil, Plus, RefreshCw, Trash2, Unplug } from "lucide-react";
import { cloudErrorMessage, cloudRequest, postCloud } from "./cloud-api";
import type { ModelRoute } from "./cloud-types";
import { Empty, Field, Loading, Modal } from "./cloud-ui";
import ModelDialog from "./cloud-model-dialog";
import ModelConnectionTest from "./cloud-model-test";
import { modelAuthenticationConfigured } from "./cloud-types";
import Image from "next/image";

export function CloudPersonalModels({ onError }: { onError: (error: unknown) => void }) {
  const [models, setModels] = useState<ModelRoute[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState<{ model?: ModelRoute } | null>(null);
  const [remove, setRemove] = useState<ModelRoute | null>(null);
  const [chatTest, setChatTest] = useState<ModelRoute | null>(null);
  const [imageTest, setImageTest] = useState<ModelRoute | null>(null);
  const refresh = useCallback(async () => {
    try { setModels(await cloudRequest<ModelRoute[]>("/user/models")); }
    catch (failure) { onError(failure); }
    finally { setLoading(false); }
  }, [onError]);
  useEffect(() => { void refresh(); }, [refresh]);
  return <section className="cloud-page-content"><div className="cloud-page-heading"><h1>个人模型</h1><div className="cloud-row-actions"><button className="cloud-icon-button" aria-label="刷新个人模型" onClick={() => void refresh()}><RefreshCw size={16} /></button><button className="cloud-button" onClick={() => setEdit({})}><Plus size={16} />添加个人模型</button></div></div>
    {loading ? <Loading label="正在读取个人模型…" /> : <div className="cloud-card"><div className="cloud-table-wrap"><table className="cloud-table"><thead><tr><th>名称 / 路由 ID</th><th>服务商 / 模型</th><th>用途</th><th>认证状态</th><th>状态</th><th>操作</th></tr></thead><tbody>{models.map(model => <tr key={model.id}><td><strong>{model.name}</strong><small className="cloud-table-sub">{model.id}</small></td><td>{model.provider}<small className="cloud-table-sub">{model.model}</small></td><td>{model.kind === "image" ? "图像生成" : "对话与 Agent"}</td><td><span className={`cloud-status ${modelAuthenticationConfigured(model) ? "cloud-status-completed" : "cloud-status-needs_review"}`}>{model.authentication_mode === "none" ? "无需身份验证" : modelAuthenticationConfigured(model) ? "已配置" : "未配置"}</span></td><td><span className={`cloud-status ${model.enabled ? "cloud-status-completed" : "cloud-status-cancelled"}`}>{model.enabled ? "已启用" : "已停用"}</span></td><td><div className="cloud-row-actions"><button className="cloud-button-small" onClick={() => setEdit({ model })}><Pencil size={13} />编辑</button>{model.kind !== "image" && <button className="cloud-button-small" disabled={!model.enabled || !modelAuthenticationConfigured(model)} onClick={() => setChatTest(model)}><Unplug size={13} />连接测试</button>}{model.kind === "image" && <button className="cloud-button-small" disabled={!model.enabled || !modelAuthenticationConfigured(model)} onClick={() => setImageTest(model)}>生成测试图</button>}<button className="cloud-icon-button" aria-label={`删除个人模型 ${model.name}`} onClick={() => setRemove(model)}><Trash2 size={15} /></button></div></td></tr>)}</tbody></table>{!models.length && <Empty title="暂无个人模型" />}</div></div>}
    {edit && <ModelDialog personal model={edit.model} busy={busy} onClose={() => setEdit(null)} onSubmit={async values => { setBusy(true); try { if (edit.model) await cloudRequest(`/user/models/${encodeURIComponent(edit.model.id)}`, { method: "PUT", body: JSON.stringify(values) }); else await postCloud("/user/models", values); setEdit(null); await refresh(); } catch (failure) { onError(failure); } finally { setBusy(false); } }} />}
    {remove && <Modal title="删除个人模型？" onClose={() => setRemove(null)}><p>{remove.name} · {remove.id}</p><div className="cloud-form-actions"><button className="cloud-button-secondary" onClick={() => setRemove(null)}>取消</button><button className="cloud-button cloud-button-danger" disabled={busy} onClick={async () => { setBusy(true); try { await cloudRequest(`/user/models/${encodeURIComponent(remove.id)}`, { method: "DELETE" }); setRemove(null); await refresh(); } catch (failure) { onError(failure); } finally { setBusy(false); } }}>确认删除</button></div></Modal>}
    {chatTest && <ModelConnectionTest model={chatTest} onClose={() => setChatTest(null)} />}
    {imageTest && <CloudImageTest model={imageTest} onClose={() => setImageTest(null)} />}
    {/* Personal model credentials are write-only, encrypted by the server and bound to the current account. */}
  </section>;
}

export function CloudImageTest({ model, onClose }: { model: ModelRoute; onClose: () => void }) {
  const [prompt, setPrompt] = useState("一只坐在书桌旁的小猫，柔和的自然光，插画风格");
  const [busy, setBusy] = useState(false);
  const [image, setImage] = useState("");
  const [error, setError] = useState("");
  const operation = useRef<{ prompt: string; id: string } | null>(null);
  async function generate() {
    if (!prompt.trim() || busy) return;
    if (!operation.current || operation.current.prompt !== prompt.trim()) operation.current = { prompt: prompt.trim(), id: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      const result = await postCloud<{ data: { b64_json: string }[] }>("/images/generations", { model: model.id, prompt: prompt.trim(), operation_id: operation.current.id });
      if (!result.data?.[0]?.b64_json) throw new Error("服务商没有返回可显示的图片。");
      setImage(`data:image/png;base64,${result.data[0].b64_json}`);
      operation.current = null;
    } catch (failure) { setError(cloudErrorMessage(failure)); }
    finally { setBusy(false); }
  }
  return <Modal title={`图像测试 · ${model.name}`} onClose={onClose}><Field label="图像提示词"><textarea value={prompt} onChange={event => setPrompt(event.target.value)} rows={4} required maxLength={10000} /></Field>{image && <div className="cloud-card"><Image unoptimized src={image} alt="模型生成的测试图片" width={1024} height={1024} className="cloud-test-image" /></div>}{error && <p role="alert" className="cloud-inline-error">{error}</p>}<div className="cloud-form-actions"><button className="cloud-button-secondary" onClick={onClose}>关闭</button><button className="cloud-button" disabled={busy || !prompt.trim()} onClick={() => void generate()}>{busy ? "正在生成…" : "确认生成 1 张（消耗图片额度）"}</button></div>{/* This explicit action invokes the real image provider and records one image in the central ledger. */}</Modal>;
}
