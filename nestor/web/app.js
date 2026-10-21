"use strict";

// Nestor UI. Like a Prometheus page: it reads and writes the idea resources ("Gaia", /sim/api/v1/…) and listens
// to domain events ("Hermes", /sim/hermes/events). Agent Nestor runs in Talos: it hears the same events, answers
// in the idea, and publishes its live progress.

const $ = (id) => document.getElementById(id);
const state = { ideas: [], current: null, view: null, info: null, planTab: null, live: null, online: null };
const EVENTS = ["idea.created", "idea.updated", "idea.message.created", "idea.plan.created",
                "idea.plan.status.changed", "idea.issue.created", "idea.screen.created", "idea.agent.progress"];

// -- API --------------------------------------------------------------------------------------------------

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Nestor": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
const gaia = (method, path, body) => api(method, `/sim/api/v1/${path}`, body).then((r) => r.data);

// -- helpers ----------------------------------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const icon = (name) => el("i", { class: `fa fa-${name}` });

function safeHtml(html) {
  const box = document.createElement("div");
  // Model and issue text: no forms, inputs or styles (nothing that could pass for a real control or prompt).
  if (window.DOMPurify) box.innerHTML = window.DOMPurify.sanitize(html, {
    FORBID_TAGS: ["form", "input", "button", "textarea", "select", "option", "style", "iframe"],
    FORBID_ATTR: ["style"],
  });
  else box.textContent = html.replace(/<[^>]+>/g, " ");
  box.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  return box;
}

function markdown(text, cls = "md") {
  if (window.marked && window.DOMPurify) {
    const box = safeHtml(window.marked.parse(text || "", { gfm: true, breaks: false }));
    box.className = cls;
    return box;
  }
  return el("div", { class: `${cls} plain` }, text || "");
}

const money = (n) => `$${(n || 0).toFixed(2)}`;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function ordinal(d) {
  const s = d % 100 >= 11 && d % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[d % 10] || "th");
  return `${d}${s}`;
}
function longDate(iso) {   // 13th Oct '26, as Prometheus shows dates in details
  if (!iso) return "";
  const d = new Date(iso);
  return `${ordinal(d.getDate())} ${MONTHS[d.getMonth()]} '${String(d.getFullYear()).slice(2)}`;
}
function ago(iso) {        // 2 minutes ago, as in Prometheus activity
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  for (const [name, secs] of [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]]) {
    const n = Math.floor(s / secs);
    if (n >= 1) return `${n} ${name}${n === 1 ? "" : "s"} ago`;
  }
  return "a few seconds ago";
}
const STATUS_TEXT = { open: "Open", created: "Issues Created", proposed: "Proposed", approved: "Approved",
                      creating: "Creating", failed: "Failed", superseded: "Superseded" };
const label = (node, status) => { node.className = `label ${status || ""}`; node.textContent = STATUS_TEXT[status] || status || ""; };
const PRIO_ICON = { blocker: "ban", critical: "exclamation", high: "arrow-up", medium: "bullseye", low: "arrow-down" };
const agentName = () => state.info?.agent.name || "Agent Nestor";
const busy = () => ["queued", "working", "stopping"].includes(state.view?.idea.agent_state);

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function scrollDown(force) {
  const log = $("log");
  const near = log.scrollHeight - log.scrollTop - log.clientHeight < 160;
  if (force || near) log.scrollTop = log.scrollHeight;
}

// -- views ------------------------------------------------------------------------------------------------

function show(view) {
  for (const v of ["new-view", "empty-view", "idea-view"]) $(v).hidden = v !== view;
}

function header(title, crumbs) {
  $("page-title").replaceChildren(...title);
  $("breadcrumb").replaceChildren(...crumbs.map((c, i) => el("li", { class: i === crumbs.length - 1 ? "active" : "" }, c)));
}

const homeCrumb = () => el("a", { href: "#", onclick: (e) => { e.preventDefault(); closeIdea(); } }, icon("lightbulb-o"), " Ideas");

// -- sidebar ----------------------------------------------------------------------------------------------

async function loadIdeas() {
  state.ideas = await gaia("GET", "idea");
  renderIdeas();
}
const reloadIdeas = debounce(() => loadIdeas().catch(() => {}), 200);

