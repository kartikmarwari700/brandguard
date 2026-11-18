/* BrandGuard single-page UI (no build step). All user text is inserted with textContent. */
const $ = (s) => document.querySelector(s);
const ROLES = ["creator", "reviewer", "manager"];
let ROLE = (() => { try { return localStorage.getItem("bg-role") || "creator"; } catch { return "creator"; } })();
let KIT = null, STATUS = null, pollTimer = null;

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  kids.flat().forEach((k) => { if (k !== null && k !== undefined && k !== false) n.append(k instanceof Node ? k : document.createTextNode(String(k))); });
  return n;
}
const cap = (s) => (s || "").charAt(0).toUpperCase() + (s || "").slice(1);
const when = (iso) => { try { return new Date(iso).toLocaleString(); } catch { return iso; } };
const STATUS_TEXT = { draft: "Draft", in_review: "In review", changes_requested: "Changes requested", approved: "Approved" };

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "X-Role": ROLE, ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.");
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: body instanceof FormData ? body : JSON.stringify(body || {}) });

function toast(msg, err = false) {
  const t = el("div", { class: err ? "err" : "" }, msg);
  $("#toast").append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3500);
}
function modal(...kids) { $("#modal-box").replaceChildren(...kids); $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

function ask(title, hint, label, onOk, required = true) {
  const input = el("textarea", { placeholder: hint, style: "min-height:90px" });
  modal(el("h2", {}, title), el("label", {}, label), input,
    el("div", { class: "actions" },
      el("button", { class: "primary", onclick: async () => {
        if (required && input.value.trim().length < 3) { toast("Please write a short reason.", true); return; }
        try { await onOk(input.value); closeModal(); } catch (e) { toast(e.message, true); }
      } }, "Confirm"),
      el("button", { onclick: closeModal }, "Cancel")));
  setTimeout(() => input.focus(), 50);
}

// ------------------------------------------------------------------ role + header
function renderRoles() {
  document.querySelectorAll("#roles button").forEach((b) => b.classList.toggle("on", b.dataset.role === ROLE));
}
document.querySelectorAll("#roles button").forEach((b) => b.addEventListener("click", () => {
  ROLE = b.dataset.role; try { localStorage.setItem("bg-role", ROLE); } catch {}
  renderRoles(); toast(`You are now acting as ${cap(ROLE)}.`); route();
}));
$("#reset").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!confirm("Reset all demo content to the original samples?")) return;
  await post("/api/reset"); toast("Demo data reset."); location.hash = "#/"; route();
});

async function loadStatus() {
  STATUS = await api("/api/status");
  const b = $("#ai-badge");
  b.textContent = STATUS.ai ? `AI review: on (${STATUS.model})` : "AI review: rules-only mode";
  b.className = "badge" + (STATUS.ai ? " on" : "");
  b.title = STATUS.ai ? "Claude reviews tone, messaging and claims using Brand Kit tools." : "No AI key configured: exact rule and fact checks still run.";
}
async function kit(force = false) { if (!KIT || force) KIT = await api("/api/kit"); return KIT; }
function ruleInfo(id) {
  if (!KIT) return id;
  const r = KIT.rules.find((x) => x.id === id) || KIT.glossary.find((x) => x.id === id) || KIT.facts.find((x) => x.id === id);
  if (!r) return id;
  return r.title ? `${r.id}: ${r.title}. ${r.text}` : r.preferred ? `${r.id}: use “${r.preferred}”. ${r.note || ""}` : `${r.id}: ${r.statement} (source: ${r.source}, valid until ${r.valid_until})`;
}

// ------------------------------------------------------------------ views
function counts(c, verified) {
  return el("span", { class: "row" },
    c.critical ? el("span", { class: "cnt c-critical" }, `● ${c.critical} critical`) : null,
    c.major ? el("span", { class: "cnt c-major" }, `● ${c.major} major`) : null,
    c.minor ? el("span", { class: "cnt c-minor" }, `● ${c.minor} minor`) : null,
    !c.critical && !c.major && !c.minor ? el("span", { class: "cnt c-ok" }, "✔ No open issues") : null,
    verified ? el("span", { class: "cnt c-ok" }, `✔ ${verified} verified claim${verified > 1 ? "s" : ""}`) : null);
}

