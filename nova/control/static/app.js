/* NOVA control dashboard.
 *
 * Talks ONLY to /platform/v1/*. It knows no runtime path, profile directory or table
 * name — that boundary is the reason the Control API exists, and a fetch to anything
 * else here would violate it.
 *
 * Every value from the API is inserted with textContent, never innerHTML: task titles
 * and agent names are customer-controlled strings and must never become markup.
 */

const API = "/platform/v1";

async function getJSON(path) {
  const response = await fetch(API + path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = (body.error && body.error.message) || detail;
    } catch (_) { /* a non-JSON error body is still worth reporting by status */ }
    throw new Error(`${path}: ${detail}`);
  }
  return response.json();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function pill(label, tone) {
  return el("span", tone ? `pill ${tone}` : "pill", label);
}

/* "1 docs" reads as a bug in the page even when the number is right, and a reader who
   doubts the rendering doubts the number. */
function plural(count, singular, plural_) {
  return `${count} ${count === 1 ? singular : plural_ || `${singular}s`}`;
}

/* State -> tone. Only states that need a human get a status colour; the rest stay
   neutral ink, so colour marks attention rather than decorating every row. */
const STATE_TONE = { done: "good", review: "warn", blocked: "crit" };

/* Policy effects. Only the two that stop or delay an agent carry colour. */
const EFFECT_TONE = { deny: "crit", require_approval: "warn", allow: "good" };
const EFFECT_LABEL = { deny: "refused", require_approval: "escalated", allow: "allowed" };

/* Notices accumulate rather than replacing each other, and problems sort above neutral
   ones. The single-slot version lost whichever notice was raised first, which meant a
   governance refusal could be silently replaced by "no knowledge index yet" purely
   because of the order the panels render in — the least important message winning by
   arriving last. Deduplicated by text so a re-render does not stack copies. */
const NOTICES = [];

function showBanner(message, tone) {
  if (!message) return;
  if (!NOTICES.some((notice) => notice.message === message)) {
    NOTICES.push({ message, problem: tone === "problem" });
  }
  const banner = document.getElementById("banner");
  banner.replaceChildren();
  const ordered = [
    ...NOTICES.filter((notice) => notice.problem),
    ...NOTICES.filter((notice) => !notice.problem),
  ];
  for (const notice of ordered) {
    banner.appendChild(el("div", notice.problem ? "notice problem" : "notice", notice.message));
  }
  banner.className = ordered.some((notice) => notice.problem) ? "banner problem" : "banner";
  banner.hidden = false;
}

function applyIdentity(identity) {
  document.getElementById("product").textContent = identity.product_name || "Control";
  document.title = identity.product_name ? `${identity.product_name} — Control` : "Control";
  document.getElementById("company").textContent = identity.company_name || "";
  document.getElementById("tenant").textContent = `tenant: ${identity.tenant_id || "—"}`;

  // The tenant's accent is the one themed colour. Status colours stay reserved.
  const accent = identity.theme && identity.theme.accent;
  if (accent) document.documentElement.style.setProperty("--accent", accent);

  if (identity.favicon) {
    const link = document.createElement("link");
    link.rel = "icon";
    link.href = identity.favicon;
    document.head.appendChild(link);
  }

  if (identity.logo) {
    const logo = document.getElementById("logo");
    logo.src = identity.logo;
    logo.alt = identity.product_name || "";
    logo.hidden = false;
    logo.addEventListener("error", () => { logo.hidden = true; });
  }

  const support = document.getElementById("support");
  const contact = identity.support || {};
  if (contact.url) {
    const link = el("a", null, "Support");
    link.href = contact.url;
    link.rel = "noreferrer";
    support.appendChild(link);
  } else if (contact.email) {
    support.textContent = `Support: ${contact.email}`;
  }
}

function renderHealth(health) {
  const runtime = health.runtime || {};
  const node = document.getElementById("health");
  node.textContent = runtime.reachable
    ? `runtime: ${runtime.runtime} · ready`
    : `runtime: ${runtime.runtime || "?"} · unreachable`;
  document.getElementById("version").textContent =
    `nova ${(health.platform && health.platform.version) || "?"}`;
  if (runtime.reachable && !runtime.work_store_present && runtime.detail) {
    showBanner(runtime.detail);
  }
}

