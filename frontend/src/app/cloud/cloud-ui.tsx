"use client";

import { createContext, useContext, useEffect, useId, useRef, type ReactNode } from "react";
import { X, LoaderCircle } from "lucide-react";
import type { RequestRecord } from "./cloud-types";

export const CloudModalNotice = createContext<{ error: string; clear: () => void }>({ error: "", clear: () => {} });

export const number = (value: number | undefined) => new Intl.NumberFormat("zh-CN").format(value ?? 0);
export const money = (value: number | undefined) => `$${((value ?? 0) / 1_000_000).toFixed(4)}`;
export function RequestCost({ request }: { request: RequestRecord }) {
  if (request.state === "released") return <>—</>;
  if (request.state !== "settled") return <>{request.state === "unknown" ? "待核查" : "待结算"}</>;
  const image = request.source === "image" || request.price_snapshot?.unit === "image";
  const configured = image ? request.normalized_usage?.cost_status === "calculated" : request.price_snapshot?.status === "route_configured";
  if (!configured) return <>价格未知</>;
  // 价格快照采用请求时单价和对应计价规则版本，不跟随当前模型配置变化。
  // 页面保留实际模型、单价和币种数据，不展示计价规则的辅助说明。
  const price = request.price_snapshot;
  return <>{money(request.cost_microusd)}<details className="cloud-audit-details"><summary>价格依据</summary><div>{price?.provider}{price?.provider ? " / " : ""}{price?.model}</div>{image ? <div>{money(price?.image_price_microusd)} / 张</div> : <><div>输入 {money(price?.input_price_microusd_per_million)} / 百万 Token</div><div>输出 {money(price?.output_price_microusd_per_million)} / 百万 Token</div></>}<div>{price?.currency}</div></details></>;
}
export function RequestQuantity({ request, reserved = false }: { request: RequestRecord; reserved?: boolean }) {
  if (!reserved && request.state === "unknown") return <>待核查</>;
  const image = request.source === "image" || request.price_snapshot?.unit === "image";
  if (image) return <>{number(reserved ? (["reserved", "dispatched", "unknown"].includes(request.state) ? 1 : 0) : Number(request.normalized_usage?.count ?? 0))} 张</>;
  return <>{number(reserved ? request.reserved_tokens : request.total_tokens)} Token</>;
}
export function date(value?: string) { if (!value) return "—"; const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
export function size(value: number) { return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / (1024 * 1024)).toFixed(1)} MB`; }
export const statusNames: Record<string, string> = { queued: "排队中", running: "运行中", completed: "已完成", failed: "失败", cancelled: "已取消", needs_review: "待核查", reserved: "已预留", dispatched: "已发送", settled: "已结算", released: "已释放", unknown: "待核查" };
export function Status({ value }: { value: string }) { return <span className={`cloud-status cloud-status-${value}`}>{statusNames[value] ?? value}</span>; }
export function Empty({ title }: { title: string }) { return <div className="cloud-empty"><h3>{title}</h3></div>; }
export function Loading({ label = "正在加载…" }: { label?: string }) { return <div className="cloud-loading" role="status"><LoaderCircle size={18} className="cloud-spin" />{label}</div>; }
// 字段约束由原生属性及服务端验证执行；辅助说明写在调用处的代码注释中。
export function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="cloud-field"><span>{label}</span>{children}</label>; }
export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const notice = useContext(CloudModalNotice);
  const clearNotice = useRef(notice.clear);
  useEffect(() => {
    clearNotice.current();
    const previous = document.activeElement as HTMLElement | null;
    const container = ref.current;
    container?.querySelector<HTMLElement>("input,button,select,textarea")?.focus();
    function key(event: KeyboardEvent) {
      if (event.key === "Escape") closeRef.current();
      if (event.key !== "Tab" || !container) return;
      const focusable = [...container.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href]')];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("keydown", key); previous?.focus(); };
  }, []);
  return <div className="cloud-modal-shade ui-overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><div ref={ref} className="cloud-modal" role="dialog" aria-modal="true" aria-labelledby={titleId}><header><h2 id={titleId}>{title}</h2><button type="button" className="cloud-icon-button" aria-label="关闭对话框" onClick={onClose}><X size={18} /></button></header>{notice.error && <div className="cloud-inline-error" role="alert">{notice.error}</div>}{children}</div></div>;
}
export function Metric({ label, value }: { label: string; value: string }) { return <div className="cloud-metric"><span>{label}</span><strong className={value.length > 15 ? "cloud-metric-value-wide" : undefined}>{value}</strong></div>; }
