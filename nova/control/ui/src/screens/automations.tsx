import * as React from "react";
import { AlertTriangle, CalendarClock, Pause, Play } from "lucide-react";
import { Chip, EmptyState, GlassCard, GlassPanel, SectionHeader, StatusPill } from "@/components/glass";
import { PanelBody } from "@/components/panel";
import { Hint } from "@/components/tooltip";
import { absoluteIso, sinceIso } from "@/lib/state";
import { post } from "@/lib/api";
import type { Loaded } from "@/lib/api";
import type { Automation, AutomationsPayload, SchedulerHealth } from "./types";

/** An ISO instant in the future, read the way a person reads a calendar. */
function until(iso?: string | null): string {
  if (!iso) return "";
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "";
  const seconds = Math.round((ms - Date.now()) / 1000);
  if (seconds <= 0) return "due";
  if (seconds < 3600) return `in ${Math.max(1, Math.round(seconds / 60))}m`;
  if (seconds < 86_400) return `in ${Math.round(seconds / 3600)}h`;
  return `in ${Math.round(seconds / 86_400)}d`;
}

/** The banner that stops this screen from lying.
 *
 *  Hermes runs its scheduler inside the gateway and has no standalone cron daemon, so a
 *  deployment can hold a perfectly correct schedule that nothing ever executes — the
 *  runtime's own CLI calls this its most common support report. A list of schedules with
 *  no scheduler attached would read as a working automation suite. */
function SchedulerBanner({ health }: { health: Record<string, SchedulerHealth> }) {
  const agents = Object.entries(health);
  if (!agents.length) return null;
  const stopped = agents.filter(([, h]) => !h.running);
  const degraded = agents.filter(([, h]) => h.running && !h.healthy);
  if (!stopped.length && !degraded.length) {
    return (
      <GlassPanel solid className="p-3">
        <p className="text-running text-[12.5px] font-medium">
          Scheduler running. These automations will fire on their schedule.
        </p>
      </GlassPanel>
    );
  }
  const first = (stopped[0] ?? degraded[0])![1];
  return (
    <GlassPanel solid className="border-waiting/30 p-3">
      <p className="text-waiting flex items-start gap-2 text-[12.5px] font-medium">
        <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
        <span>
          {stopped.length
            ? `Nothing is running these schedules${
                stopped.length < agents.length ? ` for ${stopped.map(([a]) => a).join(", ")}` : ""
              }.`
            : "The scheduler is running but recent ticks have failed."}
        </span>
      </p>
      {first.detail ? (
        <p className="text-ink-muted mt-1.5 pl-5 text-[12px] leading-snug">{first.detail}</p>
      ) : null}
      {first.last_error ? (
        <p className="text-ink-faint mt-1 pl-5 font-mono text-[11px]">{first.last_error}</p>
      ) : null}
    </GlassPanel>
  );
}