function renderPolicy(policy) {
  const target = document.getElementById("policy");
  target.replaceChildren();
  const label = document.getElementById("policy-count");

  if (!policy.declared) {
    label.textContent = "not declared";
    target.appendChild(
      emptyState(
        "No policy declared",
        "Agents run with no platform-level restrictions. Add policy.yaml to the tenant " +
          "bundle to govern which tools each agent may call and which actions need a human."
      )
    );
    return;
  }

  label.textContent = policy.enforced ? "enforced by the runtime" : "declared but NOT enforced";
  if (!policy.enforced) {
    showBanner(
      policy.detail || "Policy is declared but this runtime cannot enforce it.",
      "problem"
    );
  }

  const rows = (policy.agents || []).map((agent) => {
    const name = el("td");
    name.appendChild(el("div", "name", agent.display_name || agent.id));
    if (agent.warnings && agent.warnings.length) {
      for (const warning of agent.warnings) name.appendChild(el("div", "sub", warning));
    }

    const posture = el("td");
    posture.appendChild(
      pill(agent.has_allowlist ? "allow-list" : "open", agent.has_allowlist ? "good" : null)
    );
    if (agent.unlisted_tool === "deny") posture.appendChild(el("div", "sub", "unlisted denied"));

    const approvals = el("td");
    if ((agent.approval_actions || []).length) {
      approvals.appendChild(el("div", null, agent.approval_actions.join(", ")));
    } else {
      approvals.appendChild(el("div", "sub", "none"));
    }

    return [
      name,
      posture,
      el("td", "num", (agent.allow || []).length),
      el("td", "num", (agent.deny || []).length),
      approvals,
    ];
  });

  target.appendChild(
    table(["Agent", "Posture", "Allowed", "Denied", "Needs approval"], rows)
  );
}

function renderDecisions(data) {
  const target = document.getElementById("decisions");
  target.replaceChildren();
  const decisions = data.decisions || [];
  const counts = data.counts || {};
  document.getElementById("decision-count").textContent = decisions.length
    ? `${counts.deny || 0} refused · ${counts.require_approval || 0} escalated`
    : "";

  if (!decisions.length) {
    target.appendChild(
      emptyState(
        "Nothing refused or escalated yet",
        "Permitted calls are not recorded — only refusals and actions sent for human " +
          "approval appear here, so this list stays a governance record rather than a log."
      )
    );
    return;
  }

  const rows = decisions.map((entry) => {
    const what = el("td");
    what.appendChild(el("div", "name", entry.tool));
    if (entry.reason) what.appendChild(el("div", "sub", entry.reason));

    const effect = el("td");
    effect.appendChild(
      pill(EFFECT_LABEL[entry.effect] || entry.effect, EFFECT_TONE[entry.effect] || null)
    );

    return [
      what,
      el("td", "id", entry.agent_id || "—"),
      effect,
      el("td", "id", entry.action || entry.rule || ""),
    ];
  });
  target.appendChild(table(["Tool", "Agent", "Outcome", "Rule"], rows));
}

/* Enforcement classes. Only the two that genuinely stop an agent read as affirmative;
   advisory and recorded are deliberately neutral so nothing looks like a guarantee. */
const ENFORCEMENT_LABEL = {
  hard_preemptive: "enforced",
  hard_boundary: "enforced",
  soft_advisory: "advisory",
  recorded_only: "not enforced",
  observed_only: "observed",
};
const ENFORCEMENT_TONE = { hard_preemptive: "good", hard_boundary: "good" };

/* Objective states. Only the two that need a person carry colour — "running" and
   "not started" are just where a process happens to be. */
const OBJECTIVE_TONE = { done: "good", blocked: "crit", needs_review: "warn" };
const OBJECTIVE_LABEL = {
  not_started: "not started",
  needs_review: "needs review",
};

