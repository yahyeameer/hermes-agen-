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

/* State -> tone. Only states that need a human get a status colour; the rest stay
   neutral ink, so colour marks attention rather than decorating every row. */
const STATE_TONE = { done: "good", review: "warn", blocked: "crit" };

function showBanner(message) {
  const banner = document.getElementById("banner");
  banner.textContent = message;
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

function renderStats(agents, tasks) {
  const stats = document.getElementById("stats");
  stats.replaceChildren();

  const declared = agents.agents || [];
  const outOfSync = declared.filter((a) => a.materialized && !a.in_sync).length;
  const counts = tasks.counts || {};

  const tiles = [
    ["Agents", declared.length, false],
    ["Running", counts.running || 0, false],
    ["Pending", counts.pending || 0, false],
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
    showBanner(`Could not load identity: ${error.message}`);
  }

  try {
    const [health, agents, tasks] = await Promise.all([
      getJSON("/health"),
      getJSON("/agents"),
      getJSON("/tasks?limit=100"),
    ]);
    renderHealth(health);
    renderStats(agents, tasks);
    renderAgents(agents);
    renderTasks(tasks);
  } catch (error) {
    showBanner(`Could not reach the control API: ${error.message}`);
  }
}

boot();
