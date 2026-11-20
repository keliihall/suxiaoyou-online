"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, ArrowUpRight, Cpu, Gauge, Loader2, LogOut, Menu, PanelLeftClose, PanelLeftOpen, RefreshCw, Settings2, ShieldCheck, Sparkles, X } from "lucide-react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Dialog, DialogClose, DialogOverlay, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import { CloudError, clearCloudSession, cloudErrorMessage, cloudRequest, postCloud, setCloudSession } from "./cloud-api";
import Link from "next/link";
import Image from "next/image";
import type { AuthData, RequestRecord, Usage } from "./cloud-types";
import { CloudModalNotice, date, Empty, Field, Loading, Metric, Modal, money, number, RequestCost, RequestQuantity, Status } from "./cloud-ui";
import CloudAdmin from "./cloud-admin";
import { activateCloudIdentity, leaveCloudWorkspace, userStorage } from "@/lib/cloud-session";
import { CloudPersonalModels } from "./cloud-personal-models";
import { ThemeProvider } from "@/components/providers/theme-provider";
import { CloudAppearanceProvider } from "@/components/providers/cloud-appearance-provider";
import { CloudLoginCaptcha } from "./cloud-login-captcha";

type Tab = "usage" | "admin" | "models";
type Notice = { text: string; type: "error" | "success" | "info" } | null;
const SIDEBAR_COLLAPSED_KEY = "cloud-sidebar-collapsed";

export default function CloudApp() {
  const [auth, setAuth] = useState<AuthData | null>();
  const [bootError, setBootError] = useState("");
  const [expired, setExpired] = useState(() => typeof window !== "undefined" && new URLSearchParams(window.location.search).get("login") === "expired");
  const [loginMessage, setLoginMessage] = useState("");
  const expire = useCallback(() => { clearCloudSession(); setAuth(null); setExpired(true); void leaveCloudWorkspace("/cloud?login=expired"); }, []);
  const acceptAuth = useCallback((data: AuthData) => { activateCloudIdentity(data.user, data.csrf_token); setCloudSession(data.csrf_token, expire); setAuth(data); setExpired(false); setLoginMessage(""); }, [expire]);
  const boot = useCallback(async () => {
    setBootError("");
    try { acceptAuth(await cloudRequest<AuthData>("/auth/me")); }
    catch (error) { if (error instanceof CloudError && error.status === 401) setAuth(null); else setBootError(cloudErrorMessage(error)); }
  }, [acceptAuth]);
  useEffect(() => { void boot(); return () => clearCloudSession(); }, [boot]);
  useEffect(() => {
    if (auth && new URLSearchParams(window.location.search).get("manage") !== "1") window.location.replace("/c/new");
  }, [auth]);
  const content = auth === undefined
    ? <div className="cloud-root cloud-boot"><Brand compact />{bootError ? <div className="cloud-card"><p role="alert">{bootError}</p><button className="cloud-button" onClick={boot}>重新连接</button></div> : <Loading label="正在连接你的云工作台…" />}</div>
    : !auth
      ? <Login onLogin={acceptAuth} expired={expired} message={loginMessage} />
      : <CloudAccountApp key={auth.user.id} auth={auth} onRefreshAuth={acceptAuth} onPasswordChanged={() => { clearCloudSession(); setAuth(null); setLoginMessage("密码已更新，请使用新密码重新登录。"); }} onLogout={() => { clearCloudSession(); setAuth(null); }} />;
  return <ThemeProvider key={auth?.user.id ?? "anonymous"}><CloudAppearanceProvider userId={auth?.user.id}>{content}</CloudAppearanceProvider></ThemeProvider>;
}

function Brand({ compact = false }: { compact?: boolean }) { return <div className="cloud-brand"><span className="cloud-brand-symbol"><Image src="/favicon.svg" alt="" width={40} height={40} priority /></span><span className="cloud-brand-name">苏小有{!compact && <small>云工作台</small>}</span></div>; }