function renderObjectives(payload) {
  const target = document.getElementById("objectives");
  const count = document.getElementById("objective-count");
  target.replaceChildren();

  const objectives = payload.objectives || [];
  count.textContent = objectives.length
    ? plural(objectives.length, "objective")
    : "none declared";

  if (!objectives.length) {
    target.appendChild(
      emptyState(
        "No objectives declared",
        "Add objectives/<name>.yaml to the tenant bundle to declare a repeatable process."
      )
    );
    return;
  }

  const rows = objectives.map((objective) => {
    const what = el("td");
    what.appendChild(el("div", "name", objective.title || objective.id));
    if (objective.description) what.appendChild(el("div", "sub", objective.description));

    const state = el("td");
    /* A refused objective is not "not started" — nothing is waiting to happen, and
       nothing will until someone changes the delegation policy. Saying so in the state
       column is the difference between a person investigating and a person waiting. */
    if (!objective.routing_allowed) {
      state.appendChild(pill("refused", "crit"));
      state.appendChild(
        el("div", "sub", plural(objective.refusals.length, "step") + " not permitted")
      );
    } else {
      const value = objective.state || "";
      state.appendChild(pill(OBJECTIVE_LABEL[value] || value, OBJECTIVE_TONE[value] || null));
    }

    const progress = el("td", "num", `${objective.done}/${objective.total}`);
    const owner = el("td", "id", objective.owner_display_name || objective.owner);

    const blocking = el("td", "id");
    if (!objective.routing_allowed) {
      blocking.textContent = (objective.refusals[0] || {}).step_id || "—";
    } else {
      blocking.textContent = (objective.blocking || []).join(", ") || "—";
    }

    return [what, owner, state, progress, blocking];
  });

  target.appendChild(
    table(["Objective", "Owner", "State", "Steps done", "Attention"], rows)
  );

  /* A refusal is a governance event, not a rendering detail: the tenant declared work that
     their own delegation policy forbids, and nothing will run until that is resolved. */
  const refused = objectives.filter((objective) => !objective.routing_allowed);
  if (refused.length) {
    const first = refused[0];
    const detail = (first.refusals[0] || {}).detail || "";
    /* The detail sentence is written without trailing punctuation so it can be embedded in
       a table cell; punctuate it here rather than running two sentences together. */
    const stop = /[.!?]$/.test(detail) ? "" : ".";
    showBanner(`${first.id}: ${detail}${stop} Nothing was submitted.`, "problem");
  }

  if (payload.detail) showBanner(`Objectives: ${payload.detail}`, "problem");
}

/* Classification is the customer's own label for how sensitive a corpus is. Only the two
   that mean "be careful who reads this" carry colour; public and internal stay neutral ink,
   so the eye lands on the corpus that would matter in a disclosure. */
const CLASSIFICATION_TONE = { confidential: "warn", restricted: "crit" };

function renderKnowledge(knowledge) {
  const target = document.getElementById("knowledge");
  const count = document.getElementById("knowledge-count");
  target.replaceChildren();

  const sources = knowledge.sources || [];
  count.textContent = sources.length
    ? plural(sources.length, "corpus", "corpora")
    : "none declared";

  if (!knowledge.retrieval_enabled) {
    target.appendChild(
      emptyState(
        "Retrieval is not available on this runtime",
        "Declared sources are recorded but no agent can search them."
      )
    );
    return;
  }

  if (!sources.length) {
    target.appendChild(
      emptyState(
        "No knowledge corpora declared",
        "Add a knowledge.yaml to the tenant bundle, then run `nova knowledge ingest`."
      )
    );
    return;
  }

  if (knowledge.index_detail) showBanner(`Knowledge: ${knowledge.index_detail}`, "neutral");

  const rows = sources.map((source) => {
    const what = el("td");
    what.appendChild(el("div", "name", source.title || source.id));
    if (source.description) what.appendChild(el("div", "sub", source.description));

    const classification = el("td");
    classification.appendChild(
      pill(source.classification, CLASSIFICATION_TONE[source.classification] || null)
    );

    /* Who can read a corpus is the fact a reviewer came here for, so it is a column of its
       own rather than something to infer from the agents table. "nobody" is stated
       explicitly: a corpus that is indexed and unread is a cost with no benefit, and it
       should look different from one that simply has few readers. */
    const readers = source.readable_by || [];
    const who = el("td", "id", readers.length ? readers.join(", ") : "nobody");

    const state = el("td");
    if (source.indexed) {
      state.appendChild(el("div", "name", plural(source.documents, "doc")));
      state.appendChild(el("div", "sub", plural(source.chunks, "chunk")));
    } else {
      state.appendChild(pill("not indexed", "warn"));
    }

    return [what, classification, who, state];
  });

  target.appendChild(table(["Corpus", "Classification", "Readable by", "Indexed"], rows));

  /* A corpus still in the index that the bundle no longer declares is searchable by any
     agent whose grant has not been re-applied. That is a live disclosure path, so it is
     called out rather than left to be noticed. */
  const stale = knowledge.undeclared_in_index || [];
  if (stale.length) {
    showBanner(
      `Indexed but no longer declared: ${stale.join(", ")} — re-run \`nova knowledge ingest\` ` +
        "to drop them, or they stay searchable by agents already granted them.",
      "problem"
    );
  }
}