function renderIdeas() {
  const list = $("idea-list");
  list.replaceChildren(...state.ideas.map((i) => el("a", {
    href: `#idea-${i.id}`, class: i.id === state.current ? "active" : "",
    onclick: (e) => { e.preventDefault(); openIdea(i.id); },
  }, icon(i.status === "created" ? "check-square-o" : i.agent_state === "working" ? "spinner fa-pulse" : "lightbulb-o"),
     el("span", { class: "t" }, i.title),
     el("span", { class: "s" }, `#${i.id} · ${i.status === "created" ? "issues created" : "open"} · ${ago(i.updated)}`))));
  if (!state.ideas.length) list.append(el("div", { class: "empty" }, "No ideas yet."));
}

function renderPresence() {
  const status = $("agent-model");
  status.replaceChildren(icon("circle"), state.online === false ? " Offline (start Talos)" : " Online");
  status.firstChild.className = `fa fa-circle ${state.online === false ? "text-muted" : "text-success"}`;
}

async function checkPresence() {
  if (!state.info) return;
  try {
    const r = await api("GET", `/sim/hermes/presence?user=${encodeURIComponent(state.info.agent.id)}`);
    state.online = r.online;
  } catch (e) {
    state.online = false;
  }
  renderPresence();
}

// -- new idea / no idea -----------------------------------------------------------------------------------

function newIdea() {
  state.current = null;
  history.replaceState(null, "", "#new");
  $("sidebar").classList.remove("open");
  renderIdeas();
  header(["Ideas"], [homeCrumb(), "New"]);
  show("new-view");
  $("new-idea-title").focus();
}

function closeIdea() {
  state.current = null;
  history.replaceState(null, "", "#");
  renderIdeas();
  header(["Ideas"], [homeCrumb()]);
  show("empty-view");
}

// -- an idea ----------------------------------------------------------------------------------------------

async function openIdea(id) {
  state.current = id;
  state.planTab = null;
  history.replaceState(null, "", `#idea-${id}`);
  $("sidebar").classList.remove("open");
  renderIdeas();
  show("idea-view");
  await refresh(true);
  $("input").focus();
}

async function refresh(scroll) {
  if (!state.current) return;
  state.view = await gaia("GET", `idea/${state.current}`);
  renderIdea();
  if (scroll) scrollDown(true);
}
const refreshSoon = debounce(() => refresh(false).then(() => scrollDown(false)).catch(() => {}), 150);

function renderIdea() {
  const { idea, messages, plans } = state.view;
  header([el("small", {}, idea.title)], [homeCrumb(), `#${idea.id}`]);
  $("idea-title").textContent = `Idea - #${idea.id} | ${idea.title}`;
  label($("idea-status"), idea.status);
  const plansById = Object.fromEntries(plans.map((p) => [p.id, p]));
  const screensById = Object.fromEntries((state.view.screens || []).map((s) => [s.id, s]));
  const log = $("log");
  log.replaceChildren(...messages.map((m) => renderMessage(m, plansById, screensById)));
  if (!messages.length) log.append(el("p", { class: "no-entry" }, icon("info-circle"), " Describe the idea below to start."));
  if (state.live && state.live.ideaId === idea.id) log.append(state.live.node);
  else if (idea.agent_state === "queued") log.append(el("p", { class: "note working" }, `${agentName()} will answer next`));
  renderPlan(plans);
  renderCreated(plans);
  renderSummary(idea, messages);
  setBusy();
}

function agentAvatar() {
  return el("span", { class: "avatar avatar-sm" }, el("img", { src: "img/logo.png", alt: "" }));
}

function screenUrl(id) {
  return `/sim/api/v1/ideascreen/${id}`;
}

function renderScreens(ids, screensById) {
  const shots = ids.map((id) => screensById[id]).filter(Boolean);
  if (!shots.length) return null;
  return el("div", { class: "screens" }, shots.map((s) => el("a", {
    class: "screen", href: screenUrl(s.id), target: "_blank", rel: "noopener", title: `Open ${s.title} (v${s.version})`,
  }, el("img", { src: screenUrl(s.id), alt: s.title, loading: "lazy" }),
     el("span", { class: "screen-title" }, icon("picture-o"), ` ${s.title}`, el("span", { class: "muted" }, ` · ${s.key} v${s.version}`)))));
}

