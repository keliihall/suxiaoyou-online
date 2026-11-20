"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { cloudErrorMessage, cloudRequest } from "./cloud-api";
import type { CloudUser } from "./cloud-types";
import { Field, Loading, Modal } from "./cloud-ui";

interface MlxNode {
  configured: boolean;
  enabled: boolean;
  url: string;
  certificate_fingerprint: string;
  token_configured: boolean;
}

export default function CloudMlxNodeDialog({ user, onClose, onSaved }: { user: CloudUser; onClose: () => void; onSaved: () => void }) {
  const [node, setNode] = useState<MlxNode | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const token = useRef<HTMLInputElement>(null);
  const endpoint = `/admin/users/${encodeURIComponent(user.id)}/mlx-node`;
  useEffect(() => {
    let active = true;
    void cloudRequest<MlxNode>(endpoint).then(value => { if (active) setNode(value); }).catch(cause => { if (active) setError(cloudErrorMessage(cause)); });
    return () => { active = false; };
  }, [endpoint]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const value: Record<string, unknown> = { url: form.get("url"), enabled: form.get("enabled") === "on" };
    const newToken = token.current?.value.trim();
    const certificate = String(form.get("certificate_pem") ?? "").trim();
    if (newToken) value.token = newToken;
    if (certificate) value.certificate_pem = certificate;
    // 凭据只写入服务端；留空保留现有凭据，不进入 React state 或浏览器存储。
    if (token.current) token.current.value = "";
    setBusy(true); setError("");
    try { await cloudRequest(endpoint, { method: "PUT", body: JSON.stringify(value) }); onSaved(); }
    catch (cause) { setError(cloudErrorMessage(cause)); }
    finally { setBusy(false); }
  }
  return <Modal title={`专属 Mac 节点 · ${user.display_name || user.email}`} onClose={onClose}>
    {error && <p className="cloud-inline-error" role="alert">{error}</p>}
    {!node ? !error && <Loading label="正在读取节点配置…" /> : <form onSubmit={submit}>
      <Field label="HTTPS 节点地址"><input name="url" type="url" pattern="https://.+" required defaultValue={node.url} autoComplete="off" /></Field>
      <Field label="节点令牌"><input ref={token} name="token" type="password" autoComplete="new-password" required={!node.token_configured} /></Field>
      <Field label="节点证书 PEM"><textarea name="certificate_pem" rows={6} required={!node.configured} autoComplete="off" spellCheck={false} /></Field>
      {/* 已配置节点的令牌和证书留空则保留；更换证书时使用节点实际 leaf 证书固定身份。 */}
      {node.certificate_fingerprint && <Field label="已固定证书 SHA-256"><output className="cloud-monospace">{node.certificate_fingerprint}</output></Field>}
      <label className="cloud-checkbox-label"><input type="checkbox" name="enabled" defaultChecked={node.enabled} />启用专属 Mac 节点</label>
      <div className="cloud-form-actions"><button type="button" className="cloud-button-secondary" onClick={onClose}>取消</button><button className="cloud-button" disabled={busy}>{busy ? "保存中…" : "保存节点"}</button></div>
    </form>}
  </Modal>;
}