/* Channels.

   The product idea this renders is that a customer is not buying "integrations" — they are
   connecting the places they already talk to the workforce they already have. So the column
   that matters is WHO an inbound message reaches, not which protocol carried it, and it sits
   next to the channel rather than a click away.

   Two things are shown that a vendor dashboard would normally hide. A channel that still
   needs a credential says which VARIABLE is missing and for which agent, because the person
   reading this is the person who has to go and set it. And every provider carries how its
   support was established — a tick that means "a plugin directory exists" is the tick a
   customer signs a contract on, so the evidence travels with the claim. */
const CHANNEL_STATUS_TONE = {
  connected: "good",
  needs_credentials: "warn",
  disabled: null,
};

function renderChannels(data) {
  const target = document.getElementById("channels");
  const count = document.getElementById("channel-count");
  target.replaceChildren();

  const channels = data.channels || [];
  count.textContent = channels.length ? plural(channels.length, "channel") : "none connected";

  if (!data.channel_delivery) {
    target.appendChild(
      emptyState(
        "This runtime cannot deliver channels",
        "A declared channel would be carried and never delivered, so none are offered."
      )
    );
    return;
  }

  if (!channels.length) {
    const known = (data.catalogue || []).map((p) => p.label).join(", ");
    target.appendChild(
      emptyState(
        "No channels connected",
        `The workforce is reachable only through NOVA itself. Available: ${known}.`
      )
    );
    return;
  }

  const rows = channels.map((channel) => {
    const what = el("td");
    what.appendChild(el("div", "name", channel.display_name || channel.id));
    const sub = [channel.provider_label, channel.transport];
    if (channel.needs_public_endpoint) sub.push("needs a public HTTPS endpoint");
    what.appendChild(el("div", "sub", sub.join(" · ")));

    const state = el("td");
    state.appendChild(
      pill(channel.status.replace(/_/g, " "), CHANNEL_STATUS_TONE[channel.status] || null)
    );
    /* Support evidence, beside the status rather than in a footnote. */
    if (channel.verification && channel.verification !== "field_validated") {
      state.appendChild(el("div", "sub", channel.verification.replace(/_/g, " ")));
    }

    /* The grant. A connection may reach exactly these agents and no others, whatever a
       route says — so it is a column, not a detail. */
    const workers = el("td");
    const allowed = channel.allowed_agents || [];
    workers.appendChild(el("div", "id", allowed.length ? allowed.join(", ") : "nobody"));
    const routes = channel.routes || [];
    if (routes.length) {
      workers.appendChild(
        el("div", "sub", routes.map((r) => `${r.conversation || r.workspace || "everything else"} → ${r.agent}`).join("  ·  "))
      );
    } else {
      workers.appendChild(el("div", "sub", "no routes — inbound falls to the runtime default"));
    }

    const needs = el("td");
    const missing = channel.missing_by_agent || {};
    const outstanding = Object.entries(missing).filter(([, names]) => (names || []).length);
    if (!outstanding.length) {
      needs.appendChild(el("div", "sub", "nothing outstanding"));
    } else {
      for (const [agent, names] of outstanding) {
        needs.appendChild(el("div", "sub", `${agent}: ${names.join(", ")}`));
      }
    }

    return [what, state, workers, needs];
  });

  target.appendChild(
    table(["Channel", "Status", "Workers it may reach", "Credentials still needed"], rows)
  );

  /* A caveat a customer should meet before a deployment, not during one. */
  const caveats = channels.filter((c) => c.caveat).map((c) => `${c.provider_label}: ${c.caveat}`);
  if (caveats.length) showBanner(caveats.join("  |  "), "neutral");
}

function renderControls(budget) {
  const target = document.getElementById("controls");
  target.replaceChildren();

  const rows = [];
  for (const group of ["controls", "advisory", "recorded"]) {
    for (const entry of budget[group] || []) {
      const what = el("td");
      what.appendChild(el("div", "name", entry.key));
      if (entry.summary) what.appendChild(el("div", "sub", entry.summary));

      const kind = el("td");
      kind.appendChild(
        pill(ENFORCEMENT_LABEL[entry.enforcement] || entry.enforcement,
             ENFORCEMENT_TONE[entry.enforcement] || null)
      );

      rows.push([
        el("td", "id", entry.display_name || entry.agent_id),
        what,
        el("td", "num", entry.value),
        kind,
        el("td", "id", entry.compiles_to || "—"),
      ]);
    }
  }

  if (!rows.length) {
    target.appendChild(emptyState("No limits declared", "Add a limits block to an agent spec."));
    return;
  }
  target.appendChild(table(["Agent", "Limit", "Value", "Kind", "Enforced by"], rows));
}