function Login({ onLogin, expired, message }: { onLogin: (auth: AuthData) => void; expired: boolean; message: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(expired ? "登录已失效，请重新登录。" : "");
  const [captchaOpen, setCaptchaOpen] = useState(false);
  const [captchaAttempt, setCaptchaAttempt] = useState(0);
  const attemptRef = useRef(0);
  const credentialsRef = useRef<{ email: string; password: string; attempt: number } | null>(null);
  const loginButtonRef = useRef<HTMLButtonElement>(null);
  const submittingRef = useRef(false);
  const loginRequestRef = useRef<AbortController | null>(null);
  const loginTimeoutRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      attemptRef.current += 1;
      credentialsRef.current = null;
      loginRequestRef.current?.abort();
      if (loginTimeoutRef.current !== null) window.clearTimeout(loginTimeoutRef.current);
      loginTimeoutRef.current = null;
    };
  }, []);
  const cancelAttempt = useCallback(() => {
    attemptRef.current += 1;
    credentialsRef.current = null;
    loginRequestRef.current?.abort();
    loginRequestRef.current = null;
    if (loginTimeoutRef.current !== null) window.clearTimeout(loginTimeoutRef.current);
    loginTimeoutRef.current = null;
    submittingRef.current = false;
    setBusy(false);
    setCaptchaOpen(false);
  }, []);
  const authenticate = useCallback(async (proof: string, attempt: number) => {
    const credentials = credentialsRef.current;
    if (!mountedRef.current || submittingRef.current || !credentials || credentials.attempt !== attempt || attemptRef.current !== attempt) return;
    submittingRef.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    loginRequestRef.current = controller;
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
    loginTimeoutRef.current = timeout;
    try {
      const auth = await cloudRequest<AuthData>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: credentials.email, password: credentials.password, captcha_proof: proof }),
        signal: controller.signal,
      });
      if (!mountedRef.current || controller.signal.aborted || loginRequestRef.current !== controller || attemptRef.current !== attempt) return;
      credentialsRef.current = null;
      onLogin(auth);
    } catch (error) {
      if (!mountedRef.current || loginRequestRef.current !== controller || attemptRef.current !== attempt || (controller.signal.aborted && !timedOut)) return;
      setError(timedOut ? "登录超时，请重新验证后重试。" : error instanceof CloudError && error.status === 401 ? "邮箱或密码不正确，请重新输入。" : error instanceof CloudError && error.code.startsWith("captcha_") ? error.message : cloudErrorMessage(error));
      cancelAttempt();
    } finally {
      window.clearTimeout(timeout);
      if (loginRequestRef.current === controller) {
        loginRequestRef.current = null;
        loginTimeoutRef.current = null;
        submittingRef.current = false;
        if (mountedRef.current) setBusy(false);
      }
    }
  }, [cancelAttempt, onLogin]);
  const acceptProof = useCallback((proof: string | null) => {
    if (proof) void authenticate(proof, captchaAttempt);
  }, [authenticate, captchaAttempt]);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submittingRef.current || credentialsRef.current || !event.currentTarget.reportValidity()) return;
    const form = new FormData(event.currentTarget);
    const attempt = ++attemptRef.current;
    credentialsRef.current = { email: String(form.get("email") ?? ""), password: String(form.get("password") ?? ""), attempt };
    setError("");
    setCaptchaAttempt(attempt);
    setCaptchaOpen(true);
  }
  // 原品牌辅助文案：从一份文件，到一件事的完成。把材料交给小有，让想法、分析和结果在你的私人工作区里有序发生。
  // 整理材料 · 说出想法 · 收获结果。文件与任务按账户隔离 · 用量清晰可查。
  return <div className="cloud-root cloud-login"><main className="cloud-login-form" aria-label="登录">
    <Brand compact />
    <div className="cloud-login-box">
      {/* 欢迎回来 */}
      {/* 登录苏小有，继续你的工作。 */}
      <form onSubmit={submit} aria-busy={busy}>
        {message && <div className="cloud-login-success" role="status">{message}</div>}
        <Field label="邮箱"><input name="email" type="email" autoComplete="username" placeholder="you@example.com" required autoFocus disabled={busy} /></Field>
        <Field label="密码"><input name="password" type="password" autoComplete="current-password" placeholder="请输入密码" required disabled={busy} /></Field>
        {error && <div className="cloud-inline-error" role="alert">{error}</div>}
        <div className="cloud-login-actions"><button ref={loginButtonRef} className="cloud-button cloud-login-submit" disabled={busy} aria-label={busy ? "正在登录…" : "登录工作台"} title={busy ? "正在登录…" : "登录工作台"}>{busy ? <Loader2 className="cloud-spin" size={18} aria-hidden="true" /> : <ArrowRight size={18} aria-hidden="true" />}</button></div>
      </form>
      {/* 账户由管理员创建。需要访问权限或重置密码，请联系管理员。 */}
    </div>
    {/* 苏小有 · 让每一件小事，都有所成 */}
  </main>
    <Dialog open={captchaOpen} onOpenChange={open => { if (!open) cancelAttempt(); }}>
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content className="cloud-root cloud-login-verification" aria-describedby={undefined} aria-busy={busy} onCloseAutoFocus={event => { event.preventDefault(); if (mountedRef.current) loginButtonRef.current?.focus(); }}>
          <header><DialogTitle>安全验证</DialogTitle><DialogClose asChild><button type="button" className="cloud-icon-button cloud-login-verification-close" aria-label="关闭验证" title="关闭验证"><X size={18} aria-hidden="true" /></button></DialogClose></header>
          {captchaOpen && <CloudLoginCaptcha key={captchaAttempt} disabled={busy} resetKey={0} onProofChange={acceptProof} />}
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  </div>;
}