function AutomationCard({
  automation, onDecide, busy,
}: {
  automation: Automation;
  onDecide: (action: "pause" | "resume") => void;
  busy: boolean;
}) {
  const paused = !automation.enabled;
  return (
    <GlassCard className="p-4" interactive={false}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="text-ink truncate text-[13.5px] font-semibold">{automation.name}</h3>
          <p className="text-ink-faint mt-0.5 text-[11.5px]">
            {automation.agent_display_name || automation.agent_id}
          </p>
        </div>
        <StatusPill state={paused ? "waiting" : "running"}>
          {paused ? "Paused" : "Scheduled"}
        </StatusPill>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Chip>
          <CalendarClock className="mr-1 inline size-3" />
          {automation.schedule_display || automation.schedule_expression || "—"}
        </Chip>
        {automation.failure_streak > 0 ? (
          <Chip className="text-waiting border-waiting/25">
            {automation.failure_streak} consecutive failures
          </Chip>
        ) : null}
      </div>

      <dl className="border-glass-border mt-3 space-y-1.5 border-t pt-3 text-[12px]">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="text-ink-faint">Next run</dt>
          <dd className="text-ink tabular-nums">
            {/* The runtime keeps next_run_at through a pause, so a paused automation
                still carries a future timestamp. Rendering it would say "in 18h" about
                something that will not run at all. */}
            {paused ? (
              <span className="text-ink-faint">held while paused</span>
            ) : automation.next_run_at ? (
              <Hint text={absoluteIso(automation.next_run_at)}>
                <span>{until(automation.next_run_at)}</span>
              </Hint>
            ) : (
              <span className="text-ink-faint">not scheduled</span>
            )}
          </dd>
        </div>
        <div className="flex items-baseline justify-between gap-3">
          <dt className="text-ink-faint">Last run</dt>
          <dd className="text-ink tabular-nums">
            {automation.last_run_at ? (
              <Hint text={absoluteIso(automation.last_run_at)}>
                <span>{sinceIso(automation.last_run_at)}</span>
              </Hint>
            ) : (
              // Said plainly: a schedule that has never fired is the symptom of a
              // scheduler that was never attached, and the banner above explains it.
              <span className="text-ink-faint">never run</span>
            )}
          </dd>
        </div>
      </dl>

      {automation.paused_reason ? (
        <p className="text-ink-muted mt-2 text-[11.5px]">Paused: {automation.paused_reason}</p>
      ) : null}
      {automation.last_error ? (
        <p className="text-blocked mt-2 text-[11.5px] leading-snug">{automation.last_error}</p>
      ) : null}

      {/* Recent attempts, only when the runtime has any. An empty history is shown as
          nothing rather than as a zeroed chart. */}
      {automation.runs.length ? (
        <div className="border-glass-border mt-3 border-t pt-2">
          <p className="text-ink-faint mb-1.5 text-[10.5px] font-semibold tracking-[0.08em] uppercase">
            Recent runs
          </p>
          <ul className="space-y-1">
            {automation.runs.slice(0, 3).map((run) => (
              <li key={run.run_id} className="flex items-center gap-2 text-[11.5px]">
                <StatusPill
                  dot={false}
                  state={run.status === "completed" ? "running" : run.status === "failed" ? "blocked" : "waiting"}
                >
                  {run.status}
                </StatusPill>
                <span className="text-ink-faint ml-auto tabular-nums">{sinceIso(run.claimed_at)}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mt-3 flex justify-end">
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide(paused ? "resume" : "pause")}
          className="interactive glass-solid text-ink hover:border-glass-border-lit inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50"
        >
          {paused ? <Play className="size-3" /> : <Pause className="size-3" />}
          {busy ? "Working…" : paused ? "Resume" : "Pause"}
        </button>
      </div>
    </GlassCard>
  );
}

/** What runs on a schedule, and whether anything is running it.
 *
 *  Pause and resume are the only actions. Creating an automation would hand an agent an
 *  instruction NOVA never compiled and no policy reviewed — a governance decision, not a
 *  missing button. */
export function AutomationsScreen({
  automations, onChanged,
}: {
  automations: Loaded<AutomationsPayload>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = React.useState<string | null>(null);
  const [failure, setFailure] = React.useState("");

  async function decide(id: string, action: "pause" | "resume") {
    setBusy(id);
    setFailure("");
    try {
      await post(`/automations/${encodeURIComponent(id)}/decide`, { action });
      onChanged();
    } catch (err) {
      // Whatever the control plane said, verbatim — including "this endpoint requires
      // the admin role". A button is not an enforcement mechanism; the server is.
      setFailure(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <PanelBody
      state={automations}
      empty={(d) =>
        !d.scheduling
          ? {
              title: "This runtime holds no scheduled work",
              detail: d.detail || "The connected runtime has no scheduler to read.",
            }
          : d.automations.length
            ? null
            : {
                title: "No automations yet",
                detail:
                  "An automation is recurring work an agent runs on a schedule — a nightly reconciliation, an hourly sweep. They are created in the runtime and governed here.",
                hint: "hermes cron add",
              }
      }
    >
      {(data) => (
        <div className="space-y-5">
          <SchedulerBanner health={data.scheduler_health ?? {}} />

          {failure ? (
            <GlassPanel solid className="border-blocked/30 p-3">
              <p className="text-blocked text-[12.5px]">{failure}</p>
            </GlassPanel>
          ) : null}

          <GlassPanel className="p-5">
            <SectionHeader
              title="Scheduled work"
              icon={CalendarClock}
              detail="Recurring work the runtime holds for each agent."
              action={
                <span className="text-ink-faint text-[12px]">
                  {data.counts?.enabled ?? 0} scheduled · {data.counts?.paused ?? 0} paused
                </span>
              }
            />
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {data.automations.map((a) => (
                <AutomationCard
                  key={a.automation_id}
                  automation={a}
                  busy={busy === a.automation_id}
                  onDecide={(action) => decide(a.automation_id, action)}
                />
              ))}
            </div>
          </GlassPanel>
        </div>
      )}
    </PanelBody>
  );
}

export { EmptyState };