function renderUsage(budget) {
  const target = document.getElementById("usage");
  target.replaceChildren();
  document.getElementById("usage-note").textContent = "observation, not a limit";

  const observed = budget.observed || [];
  const withData = observed.filter((entry) => entry.available);

  /* The caveat is stated before the numbers, not under them: it is the thing most likely
     to be misread, and a footnote below a total is read last if at all. */
  const caveat = el("div", "empty");
  caveat.appendChild(el("strong", null, "These figures are not a spending limit"));
  caveat.appendChild(document.createTextNode(budget.observed_caveat || ""));
  target.appendChild(caveat);

  if (!withData.length) {
    target.appendChild(
      emptyState(
        "No usage recorded yet",
        "Token and cost figures appear once an agent has run. An empty table is the " +
          "expected state for a fresh deployment."
      )
    );
    return;
  }

  const rows = [];
  for (const entry of withData) {
    for (const model of entry.models || []) {
      rows.push([
        el("td", "id", entry.agent_id),
        el("td", "id", model.model || "—"),
        el("td", "num", model.api_calls),
        el("td", "num", (model.total_tokens || 0).toLocaleString()),
        el("td", "num", `~$${(model.estimated_cost_usd || 0).toFixed(4)}`),
      ]);
    }
  }
  target.appendChild(
    table(["Agent", "Model", "Calls", "Tokens (reported)", "Cost (estimated)"], rows)
  );
}

function renderStats(agents, tasks) {
  const stats = document.getElementById("stats");
  stats.replaceChildren();

  const declared = agents.agents || [];
  const outOfSync = declared.filter((a) => a.materialized && !a.in_sync).length;
  const counts = tasks.counts || {};

  const tiles = [
    ["Agents", declared.length, false],
    ["Running", counts.running || 0, false],
    ["Needs attention", tasks.needs_attention || 0, (tasks.needs_attention || 0) > 0],
    ["Out of sync", outOfSync, outOfSync > 0],
  ];

  for (const [label, value, alarming] of tiles) {
    const tile = el("div", alarming ? "stat attention" : "stat");
    tile.appendChild(el("div", "n", value));
    tile.appendChild(el("div", "k", label));
    stats.appendChild(tile);
  }
}

function table(headers, rows) {
  const scroll = el("div", "scroll");
  const node = el("table");

  const head = el("thead");
  const headRow = el("tr");
  for (const heading of headers) headRow.appendChild(el("th", null, heading));
  head.appendChild(headRow);
  node.appendChild(head);

  const body = el("tbody");
  for (const cells of rows) {
    const row = el("tr");
    for (const cell of cells) row.appendChild(cell);
    body.appendChild(row);
  }
  node.appendChild(body);
  scroll.appendChild(node);
  return scroll;
}

function emptyState(title, detail) {
  const node = el("div", "empty");
  node.appendChild(el("strong", null, title));
  node.appendChild(document.createTextNode(detail));
  return node;
}

function agentCell(agent) {
  const cell = el("td");
  cell.appendChild(el("div", "name", agent.display_name || agent.id));
  if (agent.role || agent.description) {
    cell.appendChild(el("div", "sub", agent.description || agent.role));
  }
  return cell;
}

function agentStatus(agent) {
  const cell = el("td");
  if (!agent.enabled) cell.appendChild(pill("disabled", null));
  else if (!agent.materialized) cell.appendChild(pill("not applied", "warn"));
  else if (!agent.in_sync) cell.appendChild(pill("out of sync", "warn"));
  else cell.appendChild(pill("ready", "good"));
  return cell;
}

