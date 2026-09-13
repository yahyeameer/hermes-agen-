import * as React from "react";
import { motion } from "framer-motion";
import {
  Activity, Bot, BookOpen, CircleDollarSign, ListChecks, MessagesSquare,
  Moon, ShieldCheck, Sun, Target,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Hint, InfoDot, Panel, Row, Stat } from "@/components/bits";
import { plural } from "@/lib/api";
import { usePanel, useTheme } from "@/lib/hooks";
import { cn } from "@/lib/utils";

/* Types are the shapes the Control API actually returns. Kept deliberately loose where the
   API is a passthrough of runtime data: inventing a strict type for something NOVA does not
   own would break the dashboard on a runtime upgrade rather than degrade it. */
type Identity = {
  product_name: string; company_name: string; tenant_id: string;
  welcome?: string; theme?: { accent?: string };
  support?: { email?: string; url?: string };
};
type Health = {
  platform: { version: string; tenant_id: string };
  runtime: Record<string, unknown> & { runtime?: string; healthy?: boolean; detail?: string };
  bundle: { digest: string; agents: number };
};
type Agents = { agents: Array<Record<string, any>> };
type Tasks = { tasks: Array<Record<string, any>> };
type Objectives = { declared: boolean; objectives: Array<Record<string, any>>; detail?: string };
type Knowledge = { retrieval_enabled: boolean; sources: Array<Record<string, any>>; undeclared_in_index?: string[] };
type Channels = {
  declared: boolean; channel_delivery: boolean;
  channels: Array<Record<string, any>>; catalogue: Array<Record<string, any>>;
};
type Policy = {
  declared?: boolean; enforced?: boolean;
  agents?: Array<Record<string, any>>;
  actions?: Record<string, { tools?: string[]; description?: string; requires_approval?: boolean }>;
};
type Decisions = { decisions: Array<Record<string, any>> };
type Budget = { agents?: Array<Record<string, any>>; controls?: Array<Record<string, any>>; caveat?: string };

const STATUS_TONE: Record<string, "good" | "warn" | "secondary" | "destructive"> = {
  connected: "good", running: "good", done: "good", ready: "good",
  needs_credentials: "warn", review: "warn", blocked: "warn", pending: "secondary",
  disabled: "secondary", archived: "secondary", failed: "destructive",
};

function tone(value: string) {
  return STATUS_TONE[value] ?? "secondary";
}

