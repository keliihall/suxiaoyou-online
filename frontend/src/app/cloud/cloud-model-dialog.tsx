"use client";

import { useRef, useState, type FormEvent } from "react";
import type { ModelRoute } from "./cloud-types";
import { Field, Modal } from "./cloud-ui";

export default function ModelDialog({ model, busy, onClose, onSubmit, personal = false }: { model?: ModelRoute; busy: boolean; onClose: () => void; onSubmit: (values: Record<string, unknown>) => void; personal?: boolean }) {
  const [kind, setKind] = useState<"chat" | "image">(model?.kind ?? "chat");
  const [strict, setStrict] = useState(model?.strict_quota ?? false);
  const [enabled, setEnabled] = useState(model?.enabled ?? false);
  const [clearKey, setClearKey] = useState(false);
  const [provider, setProvider] = useState(model?.provider ?? "deepseek");
  const [headerAction, setHeaderAction] = useState<"preserve" | "replace" | "clear">("preserve");
  const [headerRows, setHeaderRows] = useState([0]);
  const nextHeaderId = useRef(1);
  const headerInputs = useRef<Record<number, HTMLInputElement | null>>({});
  const [unauthenticated, setUnauthenticated] = useState(model?.allow_unauthenticated ?? false);
  const [error, setError] = useState("");
  const keyInput = useRef<HTMLInputElement>(null);
  const headersAllowed = kind === "chat" && (provider === "openai" || provider.startsWith("custom_"));
  const unauthenticatedAllowed = kind === "chat" && provider.startsWith("custom_");
  function clearHeaderInputs() {
    for (const input of Object.values(headerInputs.current)) if (input) input.value = "";
  }
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const key = String(form.get("api_key") ?? "").trim();
    const environment = personal ? "" : String(form.get("api_key_env") ?? "").trim();
    const headerPairs = headerAction === "replace" ? headerRows.map(id => [String(form.get(`header_name_${id}`) ?? "").trim(), String(form.get(`header_value_${id}`) ?? "")]) : [];
    // The secret never enters React state or browser storage, and is cleared
    // before awaiting the write-only API. Empty values preserve existing keys.
    if (keyInput.current) keyInput.current.value = "";
    clearHeaderInputs();
    form.delete("api_key");
    for (const id of headerRows) form.delete(`header_value_${id}`);
    setError("");
    let capabilities: Record<string, unknown>;
    try {
      capabilities = JSON.parse(String(form.get("capabilities") || "{}"));
      if (!capabilities || typeof capabilities !== "object" || Array.isArray(capabilities)) throw new Error("object required");
    } catch { setError("模型能力必须填写有效的 JSON 对象。"); return; }
    const names = new Set<string>();
    for (const [name, value] of headerPairs) {
      if (!name || !value || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name) || names.has(name.toLowerCase())) { setError("请填写有效且不重复的请求头名称和值。"); return; }
      names.add(name.toLowerCase());
    }
    if (headerPairs.length && !headersAllowed) { setError("自定义请求头仅支持 OpenAI 或 custom_ 服务商的对话模型。"); return; }
    const hasHeaders = headerAction === "replace" ? headerPairs.length > 0 : headerAction !== "clear" && !!model?.custom_headers_configured;
    const hasKey = !!environment || (!clearKey && (!!key || (!!model?.key_configured && model.credential_source !== "environment")));
    if (unauthenticated && (!unauthenticatedAllowed || hasKey || hasHeaders)) { setError("无需身份验证仅限 custom_ 服务商，且必须清除 API Key、环境变量和请求头。"); return; }
    if (enabled && !hasKey && !hasHeaders && !unauthenticated && provider !== "mock") { setError("请配置 API Key 或自定义请求头，再向用户开放此模型。"); return; }
    const values: Record<string, unknown> = { id: model?.id ?? form.get("id"), kind, image_price_microusd: Number(form.get("image_price") || 0), name: form.get("name"), provider, model: form.get("model"), api_key_env: environment, clear_api_key: clearKey, allow_unauthenticated: unauthenticated, base_url: form.get("base_url") || null, strict_quota: kind === "chat" && strict, enabled, input_price_microusd_per_million: Number(form.get("input_price")), output_price_microusd_per_million: Number(form.get("output_price")) };
    values.capabilities = capabilities;
    if (key && !clearKey) values.api_key = key;
    if (headerAction === "replace") values.custom_headers = Object.fromEntries(headerPairs);
    if (headerAction === "clear") values.custom_headers = {};
    onSubmit(values);
  }
  return <Modal title={model ? "编辑模型路由" : "添加模型路由"} onClose={onClose}>
    <form onSubmit={submit}>
      {/* DeepSeek 官方兼容接口：https://api.deepseek.com/v1 · 当前模型：deepseek-flash。可在官方文档 https://api-docs.deepseek.com/quick_start/pricing 核对模型与价格。 */}
      <Field label="模型用途"><select value={kind} onChange={event => { const value = event.target.value as "chat" | "image"; setKind(value); clearHeaderInputs(); if (value === "image") { setUnauthenticated(false); if (!model?.custom_headers_configured) setHeaderAction("preserve"); } if (!model) setProvider(value === "image" ? "siliconflow" : "deepseek"); }}><option value="chat">对话与 Agent</option><option value="image">图像生成</option></select></Field>
      <div className="cloud-form-grid">
        {/* 仅使用字母、数字、短横线或下划线。 */}{/* 原输入提示：office-assistant */}<Field label="路由 ID"><input name="id" required pattern="[A-Za-z0-9_-]+" maxLength={personal ? 60 : 100} defaultValue={model?.id} disabled={!!model} /></Field>
        {/* 原输入提示：办公助手 */}<Field label="展示名称"><input name="name" required maxLength={100} defaultValue={model?.name} /></Field>
        {/* 原输入提示：deepseek / openai / anthropic / google */}<Field label="服务商标识"><input name="provider" required value={provider} onChange={event => { const value = event.target.value; setProvider(value); if (!value.startsWith("custom_")) setUnauthenticated(false); if (value !== "openai" && !value.startsWith("custom_") && !model?.custom_headers_configured) { clearHeaderInputs(); setHeaderAction("preserve"); } }} /></Field>
        {/* 原输入提示：deepseek-flash */}<Field label="实际模型 ID"><input key={`model-${kind}`} name="model" required defaultValue={model?.model ?? (kind === "image" ? "Kwai-Kolors/Kolors" : "deepseek-flash")} /></Field>
      </div>
      {/* 已配置密钥。留空保留，填写新值替换；服务器加密保存，页面不会回显。填写供应商 API Key。服务器加密保存，页面不会回显，也不会保存在浏览器。 */}
      {/* 原输入提示：留空保留现有密钥；请输入 API Key。 */}<Field label="API Key">
        <input ref={keyInput} name="api_key" aria-label="API Key" type="password" autoComplete="new-password" spellCheck={false} maxLength={8192} disabled={clearKey} />
      </Field>
      {model?.credential_source === "managed" && <label className="cloud-checkbox-label"><input type="checkbox" checked={clearKey} onChange={event => { setClearKey(event.target.checked); if (event.target.checked) { if (keyInput.current) keyInput.current.value = ""; if (!model.api_key_env && !model.custom_headers_configured && headerAction !== "replace" && !unauthenticated) setEnabled(false); } }} />删除已保存的 API Key</label>}
      {clearKey && <p>将删除管理界面保存的密钥。</p>}{/* 若服务器没有可用的环境变量密钥，必须关闭模型路由；服务器会校验后保存。 */}
      {!personal && <details className="cloud-audit-details"><summary>高级配置（可选）</summary>{/* 仅在使用服务器环境变量时填写 SUXIAOYOU_MODEL_ 开头的变量名，不要在此填写密钥值。界面保存的密钥优先使用。 */}{/* 原输入提示：SUXIAOYOU_MODEL_DEEPSEEK_KEY */}<Field label="密钥环境变量名（可选）"><input name="api_key_env" pattern="SUXIAOYOU_MODEL_[A-Z0-9_]+" defaultValue={model?.api_key_env ?? ""} autoComplete="off" /></Field></details>}
      {(headersAllowed || model?.custom_headers_configured) && <details className="cloud-audit-details"><summary>自定义请求头</summary>
        {model?.custom_headers_configured && <Field label="请求头状态"><span>已配置</span></Field>}
        <Field label="请求头操作"><select value={headerAction} onChange={event => { clearHeaderInputs(); setHeaderAction(event.target.value as typeof headerAction); }}><option value="preserve">保留已保存请求头</option><option value="replace">替换全部请求头</option><option value="clear">清除全部请求头</option></select></Field>
        {headerAction === "replace" && <>{headerRows.map((id, index) => <div className="cloud-form-grid" key={id}>
          <Field label={`请求头名称 ${index + 1}`}><input name={`header_name_${id}`} autoComplete="off" spellCheck={false} maxLength={128} /></Field>
          <Field label={`请求头值 ${index + 1}`}><input ref={input => { headerInputs.current[id] = input; }} name={`header_value_${id}`} type="password" autoComplete="new-password" spellCheck={false} maxLength={4096} /></Field>
          {headerRows.length > 1 && <button type="button" className="cloud-button-small" onClick={() => { if (headerInputs.current[id]) headerInputs.current[id]!.value = ""; delete headerInputs.current[id]; setHeaderRows(rows => rows.filter(row => row !== id)); }}>移除请求头 {index + 1}</button>}
        </div>)}<button type="button" className="cloud-button-secondary" disabled={headerRows.length >= 32} onClick={() => setHeaderRows(rows => [...rows, nextHeaderId.current++])}>添加请求头</button></>}
        {/* Header values stay only in uncontrolled password inputs and the transient request body. Omitted/null preserves, a nonempty object replaces all headers, and {} explicitly clears. The server validates blocked/control headers; no stored names or values are returned. */}
      </details>}
      {unauthenticatedAllowed && <label className="cloud-checkbox-label"><input type="checkbox" checked={unauthenticated} onChange={event => setUnauthenticated(event.target.checked)} />无需身份验证</label>}
      {kind === "chat" && <details className="cloud-audit-details"><summary>模型能力</summary><Field label="能力配置（JSON）"><textarea name="capabilities" rows={6} spellCheck={false} defaultValue={JSON.stringify(model?.capabilities || {}, null, 2)} /></Field>{/* {} uses provider/catalog defaults. Boolean overrides: function_calling, vision, reasoning, json_output, prompt_caching. max_context/max_output: integers 1..2000000; max_output may be null. */}</details>}
      {/* 原输入提示：https://api.deepseek.com/v1 */}<Field label="兼容接口 Base URL（可选）"><input key={`base-${kind}`} name="base_url" type="url" defaultValue={model ? model.base_url ?? "" : kind === "image" ? "https://api.siliconflow.cn/v1" : "https://api.deepseek.com/v1"} /></Field>
      {kind === "image" ? <Field label="单张图片价格（微美元）"><input name="image_price" type="number" min={0} step={1} required defaultValue={model?.image_price_microusd ?? 0} /></Field> : <div className="cloud-form-grid">
        <Field label="输入价格（微美元 / 百万 Token）"><input name="input_price" type="number" min={0} step={1} required defaultValue={model?.input_price_microusd_per_million ?? 0} /></Field>
        <Field label="输出价格（微美元 / 百万 Token）"><input name="output_price" type="number" min={0} step={1} required defaultValue={model?.output_price_microusd_per_million ?? 0} /></Field>
      </div>}
      <label className="cloud-checkbox-label"><input type="checkbox" name="enabled" checked={enabled} onChange={event => setEnabled(event.target.checked)} />向用户开放此模型</label>
      {kind === "chat" && <label className="cloud-checkbox-label"><input type="checkbox" checked={strict} onChange={event => setStrict(event.target.checked)} />启用严格额度策略</label>}
      {/* 严格模式需要服务器提供可信输入上界，否则服务端会拒绝该配置。软额度会按实际返回用量结算，允许预估误差。 */}
      {error && <p className="cloud-inline-error" role="alert">{error}</p>}
      <div className="cloud-form-actions"><button type="button" className="cloud-button-secondary" onClick={onClose}>取消</button><button className="cloud-button" disabled={busy}>{busy ? "保存中…" : "保存模型路由"}</button></div>
    </form>
  </Modal>;
}