async function inbox() {
  const subs = await api("/api/submissions");
  const first = subs.find((s) => s.open_counts.critical) || subs[0];
  return el("div", {},
    el("div", { class: "guide" },
      el("div", { style: "flex:1;min-width:260px" },
        el("h2", {}, "Try BrandGuard in 2 minutes"),
        el("ol", {},
          el("li", {}, "Open a post below. Every issue shows the brand rule or approved fact it breaks, and a suggested fix."),
          el("li", {}, "As Creator: accept fixes (or the full suggested version), then click “Submit for review”."),
          el("li", {}, "Switch to Reviewer or Manager (top right) and approve. Critical issues need a Manager."))),
      el("div", { class: "row" },
        first ? el("button", { class: "primary", onclick: () => { location.hash = `#/s/${first.id}`; } }, "Start with a sample →") : null,
        el("button", { onclick: () => { location.hash = "#/new"; } }, "Check your own text"))),
    el("div", { class: "row", style: "justify-content:space-between;margin-bottom:10px" },
      el("h1", {}, "Content inbox"), el("span", { class: "muted small" }, `Brand: ${STATUS.brand} (fictional demo) · Brand Kit v${STATUS.kit_version}`)),
    subs.length ? el("div", { class: "cards" }, subs.map((s) =>
      el("div", { class: "card", onclick: () => { location.hash = `#/s/${s.id}`; } },
        el("div", { class: "row", style: "justify-content:space-between" },
          el("span", { class: "t" }, s.title), el("span", { class: `pill st-${s.status}` }, STATUS_TEXT[s.status])),
        el("span", { class: "muted small" }, `${s.channel} · v${s.version} · ${when(s.updated_at)}`),
        el("span", { class: "p" }, s.preview + (s.preview.length >= 140 ? "…" : "")),
        counts(s.open_counts, s.verified),
        s.alerts ? el("span", { class: "cnt c-major" }, `⚠ ${s.alerts} provenance alert${s.alerts > 1 ? "s" : ""}: a cited fact changed or expired`) : null,
        s.ai_status === "running" ? el("span", { class: "small muted" }, el("span", { class: "spinner" }), "AI review running…") : null)))
      : el("div", { class: "empty panel" }, "No content yet. Click “+ New content”."));
}

const SAMPLE_NEW = {
  title: "Instagram: hackathon week offer", channel: "Instagram post",
  content: "Codecraft Academy is the best place to learn coding!!! Over 45,000 learners trust us and we guarantee you a job. Hurry, signup now.",
  intent: { purpose: "Promote a coding course during hackathon week", audience: "College students", cta: "sign up" },
};

async function newView() {
  if (ROLE !== "creator") {
    return el("div", { class: "panel" }, el("h1", {}, "New content"),
      el("p", {}, "Only a Creator submits new content."), el("button", { class: "primary", onclick: () => { document.querySelector('#roles [data-role="creator"]').click(); } }, "Switch to Creator"));
  }
  const f = {
    title: el("input", { placeholder: "e.g. Instagram: Winter Bootcamp launch" }),
    channel: el("select", {}, ["Instagram post", "LinkedIn post", "Email", "Advertisement", "Blog / web page", "Press note", "Other"].map((c) => el("option", {}, c))),
    content: el("textarea", { placeholder: "Paste your post, ad, email or document text here…", style: "min-height:180px" }),
    purpose: el("input", { placeholder: "What is this content for?" }),
    audience: el("input", { placeholder: "Who is it for?" }),
    cta: el("input", { placeholder: "e.g. sign up, register, book a seat" }),
  };
  const file = el("input", { type: "file", accept: ".docx", style: "width:auto" });
  file.addEventListener("change", async () => {
    if (!file.files[0]) return;
    const fd = new FormData(); fd.append("file", file.files[0]);
    try { const r = await post("/api/extract-docx", fd); f.content.value = r.text; if (!f.title.value) f.title.value = file.files[0].name.replace(/\.docx$/i, ""); toast(r.truncated ? "Loaded (trimmed to 8,000 characters)." : "Document text loaded."); }
    catch (e) { toast(e.message, true); }
  });
  const submit = el("button", { class: "primary", onclick: async () => {
    submit.disabled = true; submit.textContent = "Checking…";
    try {
      const s = await post("/api/submissions", { title: f.title.value, channel: f.channel.value, content: f.content.value,
        intent: { purpose: f.purpose.value, audience: f.audience.value, cta: f.cta.value } });
      location.hash = `#/s/${s.id}`;
    } catch (e) { toast(e.message, true); submit.disabled = false; submit.textContent = "Check against Brand Kit"; }
  } }, "Check against Brand Kit");
  return el("div", { class: "panel", style: "max-width:860px;margin:auto" },
    el("div", { class: "row", style: "justify-content:space-between" }, el("h1", {}, "New content"),
      el("button", { onclick: () => { f.title.value = SAMPLE_NEW.title; f.channel.value = SAMPLE_NEW.channel; f.content.value = SAMPLE_NEW.content; f.purpose.value = SAMPLE_NEW.intent.purpose; f.audience.value = SAMPLE_NEW.intent.audience; f.cta.value = SAMPLE_NEW.intent.cta; } }, "Fill a sample with mistakes")),
    el("p", { class: "muted" }, "Paste text or upload a .docx. BrandGuard checks it against the Brand Kit before it is published."),
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Title"), f.title), el("div", {}, el("label", {}, "Channel"), f.channel)),
    el("label", {}, "Content"), f.content,
    el("div", { class: "row", style: "margin-top:6px" }, el("span", { class: "small muted" }, "or upload a Word file:"), file),
    el("h3", {}, "Intent (helps keep your message when fixes are suggested)"),
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Purpose"), f.purpose), el("div", {}, el("label", {}, "Audience"), f.audience)),
    el("label", {}, "Call to action"), f.cta,
    el("div", { class: "actions", style: "margin-top:14px" }, submit));
}