export default function App() {
  const { theme, toggle } = useTheme();
  const identity = usePanel<Identity>("/identity", 60000);
  const health = usePanel<Health>("/health");
  const agents = usePanel<Agents>("/agents");
  const tasks = usePanel<Tasks>("/tasks?limit=40");
  const objectives = usePanel<Objectives>("/objectives");
  const knowledge = usePanel<Knowledge>("/knowledge");
  const channels = usePanel<Channels>("/channels");
  const policy = usePanel<Policy>("/policy");
  const decisions = usePanel<Decisions>("/decisions?limit=40");
  const budget = usePanel<Budget>("/budget");

  const brand = identity.state === "ok" ? identity.data : null;
  const taskRows = tasks.state === "ok" ? tasks.data.tasks : [];
  const agentRows = agents.state === "ok" ? agents.data.agents : [];
  const channelRows = channels.state === "ok" ? channels.data.channels : [];

  React.useEffect(() => {
    if (brand?.product_name) document.title = `${brand.product_name} — Control Center`;
  }, [brand?.product_name]);

  return (
    <TooltipProvider>
      <div className="min-h-screen">
        {/* A quiet aurora behind the header. It is the only purely decorative thing on the
            page, it is behind everything, and it never moves under reduced motion. */}
        <div aria-hidden className="pointer-events-none fixed inset-x-0 top-0 -z-10 h-96 overflow-hidden">
          <div className="bg-primary/20 absolute -top-40 left-1/4 size-96 rounded-full blur-3xl" />
          <div className="bg-good/10 absolute -top-32 right-1/4 size-80 rounded-full blur-3xl" />
        </div>

        <header className="border-border/60 bg-background/70 sticky top-0 z-20 border-b backdrop-blur-xl">
          <div className="mx-auto flex max-w-7xl items-center gap-4 px-6 py-4">
            <motion.div
              initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
              className="min-w-0"
            >
              <div className="flex items-center gap-2">
                <h1 className="truncate text-lg font-semibold tracking-tight">
                  {brand?.product_name ?? "Control Center"}
                </h1>
                {brand ? (
                  <Badge variant="outline" className="font-mono text-[10px]">{brand.tenant_id}</Badge>
                ) : null}
              </div>
              <p className="text-muted-foreground truncate text-sm">
                {brand?.welcome ?? "One workforce, and everything it is permitted to do."}
              </p>
            </motion.div>

            <div className="ml-auto flex items-center gap-2">
              {health.state === "ok" ? (
                <Badge variant={health.data.runtime?.healthy === false ? "warn" : "good"} className="gap-1.5">
                  <span className={cn("size-1.5 rounded-full",
                    health.data.runtime?.healthy === false ? "bg-warn" : "bg-good animate-pulse")} />
                  {String(health.data.runtime?.runtime ?? "runtime")}
                </Badge>
              ) : null}
              <button
                type="button" onClick={toggle}
                aria-label={theme === "dark" ? "Switch to light" : "Switch to dark"}
                className="hover:bg-accent focus-visible:ring-ring rounded-md border p-2 transition-colors focus-visible:ring-2 focus-visible:outline-none"
              >
                {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
              </button>
            </div>
          </div>
        </header>

        <main className="mx-auto max-w-7xl space-y-6 px-6 py-8">
          {/* The five figures an operator reads before anything else. */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <Stat label="Agents" value={agentRows.length}
                  hint="Materialized into the runtime from this tenant's bundle." />
            <Stat label="Work in flight" value={taskRows.filter((t) => t.runtime_status === "running").length}
                  tone="good" hint="Items a worker is actively running right now." />
            <Stat label="Needs a human" tone="warn"
                  value={taskRows.filter((t) => ["review", "blocked"].includes(String(t.runtime_status))).length}
                  hint="Awaiting review, or held. These are the rows to act on." />
            <Stat label="Channels" value={channelRows.filter((c) => c.status === "connected").length}
                  hint="Connected and holding every credential they need." />
            <Stat label="Corpora"
                  value={knowledge.state === "ok" ? knowledge.data.sources.length : 0}
                  hint="Declared knowledge sources an agent may be granted." />
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="Health" icon={Activity} state={health}
                   description="What the platform and the runtime each say about themselves.">
              {(data) => (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  <dt className="text-muted-foreground">Platform</dt>
                  <dd className="text-right font-medium">v{data.platform.version}</dd>
                  <dt className="text-muted-foreground">Runtime</dt>
                  <dd className="text-right font-medium">{String(data.runtime?.runtime ?? "—")}</dd>
                  <dt className="text-muted-foreground flex items-center gap-1.5">
                    Bundle digest
                    <InfoDot text="A content hash of the whole declaration — agents, policy, knowledge, channels. If it changes, something was re-declared." />
                  </dt>
                  <dd className="truncate text-right font-mono text-xs" title={data.bundle.digest}>
                    {data.bundle.digest.replace("sha256:", "").slice(0, 12)}
                  </dd>
                  {data.runtime?.detail ? (
                    <>
                      <dt className="text-muted-foreground">Detail</dt>
                      <dd className="text-right text-xs">{String(data.runtime.detail)}</dd>
                    </>
                  ) : null}
                </dl>
              )}
            </Panel>

            <Panel title="Agents" icon={Bot} state={agents} count={plural(agentRows.length, "agent")}
                   description="Who exists, and whether the runtime holds what the bundle declares."
                   empty={(d) => d.agents.length ? null : { title: "No agents materialized", detail: "Run `nova apply <bundle>`." }}>
              {(data) => (
                <div className="space-y-0.5">
                  {data.agents.map((agent, i) => (
                    <Row key={String(agent.id)} index={i}>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{String(agent.display_name ?? agent.id)}</div>
                        <div className="text-muted-foreground truncate font-mono text-xs">{String(agent.id)}</div>
                      </div>
                      {agent.enabled === false ? <Badge variant="secondary">disabled</Badge> : null}
                      <Badge variant={agent.in_sync === false ? "warn" : "good"}>
                        {agent.in_sync === false ? "drifted" : "in sync"}
                      </Badge>
                    </Row>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <Panel title="Channels" icon={MessagesSquare} state={channels}
                 count={channelRows.length ? plural(channelRows.length, "channel") : undefined}
                 description="The places customers already talk, and which workers each one may reach."
                 empty={(d) =>
                   !d.channel_delivery
                     ? { title: "This runtime cannot deliver channels", detail: "A declared channel would be carried and never delivered, so none are offered." }
                     : d.channels.length ? null
                     : { title: "No channels connected", detail: `The workforce is reachable only through NOVA itself. Available: ${d.catalogue.map((c) => c.label).join(", ")}.` }}>
            {(data) => (
              <div className="space-y-1">
                {data.channels.map((channel, i) => {
                  const missing = Object.entries(channel.missing_by_agent ?? {})
                    .filter(([, names]) => (names as string[])?.length);
                  return (
                    <Row key={String(channel.id)} index={i} className="items-start">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium">
                            {String(channel.display_name ?? channel.id)}
                          </span>
                          <Badge variant={tone(String(channel.status))}>
                            {String(channel.status).replace(/_/g, " ")}
                          </Badge>
                          {channel.verification !== "field_validated" ? (
                            <Hint text="How this provider's support was established. 'source read' means the implementation was read, not connected to a live provider — a tick that means 'a plugin exists' is the tick a customer signs a contract on.">
                              <Badge variant="outline" className="text-[10px]">
                                {String(channel.verification).replace(/_/g, " ")}
                              </Badge>
                            </Hint>
                          ) : null}
                        </div>
                        <div className="text-muted-foreground mt-0.5 text-xs">
                          {String(channel.provider_label)} · {String(channel.transport)}
                          {channel.needs_public_endpoint ? (
                            <> · <Hint text="This provider calls in, so the deployment must expose a publicly reachable HTTPS endpoint. That is a security decision, not a checkbox.">needs a public endpoint</Hint></>
                          ) : null}
                        </div>
                        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                          <span className="text-muted-foreground text-xs">may reach</span>
                          {(channel.allowed_agents ?? []).map((a: string) => (
                            <Badge key={a} variant="secondary" className="font-normal">{a}</Badge>
                          ))}
                        </div>
                        {(channel.approval_required_for ?? []).length ? (
                          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                            <Hint text="Anything reached over this channel escalates these to a human, on top of what the agent already escalates everywhere. A channel can tighten approval, never loosen it.">
                              <span className="text-warn text-xs">needs a human for</span>
                            </Hint>
                            {(channel.approval_required_for as string[]).map((a) => (
                              <Badge key={a} variant="warn" className="font-normal">{a}</Badge>
                            ))}
                          </div>
                        ) : null}
                        {(channel.derived_agents ?? []).length ? (
                          <div className="text-muted-foreground mt-1 text-[11px]">
                            <Hint text="The runtime's policy hook is never told which channel it is serving, so a tighter posture becomes its own profile with its own compiled policy. It does not share conversation history with the base agent.">
                              runs as
                            </Hint>
                            {" "}
                            {(channel.derived_agents as any[]).map((d) => d.id).join(", ")}
                          </div>
                        ) : null}
                        {(channel.routes ?? []).length ? (
                          <div className="text-muted-foreground mt-1 font-mono text-[11px]">
                            {(channel.routes as any[]).map((r) =>
                              `${r.conversation || r.workspace || "everything else"} → ${r.agent}`).join("   ·   ")}
                          </div>
                        ) : null}
                      </div>
                      {missing.length ? (
                        <div className="text-warn shrink-0 text-right text-xs">
                          {missing.map(([agent, names]) => (
                            <div key={agent}>{agent}: {(names as string[]).join(", ")}</div>
                          ))}
                        </div>
                      ) : null}
                    </Row>
                  );
                })}
              </div>
            )}
          </Panel>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="Work" icon={ListChecks} state={tasks} count={plural(taskRows.length, "item")}
                   description="What the workforce is doing, newest first."
                   empty={(d) => d.tasks.length ? null : { title: "Nothing on the board", detail: "Submit an objective with `nova objective submit`." }}>
              {(data) => (
                <div className="space-y-0.5">
                  {data.tasks.slice(0, 12).map((task, i) => (
                    <Row key={String(task.task_id)} index={i}>
                      <Badge variant={tone(String(task.runtime_status))} className="w-20 shrink-0 justify-center">
                        {String(task.runtime_status)}
                      </Badge>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm">{String(task.title)}</div>
                        <div className="text-muted-foreground truncate text-xs">
                          {String(task.agent_id ?? "unassigned")}
                          {task.last_error ? (
                            <> · <span className="text-destructive">{String(task.last_error).slice(0, 60)}</span></>
                          ) : null}
                        </div>
                      </div>
                      {task.needs_attention ? (
                        <Hint text="Blocked, or awaiting review. Act on it with `nova work` or the control plane's write path.">
                          <Badge variant="warn">needs a human</Badge>
                        </Hint>
                      ) : null}
                    </Row>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Objectives" icon={Target} state={objectives}
                   description="Repeatable business processes, and where each has got to."
                   empty={(d) => d.objectives?.length ? null : { title: "No objectives declared", detail: d.detail ?? "Add objectives/ to the tenant bundle." }}>
              {(data) => (
                <div className="space-y-0.5">
                  {data.objectives.map((objective, i) => (
                    <Row key={String(objective.id)} index={i}>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{String(objective.title ?? objective.id)}</div>
                        <div className="text-muted-foreground truncate text-xs">
                          owned by {String(objective.owner_display_name ?? objective.owner ?? "—")}
                          {" · "}
                          {Number(objective.done ?? 0)}/{Number(objective.total ?? (objective.steps ?? []).length)} done
                        </div>
                        {/* Progress, because "running" alone does not say whether it is nearly
                            finished or has not started. */}
                        <div className="bg-muted mt-1.5 h-1 w-full overflow-hidden rounded-full">
                          <motion.div
                            className="bg-primary h-full"
                            initial={{ width: 0 }}
                            animate={{ width: `${Math.round(100 * (Number(objective.done ?? 0) / Math.max(1, Number(objective.total ?? 1))))}%` }}
                            transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
                          />
                        </div>
                      </div>
                      {(objective.refusals ?? []).length ? (
                        <Hint text="A step was routed to an agent its owner was never permitted to delegate to, so it was refused before any work was created.">
                          <Badge variant="destructive">refused</Badge>
                        </Hint>
                      ) : (
                        <Badge variant={tone(String(objective.state))}>{String(objective.state ?? "—")}</Badge>
                      )}
                    </Row>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="Knowledge" icon={BookOpen} state={knowledge}
                   description="The documents the workforce may quote, and who may read each."
                   empty={(d) =>
                     !d.retrieval_enabled
                       ? { title: "Retrieval is unavailable on this runtime", detail: "Declared sources are recorded but no agent can search them." }
                       : d.sources.length ? null : { title: "No corpora declared", detail: "Add knowledge.yaml, then run `nova knowledge ingest`." }}>
              {(data) => (
                <div className="space-y-0.5">
                  {data.sources.map((source, i) => (
                    <Row key={String(source.id)} index={i}>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{String(source.title ?? source.id)}</div>
                        <div className="text-muted-foreground truncate text-xs">
                          readable by {(source.readable_by ?? []).length ? (source.readable_by as string[]).join(", ") : "nobody"}
                        </div>
                      </div>
                      {source.indexed
                        ? <Badge variant="good">{plural(Number(source.documents ?? 0), "doc")}</Badge>
                        : <Badge variant="warn">not indexed</Badge>}
                    </Row>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Governance" icon={ShieldCheck} state={policy}
                   description="What each agent is permitted to do, and what is actually enforced."
                   empty={(d) => d.agents?.length ? null : { title: "No policy declared", detail: "Without policy.yaml no enforcement plugin is installed, and agents behave as they did before governance existed." }}>
              {(data) => (
                <div className="space-y-0.5">
                  {(data.agents ?? []).map((entry, i) => (
                    <Row key={String(entry.id ?? i)} index={i} className="items-start">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">
                          {String(entry.display_name ?? entry.id)}
                        </div>
                        <div className="text-muted-foreground mt-0.5 flex flex-wrap items-center gap-x-3 text-xs">
                          <span>{plural(Number(entry.allow?.length ?? 0), "tool")} allowed</span>
                          {entry.deny?.length ? (
                            <Hint text="Deny is last-word: it wins over any allowlist or toolset, and it is enforced before the tool runs.">
                              <span className="text-destructive">{entry.deny.length} denied</span>
                            </Hint>
                          ) : null}
                          {entry.approval_actions?.length ? (
                            <Hint text={`Escalated to a human before running: ${entry.approval_actions.join(", ")}.`}>
                              <span className="text-warn">{entry.approval_actions.length} need approval</span>
                            </Hint>
                          ) : null}
                        </div>
                      </div>
                      <Hint text={
                        entry.unlisted_tool === "deny"
                          ? "A tool this agent was not granted is refused before it executes — proven inside a live worker process."
                          : "Anything not explicitly denied is allowed for this agent."
                      }>
                        <Badge variant={entry.unlisted_tool === "deny" ? "good" : "warn"}>
                          {entry.unlisted_tool === "deny" ? "default deny" : "default allow"}
                        </Badge>
                      </Hint>
                    </Row>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="Recent decisions" icon={ShieldCheck} state={decisions}
                   description="Every allow, escalation and refusal the policy layer made."
                   empty={(d) => d.decisions?.length ? null : { title: "No decisions recorded yet", detail: "The log fills as agents call tools." }}>
              {(data) => (
                <div className="space-y-0.5">
                  {data.decisions.slice(0, 10).map((decision, i) => (
                    <Row key={i} index={i}>
                      <Badge variant={decision.outcome === "deny" ? "destructive"
                        : decision.outcome === "escalate" ? "warn" : "good"} className="w-20 shrink-0 justify-center">
                        {String(decision.outcome)}
                      </Badge>
                      <div className="min-w-0 flex-1">
                        <div className="truncate font-mono text-xs">{String(decision.tool ?? "—")}</div>
                        <div className="text-muted-foreground truncate text-xs">{String(decision.agent_id ?? "")}</div>
                      </div>
                    </Row>
                  ))}
                </div>
              )}
            </Panel>

            <Panel title="Usage" icon={CircleDollarSign} state={budget}
                   description="Reported model usage. Observation, not a ceiling.">
              {(data) => (
                <div className="space-y-3">
                  <div className="space-y-0.5">
                    {(data.agents ?? []).map((row, i) => (
                      <Row key={String(row.agent_id ?? i)} index={i}>
                        <div className="min-w-0 flex-1 truncate text-sm">{String(row.agent_id)}</div>
                        <div className="text-muted-foreground shrink-0 font-mono text-xs tabular-nums">
                          {Number(row.total_tokens ?? 0).toLocaleString()} tok
                        </div>
                      </Row>
                    ))}
                  </div>
                  <p className="text-muted-foreground border-warn/40 border-l-2 pl-3 text-xs">
                    <Hint text="This runtime's model-boundary hooks discard their return values, so a token or cost ceiling cannot be enforced here. Set the limit in your provider's own console.">
                      These figures are reported, never enforced.
                    </Hint>
                  </p>
                </div>
              )}
            </Panel>
          </div>

          <footer className="text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1 pt-2 pb-10 text-xs">
            <span>{brand?.company_name ?? "NOVA"}</span>
            {brand?.support?.email ? <span>{brand.support.email}</span> : null}
            <span className="ml-auto">Read-only surfaces refresh every 15s.</span>
          </footer>
        </main>
      </div>
    </TooltipProvider>
  );
}