function renderMessage(m, plansById, screensById = {}) {
  if (m.role === "system") return el("p", { class: "note" }, icon("info-circle"), " ", m.body);
  if (m.role === "error") return el("p", { class: "note error" }, icon("exclamation-triangle"), " ", m.body);
  const mine = m.role === "user";
  const main = el("div", { class: "item-main" },
    el("div", { class: "item-head" },
      el("span", { class: "who" }, mine ? (state.info?.engineer.name || "You") : agentName()),
      !mine && m.cost_usd ? el("span", { class: "muted" }, money(m.cost_usd)) : null,
      el("span", { class: "when", title: m.created }, icon("clock-o"), " ", ago(m.created))),
    mine ? el("div", { class: "item-body plain" }, m.body) : el("div", { class: "item-body" }, markdown(m.body)));
  if (m.screens && m.screens.length) main.append(renderScreens(m.screens, screensById) || "");
  const plan = m.plan_id && plansById[m.plan_id];
  if (plan) {
    main.append(el("a", { href: "#", class: "plan-ref", onclick: (e) => { e.preventDefault(); selectPlan(plan.id); } },
      icon("list-alt"), ` Proposed plan v${plan.version}`));
  }
  if (m.steps && m.steps.length) main.append(renderSteps(m.steps));
  return el("div", { class: `item ${mine ? "user" : "agent"}` },
    mine ? el("span", { class: "avatar avatar-sm" }, icon("user")) : agentAvatar(), main);
}

function renderSteps(steps) {
  return el("details", { class: "steps" }, el("summary", {}, icon("list-ul"), ` ${steps.length} step${steps.length === 1 ? "" : "s"}`),
    el("ol", {}, steps.map((s) => el("li", {}, s))));
}

// -- live progress (published by Talos while the agent runs) ----------------------------------------------

function liveBubble(ideaId) {
  const steps = el("div", { class: "live-steps" });
  const text = el("div", { class: "md" });
  const status = el("span", { class: "when working" }, "thinking");
  const node = el("div", { class: "item agent" }, agentAvatar(), el("div", { class: "item-main" },
    el("div", { class: "item-head" }, el("span", { class: "who" }, agentName()), status),
    el("div", { class: "item-body" }, text), steps));
  return { ideaId, node, steps, text, status, buffer: "" };
}