// ---------------- review screen
function segments(content, findings, verified) {
  const spans = [];
  findings.filter((f) => f.status === "open" && f.end > f.start).forEach((f) => spans.push({ s: f.start, e: f.end, cls: `hl-${f.severity}`, id: f.id, title: f.message }));
  verified.forEach((v) => { const i = content.indexOf(v.quote); if (i >= 0) spans.push({ s: i, e: i + v.quote.length, cls: "hl-ok", title: `Verified by ${v.fact_id}: ${v.statement}` }); });
  spans.sort((a, b) => a.s - b.s);
  const out = []; let pos = 0;
  for (const sp of spans) {
    if (sp.s < pos) continue;
    out.push(content.slice(pos, sp.s));
    out.push(el("span", { class: `hl ${sp.cls}`, title: sp.title, "data-fid": sp.id || "", onclick: () => {
      if (!sp.id) return; const card = document.getElementById(`f-${sp.id}`);
      if (card) { card.scrollIntoView({ behavior: "smooth", block: "center" }); card.animate([{ background: "#e8eeff" }, { background: "#fff" }], 1200); }
    } }, content.slice(sp.s, sp.e)));
    pos = sp.e;
  }
  out.push(content.slice(pos));
  return out;
}

function findingCard(sub, f) {
  const open = f.status === "open", locked = sub.status === "approved";
  const fixText = f.fix === null || f.fix === undefined ? null : f.fix === "" ? `Remove “${f.quote}”` : `“${f.quote}” → “${f.fix}”`;
  return el("div", { class: `finding ${f.severity} ${open ? "" : "dismissed"}`, id: `f-${f.id}`,
    onmouseenter: () => document.querySelectorAll(`[data-fid="${f.id}"]`).forEach((n) => n.classList.add("flash")),
    onmouseleave: () => document.querySelectorAll(`[data-fid="${f.id}"]`).forEach((n) => n.classList.remove("flash")) },
    el("div", { class: "h" },
      el("span", {}, el("span", { class: `sev c-${f.severity}` }, f.severity), " · ", cap(f.category), f.source === "ai" ? el("span", { class: "ai-tag" }, "AI agent") : null),
      el("span", { class: "row" }, f.cited.map((c) => el("span", { class: "rid", title: ruleInfo(c) }, c)))),
    f.quote ? el("div", { style: "margin:4px 0" }, el("span", { class: "quote" }, f.quote)) : null,
    el("div", {}, f.message),
    fixText ? el("div", { class: "fix" }, "Suggested fix: ", fixText) : null,
    f.fix_note ? el("div", { class: "small muted" }, "Tip: ", f.fix_note) : null,
    open ? el("div", { class: "actions" },
      ROLE === "creator" && f.fix !== null && f.fix !== undefined && !locked ? el("button", { class: "primary", onclick: async () => {
        try { await post(`/api/submissions/${sub.id}/accept`, { finding_id: f.id }); toast("Fix applied. A new version was created and re-checked."); route(); } catch (e) { toast(e.message, true); }
      } }, "Accept fix") : null,
      !locked ? el("button", { onclick: () => ask("Dismiss this finding", "e.g. This is a quote from our partner, so we keep their wording.",
        `Why should “${f.rule_id}” not apply here? Your reason is shown to reviewers and helps find unclear rules.`,
        async (reason) => { await post(`/api/submissions/${sub.id}/dismiss`, { finding_id: f.id, reason }); toast("Dismissed with reason."); route(); }) },
        f.severity === "critical" ? "Dismiss (Manager only)" : "Dismiss…") : null)
      : el("div", { class: "small muted", style: "margin-top:4px" }, `Dismissed: ${f.dismiss_reason}`));
}

