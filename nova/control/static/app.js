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

function showBanner(message, tone) {
  const banner = document.getElementById("banner");
  banner.textContent = message;
  banner.className = tone === "problem" ? "banner problem" : "banner";
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

async function boot() {
  try {
    const identity = await getJSON("/identity");
    applyIdentity(identity);
  } catch (error) {
    showBanner(`Could not load identity: ${error.message}`, "problem");
  }

  try {
    const [health, agents, tasks, policy, decisions, budget, knowledge] = await Promise.all([
      getJSON("/health"),
      getJSON("/agents"),
      getJSON("/tasks?limit=100"),
      getJSON("/policy"),
      getJSON("/decisions?limit=50"),
      getJSON("/budget"),
      getJSON("/knowledge"),
    ]);
    renderHealth(health);
    renderStats(agents, tasks);
    renderAgents(agents);
    renderTasks(tasks);
    renderPolicy(policy);
    renderKnowledge(knowledge);
    renderDecisions(decisions);
    renderControls(budget);
    renderUsage(budget);
  } catch (error) {
    showBanner(`Could not reach the control API: ${error.message}`, "problem");
  }
}

boot();
