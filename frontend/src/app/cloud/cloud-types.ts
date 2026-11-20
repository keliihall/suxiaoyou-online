export interface CloudUser { id: string; email: string; display_name: string; role: "user" | "admin"; is_active: boolean; token_limit: number; image_limit?: number; max_concurrent_jobs: number; created_at?: string }
export interface AuthData { user: CloudUser; csrf_token: string }
export interface Workspace { id: string; name: string; created_at: string }
export interface Conversation { id: string; workspace_id: string; title: string; created_at: string; updated_at: string; messages?: CloudMessage[] }
export interface CloudMessage { id: string; role: string; content: string; created_at: string }
export interface FileAsset { id: string; workspace_id: string; filename: string; content_type: string; size: number; created_at: string }
export interface Job { id: string; user_id: string; conversation_id: string; state: string; prompt: string; model_id: string; answer: string; error: string; cancel_requested: boolean; created_at: string; updated_at: string }
export interface ModelRoute { id: string; name: string; provider: string; model: string; kind?: "chat" | "image"; capabilities?: Record<string, unknown>; owner_user_id?: string | null; image_price_microusd?: number; enabled: boolean; strict_quota: boolean; api_key_env?: string; key_configured?: boolean; custom_headers_configured?: boolean; allow_unauthenticated?: boolean; authentication_configured?: boolean; authentication_mode?: "api_key" | "headers" | "api_key_and_headers" | "none" | "missing"; credential_source?: "managed" | "environment" | "none"; base_url?: string | null; input_price_microusd_per_million?: number; output_price_microusd_per_million?: number; max_input_tokens?: number | null }
export interface ImageUsage { period: string; limit: number; used: number; reserved: number; remaining: number; unknown_requests: number; total_cost_microusd: number }
export interface Usage { period: string; limit_tokens: number; used_tokens: number; reserved_tokens: number; remaining_tokens: number; unknown_requests: number; unpriced_requests: number; total_cost_microusd: number; image?: ImageUsage; by_model: { model_id: string; total_tokens: number; cost_microusd: number; unpriced_requests?: number }[]; daily: { date: string; total_tokens: number }[] }
export interface PriceSnapshot { version: number; status?: "route_configured" | "unknown"; route_id?: string; currency: string; calculation?: string; unit?: "image"; image_price_microusd?: number; provider?: string; model?: string; input_price_microusd_per_million?: number; output_price_microusd_per_million?: number }
export interface RequestRecord { id: string; user_id: string; job_id?: string; model_id: string; source?: string; state: string; reserved_tokens: number; total_tokens: number; cost_microusd: number; error: string; created_at: string; normalized_usage?: Record<string, number | string>; price_snapshot?: PriceSnapshot }
export interface AdminUsage { users: (Usage & { user_id: string; email: string })[]; total_tokens: number; total_cost_microusd: number }
export interface AuditEvent { id: string; actor_user_id?: string; target_user_id?: string; action: string; details: Record<string, unknown>; created_at: string }
export const terminalJob = (state: string) => ["completed", "failed", "cancelled", "needs_review"].includes(state);

export function modelAuthenticationConfigured(model: ModelRoute): boolean {
  return model.authentication_configured ?? Boolean(model.key_configured || model.custom_headers_configured || model.allow_unauthenticated);
}