function diffWords(a, b) {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  const n = x.length, m = y.length, L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push(x[i]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push(el("del", {}, x[i++]));
    else out.push(el("ins", {}, y[j++]));
  }
  while (i < n) out.push(el("del", {}, x[i++]));
  while (j < m) out.push(el("ins", {}, y[j++]));
  return el("div", { class: "content diff" }, out);
}

function actionBar(sub) {
  const a = sub.approval, st = sub.status;
  if (st === "approved") return el("div", { class: "actionbar" }, el("span", { class: "msg c-ok" }, "✅ Approved and ready to publish. The full trail is in Activity below."));
  if (ROLE === "creator") {
    const hint = a.open_counts.critical ? "Fix or explain the critical issues first; otherwise a Manager must sign off."
      : st === "in_review" ? "Sent for review. Switch to Reviewer or Manager to approve." : st === "changes_requested" ? "The reviewer asked for changes. Update the text, then submit again." : "When you are happy, send it for review.";
    return el("div", { class: "actionbar" }, el("span", { class: "msg" }, hint),
      el("button", { onclick: () => {
        const t = el("textarea", { style: "min-height:200px" }); t.value = sub.current.content;
        modal(el("h2", {}, "Edit text"), el("p", { class: "muted small" }, "Saving creates a new version that is checked again automatically."), t,
          el("div", { class: "actions" }, el("button", { class: "primary", onclick: async () => {
            try { await post(`/api/submissions/${sub.id}/edit`, { text: t.value }); closeModal(); toast("Saved as a new version."); route(); } catch (e) { toast(e.message, true); }
          } }, "Save and re-check"), el("button", { onclick: closeModal }, "Cancel")));
      } }, "Edit text"),
      el("button", { class: "primary", disabled: st === "in_review" || null, onclick: async () => {
        try { await post(`/api/submissions/${sub.id}/submit`); toast("Sent for review. Now switch to Reviewer or Manager."); route(); } catch (e) { toast(e.message, true); }
      } }, st === "in_review" ? "In review" : "Submit for review"));
  }
  const canApprove = st === "in_review" && !a.ai_running && (!a.needs_manager || ROLE === "manager");
  const why = st !== "in_review" ? "The creator has not submitted this for review yet. (Switch to Creator → Submit for review.)"
    : a.ai_running ? "Waiting for the AI review to finish…"
    : a.needs_manager && ROLE !== "manager" ? `${a.open_counts.critical} critical issue(s) are open: only a Manager can sign off. Request changes or switch to Manager.`
    : a.needs_manager ? `You are signing off with ${a.open_counts.critical} open critical issue(s). This is recorded in the audit log.`
    : "No critical issues open. You can approve.";
  return el("div", { class: "actionbar" }, el("span", { class: "msg" }, why),
    el("button", { onclick: () => ask("Comment", "Your comment…", "Comment for the creator", async (t) => { await post(`/api/submissions/${sub.id}/comment`, { text: t }); toast("Comment added."); route(); }) }, "Comment"),
    el("button", { disabled: st === "approved" || null, onclick: () => ask("Request changes", "What should the creator change?", "Instructions for the creator", async (t) => { await post(`/api/submissions/${sub.id}/request-changes`, { text: t }); toast("Changes requested."); route(); }) }, "Request changes"),
    el("button", { class: "good", disabled: !canApprove || null, onclick: () => ask("Approve for publishing", "Optional note", "Approval note (optional)", async (t) => { await post(`/api/submissions/${sub.id}/approve`, { text: t }); toast("Approved ✅"); route(); }, false) },
      a.needs_manager && ROLE === "manager" ? "Approve (manager sign-off)" : "Approve"));
}