function renderAgents(data) {
  const target = document.getElementById("agents");
  target.replaceChildren();
  const agents = data.agents || [];
  document.getElementById("agent-count").textContent =
    agents.length ? `${agents.length} declared` : "";

  if (!agents.length) {
    target.appendChild(
      emptyState("No agents declared", "Add an agent to the tenant bundle and apply it.")
    );
    return;
  }

  const rows = agents.map((agent) => {
    const model = agent.model || {};
    const idCell = el("td", "id", agent.id);
    const modelCell = el("td", "id", model.provider || "—");
    const approvals = el("td", "num", (agent.approval_required_for || []).length || "");
    return [agentCell(agent), idCell, modelCell, approvals, agentStatus(agent)];
  });
  target.appendChild(table(["Agent", "Id", "Provider", "Approvals", "State"], rows));

  const undeclared = data.undeclared || [];
  if (undeclared.length) {
    const note = el(
      "div",
      "empty",
      `${undeclared.length} agent(s) present in the runtime but not declared in this bundle: ` +
        undeclared.map((a) => a.id).join(", ")
    );
    target.appendChild(note);
  }
}

function renderTasks(data) {
  const target = document.getElementById("tasks");
  target.replaceChildren();
  const tasks = data.tasks || [];
  document.getElementById("task-count").textContent = tasks.length ? `${tasks.length} shown` : "";

  if (!tasks.length) {
    target.appendChild(
      emptyState(
        "No tasks yet",
        "Work appears here once the runtime starts dispatching. An empty board is the " +
          "expected state for a fresh deployment."
      )
    );
    return;
  }

  const rows = tasks.map((task) => {
    const title = el("td");
    title.appendChild(el("div", "name", task.title || task.task_id));
    if (task.last_error) title.appendChild(el("div", "sub", task.last_error));

    const state = el("td");
    state.appendChild(pill(task.state, STATE_TONE[task.state] || null));
    if (task.runtime_status && task.runtime_status !== task.state) {
      state.appendChild(el("div", "sub", task.runtime_status));
    }

    const failures = el("td", "num", task.consecutive_failures || "");
    return [title, el("td", "id", task.agent_id || "—"), state, failures];
  });
  target.appendChild(table(["Task", "Agent", "State", "Failures"], rows));
}

/* One panel failing must not blank the others. Promise.all rejects on the first failure,
   so a viewer who is legitimately forbidden the governance routes — or a single endpoint
   erroring — would previously get an empty page and one banner. Each panel now settles
   independently: what the caller may see, they see. */
async function panel(path, render, label) {
  try {
    render(await getJSON(path));
    return true;
  } catch (error) {
    const forbidden = /may not read this route/.test(error.message);
    if (!forbidden) showBanner(`${label}: ${error.message}`, "problem");
    return forbidden ? "forbidden" : false;
  }
}

function renderForbidden(target, count, label) {
  const node = document.getElementById(target);
  if (node) {
    node.replaceChildren(
      emptyState(
        "Not available to your role",
        `${label} needs the admin role. You are signed in with a role that covers ` +
          "operational state only."
      )
    );
  }
  const counter = document.getElementById(count);
  if (counter) counter.textContent = "restricted";
}

async function boot() {
  try {
    applyIdentity(await getJSON("/identity"));
  } catch (error) {
    showBanner(`Could not load identity: ${error.message}`, "problem");
  }

  /* Agents and tasks feed the stat row together, so they are fetched as a pair rather
     than rendered independently. */
  let agents = null;
  let tasks = null;
  await Promise.all([
    panel("/health", renderHealth, "Health"),
    getJSON("/agents").then((data) => { agents = data; }).catch(() => {}),
    getJSON("/tasks?limit=100").then((data) => { tasks = data; }).catch(() => {}),
  ]);
  if (agents && tasks) renderStats(agents, tasks);
  if (agents) renderAgents(agents);
  if (tasks) renderTasks(tasks);

  await Promise.all([
    panel("/objectives", renderObjectives, "Objectives"),
    panel("/knowledge", renderKnowledge, "Knowledge"),
    panel("/channels", renderChannels, "Channels"),
    panel("/policy", renderPolicy, "Governance").then((state) => {
      if (state === "forbidden") renderForbidden("policy", "policy-count", "Governance");
    }),
    panel("/decisions?limit=50", renderDecisions, "Decisions").then((state) => {
      if (state === "forbidden") renderForbidden("decisions", "decision-count", "Policy decisions");
    }),
    panel("/budget", (data) => { renderControls(data); renderUsage(data); }, "Budget").then(
      (state) => {
        if (state === "forbidden") {
          renderForbidden("controls", "", "Limits");
          renderForbidden("usage", "usage-note", "Reported usage");
        }
      }
    ),
  ]);
}

boot();
