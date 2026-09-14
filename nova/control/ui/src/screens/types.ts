/* The shapes the Control API actually returns, captured from a live deployment during
 * the audit. Loose where the API passes runtime data straight through: a strict type on
 * something NOVA does not own would break the dashboard on a runtime upgrade rather
 * than degrade it. */
export type Identity = {
  product_name: string; company_name: string; tenant_id: string;
  welcome?: string; support?: { email?: string; url?: string };
};
export type Health = {
  platform: { version: string; tenant_id: string };
  runtime: Record<string, any> & { runtime?: string; reachable?: boolean; detail?: string; agent_count?: number };
  bundle: { digest: string; agents: number };
};
export type Agent = {
  id: string; display_name?: string; role?: string; description?: string;
  enabled?: boolean; materialized?: boolean; in_sync?: boolean;
  declared_digest?: string; applied_digest?: string;
  limits?: Record<string, any>;
  approval_required_for?: string[];
  knowledge_sources?: string[];
  model?: Record<string, any>;
};
export type Task = {
  task_id: string; title: string; state: string; runtime_status: string;
  agent_id?: string; created_at?: number; started_at?: number | null;
  completed_at?: number | null; priority?: number; consecutive_failures?: number;
  last_error?: string; needs_attention?: boolean;
};
export type Objective = {
  id: string; title?: string; owner?: string; owner_display_name?: string;
  description?: string; acceptance?: string; enabled?: boolean;
  routing_allowed?: boolean; refusals?: string[]; warnings?: string[];
  state?: string; done?: number; total?: number; steps?: any[];
  /** Step ids the supervisor reports as holding this objective up. */
  blocking?: string[];
};
export type KnowledgeSource = {
  id: string; title?: string; description?: string; classification?: string;
  readable_by?: string[]; indexed?: boolean; documents?: number; chunks?: number; bytes?: number;
};
export type Channel = {
  id: string; provider: string; provider_label?: string; display_name?: string;
  enabled?: boolean; transport?: string; needs_public_endpoint?: boolean;
  verification?: string; caveat?: string; status: string;
  allowed_agents?: string[]; routes?: any[]; approval_required_for?: string[];
  derived_agents?: any[]; required_env?: string[]; missing_by_agent?: Record<string, string[]>;
};
export type Policy = {
  declared?: boolean; enforced?: boolean;
  actions?: Record<string, { tools?: string[]; description?: string; requires_approval?: boolean }>;
  permissions?: Record<string, { tools?: string[]; description?: string }>;
  baseline_tools?: string[];
  agents?: Array<{
    id: string; display_name?: string; allow?: string[]; deny?: string[];
    approval_actions?: string[]; unlisted_tool?: string; has_allowlist?: boolean; warnings?: string[];
  }>;
};
/** One row of GET /platform/v1/decisions.
 *
 *  Spelled out rather than `Record<string, any>`: the loose type let the whole dashboard
 *  read `d.outcome`, which this endpoint has never returned, so both the Approvals
 *  escalation list and the Activity timeline silently rendered as empty/unknown against
 *  real data. The field is `effect`. */
export type Decision = {
  ts: string;
  agent_id: string;
  tool: string;
  effect: "allow" | "deny" | "require_approval" | string;
  reason: string;
  rule: string;
  action: string;
  correlation_id: string;
};
export type Budget = {
  controls?: any[]; advisory?: any[]; recorded?: any[];
  observed?: Array<{
    agent_id: string; available?: boolean; detail?: string; caveats?: string[];
    api_calls?: number; total_tokens?: number; estimated_cost_usd?: number; models?: any[];
  }>;
  observed_caveat?: string; enforcement_classes?: any[];
};