async function reviewView(id) {
  await kit();
  const sub = await api(`/api/submissions/${id}`);
  const cur = sub.current, ai = cur.ai, running = cur.ai_status === "running";
  const open = cur.findings.filter((f) => f.status === "open"), dismissed = cur.findings.filter((f) => f.status !== "open");
  if (running) { clearTimeout(pollTimer); pollTimer = setTimeout(() => { if (location.hash === `#/s/${id}`) route(); }, 2500); }

  const left = el("div", {},
    el("div", { class: "panel" },
      el("div", { class: "statusbar" },
        el("div", {}, el("h1", {}, sub.title), el("span", { class: "muted small" }, `${sub.channel} · version ${cur.n} by ${cap(cur.author)} · checked against Brand Kit v${cur.kit_version}`)),
        el("span", { class: `pill st-${sub.status}` }, STATUS_TEXT[sub.status])),
      el("div", { class: "intent" }, el("b", {}, "Intent · "), `Purpose: ${sub.intent.purpose || "—"} · Audience: ${sub.intent.audience || "—"} · Call to action: ${sub.intent.cta || "—"}`),
      sub.provenance_alerts.length ? el("div", { class: "alert" },
        el("b", {}, "⚠ Provenance alert: "), sub.status === "approved" ? "this approved content relies on a fact that is no longer valid." : "a fact this draft relies on is no longer valid.",
        el("ul", {}, sub.provenance_alerts.map((a) => el("li", {}, a))),
        sub.status !== "approved" ? el("button", { class: "primary", onclick: async () => {
          try { await post(`/api/submissions/${sub.id}/recheck`); toast("Re-checked against the latest Brand Kit."); route(); } catch (e) { toast(e.message, true); }
        } }, "Re-check now") : el("div", { class: "small" }, "Create updated content from “+ New content” before publishing this again.")) : null,
      el("div", { class: "row", style: "margin-bottom:6px" }, counts(sub.approval.open_counts, cur.verified.length)),
      el("div", { class: "content" }, segments(cur.content, cur.findings, cur.verified)),
      el("h3", {}, "Suggested version"),
      running ? el("div", { class: "rewrite muted" }, el("span", { class: "spinner" }), "The AI Review Agent is checking tone, messaging and claims, and preparing a suggested version…")
        : ai && ai.rewrite && ai.rewrite.trim() !== cur.content.trim() ? el("div", {},
          el("div", { class: "rewrite" }, ai.rewrite),
          ai.rewrite_check ? el("div", { class: "small", style: `margin-top:4px;color:${ai.rewrite_check.ok ? "var(--good)" : "var(--major)"}` },
            ai.rewrite_check.ok ? "✔ Intent kept: call to action and brand name kept, no unapproved numbers." : "⚠ " + ai.rewrite_check.problems.join("; ")) : null,
          ROLE === "creator" && sub.status !== "approved" ? el("div", { class: "actions" }, el("button", { class: "primary", onclick: async () => {
            try { await post(`/api/submissions/${sub.id}/apply-rewrite`); toast("Suggested version accepted as a new version."); route(); } catch (e) { toast(e.message, true); }
          } }, "Accept suggested version")) : null)
        : el("div", { class: "small muted" }, open.length ? "No automatic rewrite available: use the fixes on the right or edit the text." : "Nothing to change. ✔"),
      ai ? el("details", {}, el("summary", {}, ai.mode === "ai" ? `How the Review Agent worked (${ai.tool_log.length} tool calls, ${ai.model})` : "About this review"),
        ai.note ? el("p", { class: "small muted" }, ai.note) : null,
        el("div", { class: "tl" }, ai.tool_log.map((t) => el("div", {}, `${t.ok ? "✔" : "✖"} ${t.tool}(${Object.values(t.input).map((v) => String(v).slice(0, 60)).join(", ")})`))),
        ai.rejected && ai.rejected.length ? el("p", { class: "small" }, `Policy guard rejected ${ai.rejected.length} AI finding(s) that did not cite a valid rule or quote.`) : null) : null),
    el("div", { class: "panel", style: "margin-top:14px" },
      el("h2", {}, "Version history"),
      el("table", {}, el("tr", {}, el("th", {}, "Version"), el("th", {}, "By"), el("th", {}, "Change"), el("th", {}, "When"), el("th", {}, "")),
        sub.versions.slice().reverse().map((v) => el("tr", {}, el("td", {}, `v${v.n}`), el("td", {}, cap(v.author)), el("td", {}, v.note), el("td", { class: "small" }, when(v.created_at)),
          el("td", {}, v.n > 1 ? el("button", { onclick: () => { const prev = sub.versions.find((p) => p.n === v.n - 1); modal(el("h2", {}, `Changes in v${v.n}`), diffWords(prev.content, v.content), el("div", { class: "actions" }, el("button", { onclick: closeModal }, "Close"))); } }, "Compare") : null)))),
      sub.comments.length ? el("div", {}, el("h3", {}, "Comments"), sub.comments.map((c) => el("div", { class: "small", style: "margin:4px 0" }, el("b", {}, cap(c.role)), `: ${c.text}`, el("span", { class: "muted" }, ` · ${when(c.created_at)}`)))) : null,
      el("h3", {}, "Activity (audit log)"),
      el("div", { class: "tl" }, sub.audit.map((a) => el("div", {}, el("b", {}, cap(a.role)), ` · ${a.action.replace(".", " ")} · ${a.detail || ""} `, el("span", { class: "muted" }, when(a.ts)))))));

  const right = el("div", {},
    el("div", { class: "panel" },
      el("h2", {}, `Issues found (${open.length})`),
      open.length ? open.map((f) => findingCard(sub, f)) : el("div", { class: "verified" }, "✔ No open issues. This content follows the Brand Kit."),
      running ? el("div", { class: "small muted" }, el("span", { class: "spinner" }), "More findings may appear when the AI review finishes.") : null,
      cur.verified.length ? el("div", {}, el("h3", {}, "Verified claims"), cur.verified.map((v) =>
        el("div", { class: "verified" }, "✔ “", v.quote, "” matches ", el("span", { class: "rid", title: ruleInfo(v.fact_id) }, v.fact_id), ` (${v.source}, valid until ${v.valid_until}; checked against Brand Kit v${cur.kit_version})`))) : null,
      dismissed.length ? el("details", {}, el("summary", {}, `Dismissed (${dismissed.length})`), dismissed.map((f) => findingCard(sub, f))) : null));

  return el("div", {}, el("div", { class: "row", style: "margin-bottom:10px" }, el("a", { href: "#/" }, "← Inbox")),
    el("div", { class: "review" }, left, right), actionBar(sub));
}