function CloudAccountApp({ auth, onLogout, onRefreshAuth, onPasswordChanged }: { auth: AuthData; onLogout: () => void; onRefreshAuth: (auth: AuthData) => void; onPasswordChanged: () => void }) {
  const [tab, setTab] = useState<Tab>(() => typeof window !== "undefined" && new URLSearchParams(window.location.search).get("tab") === "models" ? "models" : auth.user.role === "admin" ? "admin" : "usage");
  // Identity is activated before this user-keyed component mounts; only the desktop preference is persisted.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => userStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true");
  const [menuOpen, setMenuOpen] = useState(false);
  const mobileMenuRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [requests, setRequests] = useState<RequestRecord[]>([]);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const notifyError = useCallback((error: unknown) => setNotice({ type: "error", text: cloudErrorMessage(error) }), []);
  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([cloudRequest<Usage>("/usage"), cloudRequest<RequestRecord[]>("/usage/requests")]);
    if (results[0].status === "fulfilled") setUsage(results[0].value); else notifyError(results[0].reason);
    if (results[1].status === "fulfilled") setRequests(results[1].value); else notifyError(results[1].reason);
  }, [notifyError]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 801px)");
    const closeMobileMenu = (event: MediaQueryListEvent) => { if (event.matches) setMenuOpen(false); };
    desktop.addEventListener("change", closeMobileMenu);
    return () => desktop.removeEventListener("change", closeMobileMenu);
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    const sidebar = sidebarRef.current;
    const menuButton = mobileMenuRef.current;
    const controls = () => Array.from(sidebar?.querySelectorAll<HTMLElement>('a[href], button:not([disabled])') ?? []).filter(element => element.getClientRects().length > 0);
    controls()[0]?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); setMenuOpen(false); }
      if (event.key !== "Tab") return;
      const items = controls();
      const first = items[0];
      const last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); menuButton?.focus(); };
  }, [menuOpen]);
  function toggleSidebar() {
    const next = !sidebarCollapsed;
    setSidebarCollapsed(next);
    userStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
  }
  return <CloudModalNotice.Provider value={{ error: notice?.type === "error" ? notice.text : "", clear: () => setNotice(null) }}><div className="cloud-root cloud-shell">
    {menuOpen && <button className="cloud-sidebar-scrim ui-overlay" aria-label="关闭导航遮罩" tabIndex={-1} onClick={() => setMenuOpen(false)} />}
    <aside ref={sidebarRef} id="cloud-account-sidebar" className={`cloud-sidebar ${sidebarCollapsed ? "is-collapsed" : ""} ${menuOpen ? "is-open" : ""}`} aria-label="账户导航">
      <div className="cloud-sidebar-header"><Brand /><button className="cloud-sidebar-toggle cloud-icon-button" onClick={toggleSidebar} aria-label={sidebarCollapsed ? "展开导航" : "收起导航"} title={sidebarCollapsed ? "展开导航" : "收起导航"} aria-expanded={!sidebarCollapsed} aria-controls="cloud-account-sidebar">{sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}</button><button className="cloud-sidebar-close cloud-icon-button" onClick={() => setMenuOpen(false)} aria-label="收起导航" title="收起导航"><X size={18} /></button></div>
      <Link href="/c/new" className="cloud-new-chat" title="完整工作台" aria-label="完整工作台"><Sparkles size={18} /><span className="cloud-sidebar-label">完整工作台</span><ArrowUpRight className="cloud-new-chat-arrow" size={16} /></Link>
      <nav className="cloud-nav" aria-label="账户与管理导航">
        <button className={tab === "usage" ? "is-active" : ""} aria-current={tab === "usage" ? "page" : undefined} title="用量与额度" aria-label="用量与额度" onClick={() => { setTab("usage"); setMenuOpen(false); }}><Gauge size={18} /><span className="cloud-sidebar-label">用量与额度</span></button>
        <button className={tab === "models" ? "is-active" : ""} aria-current={tab === "models" ? "page" : undefined} title="个人模型" aria-label="个人模型" onClick={() => { setTab("models"); setMenuOpen(false); }}><Cpu size={18} /><span className="cloud-sidebar-label">个人模型</span></button>
        {auth.user.role === "admin" && <button className={tab === "admin" ? "is-active" : ""} aria-current={tab === "admin" ? "page" : undefined} title="管理后台" aria-label="管理后台" onClick={() => { setTab("admin"); setMenuOpen(false); }}><ShieldCheck size={18} /><span className="cloud-sidebar-label">管理后台</span></button>}
        <Link href="/settings" title="设置" aria-label="设置"><Settings2 size={18} /><span className="cloud-sidebar-label">设置</span></Link>
      </nav>
      <div className="cloud-history" />
      {usage && <div className="cloud-sidebar-quota"><strong>{number(usage.used_tokens)}<small> / {number(usage.limit_tokens)} Token</small></strong><small>剩余 {number(usage.remaining_tokens)} · 已预留 {number(usage.reserved_tokens)}</small></div>}
      <div className="cloud-account"><button className="cloud-account-info" onClick={() => { setMenuOpen(false); setPasswordOpen(true); }} title="修改密码" aria-label="修改密码"><span className="cloud-avatar" aria-hidden="true">{(auth.user.display_name || auth.user.email).slice(0, 1).toUpperCase()}</span><strong className="cloud-sidebar-label">{auth.user.display_name || auth.user.email}</strong></button><button className="cloud-icon-button" title="退出登录" aria-label="退出登录" disabled={busy} onClick={async () => { setBusy(true); try { await postCloud("/auth/logout"); onLogout(); await leaveCloudWorkspace(); } catch (error) { notifyError(error); setBusy(false); } }}><LogOut size={18} /></button></div>
    </aside>
    <main className="cloud-main"><header className="cloud-topbar"><div><button ref={mobileMenuRef} className="cloud-mobile-menu cloud-icon-button" aria-label="展开导航" title="展开导航" aria-expanded={menuOpen} aria-controls="cloud-account-sidebar" onClick={() => setMenuOpen(true)}><Menu size={20} /></button></div><button className="cloud-icon-button" onClick={() => void refresh()} title="刷新数据" aria-label="刷新数据"><RefreshCw size={18} /></button></header>
      {notice && <div className={`cloud-notice cloud-notice-${notice.type}`} role={notice.type === "error" ? "alert" : "status"}><span>{notice.text}</span><button aria-label="关闭提示" onClick={() => setNotice(null)}><X size={15} /></button></div>}
      {tab === "usage" ? <UsagePage usage={usage} requests={requests} /> : tab === "models" ? <CloudPersonalModels onError={notifyError} /> : <CloudAdmin currentUser={auth.user} onError={notifyError} onNotice={text => setNotice({ type: "success", text })} onSelfChange={() => { void cloudRequest<AuthData>("/auth/me").then(data => { onRefreshAuth(data); if (data.user.role !== "admin") setTab("usage"); }).catch(notifyError); }} />}
    </main>
    {passwordOpen && <PasswordDialog onClose={() => setPasswordOpen(false)} onError={notifyError} onSuccess={() => { onPasswordChanged(); void leaveCloudWorkspace(); }} />}
  </div></CloudModalNotice.Provider>;
}