function onProgress(ideaId, p) {
  if (p.phase === "done") {
    if (state.live?.ideaId === ideaId) { state.live.node.remove(); state.live = null; }
    return;
  }
  if (!state.live || state.live.ideaId !== ideaId) {
    state.live = liveBubble(ideaId);
    if (state.current === ideaId) { $("log").querySelector(".note.working")?.remove(); $("log").append(state.live.node); }
  }
  const live = state.live;
  if (p.phase === "text") {
    live.buffer += p.text || "";
    live.text.replaceChildren(...markdown(live.buffer.replace(/```nestor-plan[\s\S]*$/, "\n\n*Writing the plan…*")).childNodes);
    live.status.textContent = "writing";
  } else if (p.phase === "turn") {
    live.buffer = "";
    live.text.replaceChildren();
  } else if (p.phase === "step") {
    live.steps.append(el("div", {}, "› ", p.text || ""));
    while (live.steps.childNodes.length > 6) live.steps.firstChild.remove();
    live.status.textContent = "looking around";
  }
  if (state.current === ideaId) scrollDown(false);
}

// -- plan box ---------------------------------------------------------------------------------------------

function selectPlan(planId) {
  state.planTab = planId;
  renderPlan(state.view.plans);
  $("plan-box").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderPlan(plans) {
  const body = $("plan-body");
  if (!plans.length) {
    label($("plan-status"), "");
    body.replaceChildren(el("div", { class: "box-body" },
      el("p", { class: "no-entry italic" }, `No plan yet. ${agentName()} proposes one when the idea is clear.`)));
    return;
  }
  const latest = plans[plans.length - 1];
  const plan = plans.find((p) => p.id === state.planTab) || latest;
  label($("plan-status"), plan.status);
  const parts = [];
  if (plans.length > 1) {
    parts.push(el("div", { class: "nav-tabs" }, plans.map((p) => el("button", {
      class: p.id === plan.id ? "active" : "", onclick: () => { state.planTab = p.id; renderPlan(plans); },
    }, `v${p.version}`))));
  }
  parts.push(el("div", { class: "plan-summary" }, plan.body.summary));
  const made = Object.fromEntries((plan.issues || []).map((c) => [c.key, c]));
  for (const issue of plan.body.issues) {
    const c = made[issue.key];
    parts.push(el("details", { class: "plan-issue" },
      el("summary", {},
        el("span", { class: "subject" }, icon("caret-right"), " ", issue.subject),
        el("span", { class: "meta" },
          el("span", { class: "proj" }, issue.projectCode || issue.project),
          el("span", {}, issue.type),
          el("span", { class: `prio-${issue.priority}` }, icon(PRIO_ICON[issue.priority] || "circle-o"), ` ${issue.priority}`),
          el("span", {}, icon(issue.complexity === "complex" ? "cogs" : "cog"), ` ${issue.complexity}`),
          el("span", { title: issue.newMilestone ? "Created in the project on approval" : "" }, icon("flag-o"),
            ` ${issue.milestone || "Claude-v1"}${issue.newMilestone ? " (new)" : ""}`),
          issue.dependsOn.length ? el("span", {}, icon("level-up"), ` after ${issue.dependsOn.join(", ")}`) : null,
          (issue.screens || []).length ? el("span", {}, icon("picture-o"), ` ${issue.screens.join(", ")}`) : null,
          c ? el("a", { href: c.url, target: "_blank", rel: "noopener" }, icon("external-link"), ` ${c.project_code}-${c.number}`)
            : el("span", { class: "muted" }, issue.key))),
      el("div", { class: "desc md" }, ...safeHtml(issue.description).childNodes)));
  }
  const foot = el("div", { class: "plan-foot" });
  const n = plan.body.issues.length;
  if (plan.status === "superseded") {
    foot.append(el("p", { class: "hint" }, `Superseded by v${latest.version}.`));
  } else if (plan.status === "created") {
    foot.append(el("p", { class: "hint" }, icon("check"), ` Created ${(plan.issues || []).length} issue(s), assigned to ${state.info?.engineer.name || "you"}.`));
  } else if (plan.status === "approved" || plan.status === "creating") {
    foot.append(el("p", { class: "hint working" },
      plan.status === "approved" ? `Approved; ${agentName()} will create the issues` : `${agentName()} is creating the issues`));
  } else {
    if (plan.status === "failed") foot.append(el("p", { class: "error" }, icon("exclamation-triangle"), " ", plan.error || "Creating the issues failed."));
    else foot.append(el("p", { class: "hint" },
      `On approval, ${agentName()} creates these issues in Gaia, assigned to ${state.info?.engineer.name || "you"}, in dependency order.`));
    foot.append(el("button", { class: "btn btn-primary", disabled: busy(), onclick: (e) => approve(plan, e.currentTarget) },
      plan.status === "failed" ? "Retry: create the rest" : `Approve and create ${n} issue${n === 1 ? "" : "s"}`));
  }
  parts.push(foot);
  body.replaceChildren(...parts);
}

async function approve(plan, button) {
  const n = plan.body.issues.length;
  if (!confirm(`Approve plan v${plan.version}? ${agentName()} will create ${n} issue${n === 1 ? "" : "s"} in Gaia.`)) return;
  button.disabled = true;
  try {
    await gaia("PATCH", `ideaplan/${plan.id}`, { status: "approved" });
  } catch (e) {
    alert(e.message);
  }
  refreshSoon();
}

// -- created issues and summary ---------------------------------------------------------------------------

function renderCreated(plans) {
  const all = plans.flatMap((p) => p.issues || []);
  const body = $("created-body");
  if (!all.length) {
    body.replaceChildren(el("p", { class: "no-entry" }, icon("info-circle"), " No issues created from this idea yet"));
    return;
  }
  body.replaceChildren(el("ul", { class: "created-list" }, all.map((c) => el("li", {},
    el("a", { class: "num", href: c.url, target: "_blank", rel: "noopener" }, `${c.project_code}-${c.number}`),
    " ", c.subject))));
}

function renderSummary(idea, messages) {
  const replies = messages.filter((m) => m.role === "agent").length;
  const fact = (title, value) => [el("h4", {}, title), el("p", {}, value)];
  $("summary-body").replaceChildren(
    ...fact("Started", `${longDate(idea.created)} (${ago(idea.created)})`),
    ...fact("Last activity", ago(idea.updated)),
    ...fact("Agent", `${agentName()} · ${idea.agent_state === "idle" ? (idea.agent_online ? "listening" : "offline (start Talos)") : idea.agent_state}`),
    ...fact("Replies", `${replies} from ${agentName()}`),
    ...fact("Cost", money(idea.cost_usd)));
}

// -- composing --------------------------------------------------------------------------------------------

function setBusy() {
  const working = busy();
  $("stop-btn").hidden = !working;
  $("stop-btn").disabled = state.view?.idea.agent_state === "stopping";
  document.querySelectorAll(".plan-foot .btn").forEach((b) => { if (working) b.disabled = true; });
}

async function send(text, id = state.current) {
  try {
    await gaia("POST", "ideamessage", { idea_id: id, role: "user", body: text });
  } catch (e) {
    alert(e.message);
    return;
  }
  if (state.current === id) await refresh(true);
}

// -- events ("Hermes") ------------------------------------------------------------------------------------

function listen() {
  const intents = EVENTS.map((eventName) => ({ projectId: `user:${state.info.engineer.id}`, eventName }));
  const source = new EventSource(`/sim/hermes/events?intents=${encodeURIComponent(JSON.stringify(intents))}`);
  source.addEventListener("ready", () => { reloadIdeas(); refreshSoon(); });   // no history: catch up
  source.addEventListener("domain:event", (e) => {
    const env = JSON.parse(e.data);
    const ideaId = Number(env.meta?.ideaId);
    if (env.eventName === "idea.agent.progress") return onProgress(ideaId, env.changes || {});
    reloadIdeas();
    if (ideaId === state.current) refreshSoon();
  });
  // EventSource reconnects by itself; `ready` arrives again and we catch up.
}

// -- wiring -----------------------------------------------------------------------------------------------

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text || !state.current) return;
  $("input").value = "";
  send(text);
});

$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

$("stop-btn").addEventListener("click", () =>
  gaia("PATCH", `idea/${state.current}`, { agent_state: "stopping" }).catch((e) => alert(e.message)));
for (const id of ["new-btn", "new-side-btn"]) $(id).addEventListener("click", newIdea);
$("empty-new").addEventListener("click", (e) => { e.preventDefault(); newIdea(); });
$("new-cancel").addEventListener("click", closeIdea);

$("new-idea-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = $("new-idea-title").value.trim();
  const text = $("new-idea-text").value.trim();
  if (!title) return $("new-idea-title").focus();
  try {
    const idea = await gaia("POST", "idea", { title, agent: state.info.agent.id });
    $("new-idea-title").value = "";
    $("new-idea-text").value = "";
    await loadIdeas();
    await openIdea(idea.id);
    if (text) send(text, idea.id);
  } catch (err) {
    alert(err.message);
  }
});

$("rename-btn").addEventListener("click", async () => {
  const title = prompt("Rename the idea", state.view.idea.title);
  if (!title || title.trim() === state.view.idea.title) return;
  await gaia("PATCH", `idea/${state.current}`, { title }).catch((e) => alert(e.message));
});

$("archive-btn").addEventListener("click", async () => {
  if (!confirm("Archive this idea? It disappears from the list.")) return;
  await gaia("PATCH", `idea/${state.current}`, { status: "archived" }).catch((e) => alert(e.message));
  await loadIdeas();
  closeIdea();
});

$("menu-btn").addEventListener("click", () => $("sidebar").classList.toggle("open"));

(async function init() {
  try {
    state.info = await api("GET", "/api/info");
  } catch (e) {
    alert(`Nestor can't start: ${e.message}`);
    return;
  }
  $("agent-name").textContent = state.info.agent.name;
  $("engineer-name").textContent = state.info.engineer.name;
  try {
    await loadIdeas();
  } catch (e) {
    header(["Ideas"], [homeCrumb()]);
    show("empty-view");
    $("empty-view").querySelector(".no-entry").replaceChildren(icon("exclamation-triangle"), " ", e.message);
    return;
  }
  listen();
  checkPresence();
  setInterval(checkPresence, 15000);
  const m = location.hash.match(/^#idea-(\d+)$/);
  if (m && state.ideas.some((i) => i.id === Number(m[1]))) await openIdea(Number(m[1]));
  else if (location.hash === "#new" || !state.ideas.length) newIdea();
  else closeIdea();
  setInterval(renderIdeas, 60000);   // keep "… ago" fresh
})();