// ---------------- brand kit
let kitTab = "rules";
async function kitView() {
  const k = await kit(true);
  const isMgr = ROLE === "manager", today = new Date().toISOString().slice(0, 10);
  const del = (kind, id) => isMgr ? el("button", { class: "danger small", onclick: async () => {
    if (!confirm(`Delete ${id}?`)) return; try { await api(`/api/kit/${kind}/${id}`, { method: "DELETE" }); toast("Deleted."); route(); } catch (e) { toast(e.message, true); } } }, "Delete") : null;
  const tabs = el("div", { class: "tabs" }, [["rules", `Rules (${k.rules.length})`], ["facts", `Approved facts (${k.facts.length})`], ["glossary", `Glossary (${k.glossary.length})`], ["voice", "Voice & colours"]]
    .map(([t, label]) => el("button", { class: kitTab === t ? "on" : "", onclick: () => { kitTab = t; route(); } }, label)));
  let body;
  if (kitTab === "rules") {
    body = el("table", {}, el("tr", {}, el("th", {}, "ID"), el("th", {}, "Rule"), el("th", {}, "Severity"), el("th", {}, "Examples"), el("th", {}, "")),
      k.rules.map((r) => el("tr", {}, el("td", {}, el("span", { class: "rid" }, r.id)), el("td", {}, el("b", {}, r.title), el("div", { class: "small muted" }, r.text)),
        el("td", { class: `c-${r.severity}` }, r.severity), el("td", { class: "small" }, r.good ? el("div", { class: "c-ok" }, "✔ ", r.good) : null, r.bad ? el("div", { class: "c-critical" }, "✖ ", r.bad) : null), el("td", {}, del("rules", r.id)))));
  } else if (kitTab === "facts") {
    const usage = await api("/api/kit/usage");
    const edit = (f) => isMgr ? el("button", { class: "small", onclick: () => {
      const inp = { statement: el("input", { value: f.statement }), value: el("input", { value: f.value }), valid_until: el("input", { value: f.valid_until }) };
      modal(el("h2", {}, `Update ${f.id}`), el("p", { class: "small muted" }, "Every unapproved draft is re-checked automatically, and approved content that relied on the old value gets a provenance alert."),
        el("label", {}, "Statement"), inp.statement, el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Value"), inp.value), el("div", {}, el("label", {}, "Valid until (YYYY-MM-DD)"), inp.valid_until)),
        el("div", { class: "actions" }, el("button", { class: "primary", onclick: async () => {
          try { await api(`/api/kit/facts/${f.id}`, { method: "PUT", body: JSON.stringify({ statement: inp.statement.value, value: Number(inp.value.value), valid_until: inp.valid_until.value }) }); closeModal(); toast("Fact updated. Drafts were re-checked automatically."); route(); } catch (e) { toast(e.message, true); }
        } }, "Save"), el("button", { onclick: closeModal }, "Cancel")));
    } }, "Edit") : null;
    body = el("table", {}, el("tr", {}, el("th", {}, "ID"), el("th", {}, "Approved fact"), el("th", {}, "Source / owner"), el("th", {}, "Valid until"), el("th", {}, "Used in"), el("th", {}, "")),
      k.facts.map((f) => el("tr", {}, el("td", {}, el("span", { class: "rid" }, f.id)), el("td", {}, f.statement, el("div", { class: "small muted" }, `Value ${f.value} ${f.unit} · keywords: ${f.keywords.join(", ")}`)),
        el("td", { class: "small" }, f.source, el("div", { class: "muted" }, f.owner)), el("td", { class: f.valid_until < today ? "c-critical" : "" }, f.valid_until, f.valid_until < today ? " (expired)" : ""),
        el("td", { class: "small" }, (usage[f.id] || []).length ? (usage[f.id] || []).map((u) => el("div", {}, el("a", { href: `#/s/${u.id}` }, u.title), el("span", { class: "muted" }, ` (${STATUS_TEXT[u.status]})`))) : el("span", { class: "muted" }, "—")),
        el("td", {}, el("div", { class: "row" }, edit(f), del("facts", f.id))))));
  } else if (kitTab === "glossary") {
    body = el("table", {}, el("tr", {}, el("th", {}, "ID"), el("th", {}, "Use"), el("th", {}, "Instead of"), el("th", {}, "Note"), el("th", {}, "")),
      k.glossary.map((t) => el("tr", {}, el("td", {}, el("span", { class: "rid" }, t.id)), el("td", { class: "c-ok" }, el("b", {}, t.preferred)), el("td", { class: "c-critical" }, t.variants.join(", ")), el("td", { class: "small" }, t.note), el("td", {}, del("glossary", t.id)))));
  } else {
    body = el("div", {}, el("p", {}, el("b", {}, "Voice: "), k.brand.voice), el("p", { class: "muted" }, k.brand.description),
      k.brand.palette.map((c) => el("div", { style: "margin:6px 0" }, el("span", { class: "swatch", style: `background:${c.hex}` }), `${c.name} · ${c.hex}`)));
  }
  let form = null;
  if (isMgr && kitTab !== "voice") {
    const fields = kitTab === "facts" ? [["statement", "Fact statement"], ["value", "Number"], ["unit", "Unit (students, %, partners…)"], ["keywords", "Keywords (comma separated)"], ["source", "Source"], ["owner", "Owner"], ["valid_until", "Valid until (YYYY-MM-DD)"]]
      : kitTab === "rules" ? [["title", "Rule title"], ["text", "Explanation"], ["category", "Category (tone, claims, messaging, accessibility)"], ["severity", "Severity (critical, major, minor)"], ["patterns", "Banned phrases (comma separated, optional)"], ["good", "Good example"], ["bad", "Bad example"]]
      : [["preferred", "Preferred term"], ["variants", "Wrong variants (comma separated)"], ["note", "Note"]];
    const inputs = Object.fromEntries(fields.map(([k2]) => [k2, el("input", {})]));
    form = el("div", { class: "panel", style: "margin-top:14px" }, el("h2", {}, "Add to Brand Kit (Manager)"),
      el("div", { class: "grid2" }, fields.map(([k2, label]) => el("div", {}, el("label", {}, label), inputs[k2]))),
      el("div", { class: "actions" }, el("button", { class: "primary", onclick: async () => {
        const body2 = Object.fromEntries(Object.entries(inputs).map(([k2, i]) => [k2, ["keywords", "variants", "patterns"].includes(k2) ? i.value.split(",") : i.value]));
        try { await post(`/api/kit/${kitTab}`, body2); toast("Added. Open drafts were re-checked automatically."); route(); } catch (e) { toast(e.message, true); }
      } }, "Add")));
  }
  return el("div", {},
    el("h1", {}, `Brand Kit: ${k.brand.name}`),
    el("p", { class: "muted" }, `Version ${k.version}. Every finding cites one of these IDs. `, isMgr ? "You can add or remove items." : "Switch to Manager to edit."),
    tabs, el("div", { class: "panel" }, body), form);
}