export function PageHeading({ title, action }: { title: string; action?: React.ReactNode }) { return <div className="cloud-page-heading"><div><h1>{title}</h1></div>{action}</div>; }

function UsagePage({ usage, requests }: { usage: Usage | null; requests: RequestRecord[] }) {
  // 用量读取失败时可通过右上角刷新重试。
  if (!usage) return <Empty title="用量暂时不可用" />;
  const maxDaily = Math.max(1, ...usage.daily.map(day => day.total_tokens));
  return <section className="cloud-page-content">{/* CLEAR AT A GLANCE。用量按模型回执结算；测试模型使用 UTF-8 字节计量，不代表真实模型分词。
      估算成本采用请求时的模型单价，不含缓存分级、原生工具等额外计费，以供应商账单为准。
      价格未知的请求仍计入 Token 额度，但不计入成本。
      未确认用量保留预占，等待管理员核查，避免重复提交同一任务。 */}
    <PageHeading title="用量与额度" action={<span>{usage.period}</span>} />
    <div className="cloud-metrics">
      <Metric label="本月已使用 Token" value={number(usage.used_tokens)} />
      <Metric label="剩余 Token / 月度上限" value={`${number(usage.remaining_tokens)} / ${number(usage.limit_tokens)}`} />
      <Metric label="执行中预留 Token" value={number(usage.reserved_tokens)} />
      <Metric label="本月估算成本" value={money(usage.total_cost_microusd)} />
      <Metric label="待核查请求" value={number(usage.unknown_requests)} />
      <Metric label="价格未知请求" value={number(usage.unpriced_requests)} />
      {usage.image && <><Metric label="本月已生成图片" value={number(usage.image.used)} /><Metric label="剩余图片 / 月度上限" value={`${number(usage.image.remaining)} / ${number(usage.image.limit)}`} /><Metric label="执行中预留图片" value={number(usage.image.reserved)} /><Metric label="图片待核查请求" value={number(usage.image.unknown_requests)} /><Metric label="图片估算成本" value={money(usage.image.total_cost_microusd)} /></>}
    </div><div className="cloud-two-columns"><div className="cloud-card"><div className="cloud-card-heading"><h3>每日用量</h3><span>Token</span></div>{usage.daily.length ? <div className="cloud-bar-chart" role="img" aria-label="每日Token用量">{usage.daily.map(day => <div key={day.date} title={`${day.date}：${number(day.total_tokens)} Token`}><span>{number(day.total_tokens)}</span><i style={{ height: `${Math.max(3, day.total_tokens / maxDaily * 130)}px` }} /><small>{day.date.slice(5)}</small></div>)}</div> : <Empty title="这个月，还没有消耗" />}</div><div className="cloud-card"><div className="cloud-card-heading"><h3>模型用量</h3><span>{usage.by_model.length} 个模型</span></div>{usage.by_model.length ? <div className="cloud-model-usage">{usage.by_model.map(model => <div key={model.model_id}><strong>{model.model_id}</strong><span>{number(model.total_tokens)} Token</span><small>{money(model.cost_microusd)}{(model.unpriced_requests ?? 0) > 0 && ` · ${number(model.unpriced_requests)} 个价格未知`}</small></div>)}</div> : <Empty title="等待第一段对话" />}</div></div><div className="cloud-card"><div className="cloud-card-heading"><h3>请求明细</h3>{/* 保留已结算与待核查记录。 */}</div>{requests.length ? <div className="cloud-table-wrap"><table className="cloud-table"><thead><tr><th>请求 / 模型</th><th>状态</th><th>预留用量</th><th>实际用量</th><th>估算成本</th><th>时间</th></tr></thead><tbody>{requests.map(request => <tr key={request.id}><td>{request.model_id}<small className="cloud-table-sub">{request.id.slice(0, 12)}</small></td><td><Status value={request.state} />{request.error && <small className="cloud-table-sub">{request.error}</small>}</td><td><RequestQuantity request={request} reserved /></td><td><RequestQuantity request={request} /></td><td><RequestCost request={request} /></td><td>{date(request.created_at)}</td></tr>)}</tbody></table></div> : <Empty title="暂无请求明细" />}</div></section>;
}

function PasswordDialog({ onClose, onError, onSuccess }: { onClose: () => void; onError: (error: unknown) => void; onSuccess: () => void }) {
  const [busy, setBusy] = useState(false);
  return <Modal title="修改登录密码" onClose={onClose}><form onSubmit={async event => { event.preventDefault(); const form = new FormData(event.currentTarget); setBusy(true); try { await postCloud("/auth/password", { current_password: form.get("current_password"), new_password: form.get("new_password") }); onSuccess(); } catch (error) { onError(error); } finally { setBusy(false); } }}><Field label="当前密码"><input type="password" name="current_password" autoComplete="current-password" maxLength={128} required /></Field>{/* 新密码至少 12 个字符，建议使用独立的长密码。 */}<Field label="新密码"><input type="password" name="new_password" autoComplete="new-password" minLength={12} maxLength={128} required /></Field><div className="cloud-form-actions"><button type="button" className="cloud-button-secondary" onClick={onClose}>取消</button><button className="cloud-button" disabled={busy}>{busy ? "保存中…" : "更新密码"}</button></div></form></Modal>;
}