async function insightsView() {
  await kit();
  const rows = await api("/api/insights");
  return el("div", {}, el("h1", {}, "Rule insights"),
    el("p", { class: "muted" }, "How often each rule's findings are accepted or dismissed. Rules that people keep dismissing may be unclear: this is the data behind the Guideline Clarity Score (coming in Round 3)."),
    el("div", { class: "panel" }, rows.length ? el("table", {}, el("tr", {}, el("th", {}, "Rule"), el("th", {}, "Accepted"), el("th", {}, "Dismissed"), el("th", {}, "Dismiss rate")),
      rows.map((r) => el("tr", {}, el("td", {}, el("span", { class: "rid", title: ruleInfo(r.rule_id) }, r.rule_id), " ", r.title), el("td", {}, r.accepted), el("td", {}, r.rejected),
        el("td", { class: r.dismiss_rate >= 0.5 ? "c-critical" : "" }, `${Math.round(r.dismiss_rate * 100)}%`, r.dismiss_rate >= 0.5 && r.rejected >= 2 ? " · possibly unclear" : ""))))
      : el("div", { class: "empty" }, "No decisions yet. Accept or dismiss findings on a post and they will appear here.")));
}

async function activityView() {
  const rows = await api("/api/audit");
  return el("div", {}, el("h1", {}, "Activity"), el("p", { class: "muted" }, "Every action by every role, newest first."),
    el("div", { class: "panel" }, el("table", {}, el("tr", {}, el("th", {}, "When"), el("th", {}, "Role"), el("th", {}, "Action"), el("th", {}, "Content"), el("th", {}, "Detail")),
      rows.map((a) => el("tr", {}, el("td", { class: "small" }, when(a.ts)), el("td", {}, cap(a.role)), el("td", {}, a.action),
        el("td", {}, a.submission_id ? el("a", { href: `#/s/${a.submission_id}` }, a.title || `#${a.submission_id}`) : "—"), el("td", { class: "small" }, a.detail))))));
}

function howView() {
  return el("div", { class: "panel how", style: "max-width:860px;margin:auto" },
    el("h1", {}, "How BrandGuard works"),
    el("ol", {},
      el("li", {}, el("b", {}, "Brand Kit. "), "Rules with IDs, a glossary, approved facts (with source, owner and expiry date) and brand colours."),
      el("li", {}, el("b", {}, "Rule checks in code. "), "Glossary terms, banned phrases (hype, pressure, guarantees, superlatives), readability, the call to action, and every number compared with approved facts: supported, contradicted, outdated (expired fact) or no source."),
      el("li", {}, el("b", {}, "AI Review Agent. "), "When an AI key is configured, Claude plans further checks (tone, messaging, claims in context), calls Brand Kit tools for evidence, adds findings and proposes a full suggested version. A policy guard keeps only findings that cite a rule returned by a tool and quote the text exactly; the suggested version must keep the call to action and brand name and may not invent numbers."),
      el("li", {}, el("b", {}, "People decide. "), "Creators accept, edit or dismiss each fix (dismissals need a reason). Reviewers approve; open critical issues need a Manager's sign-off. The agent has no approve or publish action."),
      el("li", {}, el("b", {}, "Audit and learning. "), "Every action is logged. Dismissal reasons per rule feed the insights page, which shows rules that may be unclear.")),
    el("p", { class: "muted small" }, "Built by team Cite or Flag for CodeZen India 2026. The demo brand and its facts are fictional."));
}

// ------------------------------------------------------------------ router
async function route() {
  clearTimeout(pollTimer);
  const h = location.hash || "#/";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === h || (h.startsWith("#/s/") && a.dataset.nav === "inbox")));
  const app = $("#app");
  try {
    if (!STATUS) await loadStatus();
    let view;
    if (h.startsWith("#/s/")) view = await reviewView(h.slice(4));
    else if (h === "#/new") view = await newView();
    else if (h === "#/kit") view = await kitView();
    else if (h === "#/insights") view = await insightsView();
    else if (h === "#/activity") view = await activityView();
    else if (h === "#/how") view = howView();
    else view = await inbox();
    const y = window.scrollY;
    app.replaceChildren(view);
    if (h.startsWith("#/s/")) window.scrollTo(0, y);
  } catch (e) {
    app.replaceChildren(el("div", { class: "panel empty" }, e.message, el("div", { class: "actions", style: "justify-content:center" }, el("a", { href: "#/" }, "Back to inbox"))));
  }
}
window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
renderRoles();
route();
