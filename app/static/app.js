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

// Each browser has its own demo workspace, so visitors never see or undo each other's changes.
const WS = (() => {
  const make = () => "ws-" + (window.crypto && crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36));
  try { let w = localStorage.getItem("bg-ws"); if (!w) { w = make(); localStorage.setItem("bg-ws", w); } return w; } catch { return make(); }
})();
let busy = 0;
async function api(path, opts = {}) {
  const write = opts.method && opts.method !== "GET";
  if (write) { busy++; document.body.classList.add("busy"); }  // blocks double clicks while a change is being saved
  try {
    let res;
    try {
      res = await fetch(path, { ...opts, headers: { "X-Role": ROLE, "X-Workspace": WS, ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } });
    } catch { throw new Error("Cannot reach the server. Check your connection; the free server may take up to a minute to wake up."); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 409) setTimeout(route, 0);  // stale screen: show the latest state
      throw new Error(typeof data.detail === "string" ? data.detail : res.status >= 500 ? "The server had a problem. Please try again." : "Something went wrong. Please try again.");
    }
    return data;
  } finally {
    if (write && --busy === 0) document.body.classList.remove("busy");
  }
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
  if (!confirm("Reset your demo workspace to the original samples? Your changes in this browser will be removed.")) return;
  try { await post("/api/reset"); KIT = null; toast("Your demo workspace was reset to the samples."); location.hash = "#/"; route(); } catch (err) { toast(err.message, true); }
});

let statusAt = 0;
async function loadStatus() {
  STATUS = await api("/api/status"); statusAt = Date.now();
  const b = $("#ai-badge");
  const bud = STATUS.ai_budget, paused = bud && bud.paused;
  b.textContent = !STATUS.ai ? "AI review: rules-only mode" : bud.outage ? "AI unavailable: rules-only mode" : paused ? "AI review: paused (demo limit)" : `AI review: on (${STATUS.model})`;
  b.className = "badge" + (STATUS.ai && !paused ? " on" : "");
  b.title = !STATUS.ai ? "No AI key configured: exact rule and fact checks still run."
    : (paused ? paused + " " : "Claude reviews tone, messaging and claims using Brand Kit tools. ")
      + `Demo AI spend so far: $${bud.spent_usd.toFixed(4)} of $${bud.limit_usd.toFixed(2)} · ${bud.reviews} AI reviews, ${bud.reused} reused from cache.`;
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
  const first = subs.find((s) => s.kind === "text" && s.open_counts.critical) || subs[0];
  return el("div", {},
    el("div", { class: "guide" },
      el("div", { style: "flex:1;min-width:260px" },
        el("h2", {}, "Try BrandGuard in 2 minutes"),
        el("ol", {},
          el("li", {}, "Open a post below. Every issue shows the brand rule or approved fact it breaks, and a suggested fix."),
          el("li", {}, "As Creator: accept fixes (or the full suggested version), then click “Submit for review”."),
          el("li", {}, "Switch to Reviewer or Manager (top right) and approve. Critical issues need a Manager.")),
        el("div", { class: "small", style: "margin-top:6px" }, "Also try: the ", el("b", {}, "poster"), " (image colours, contrast and alt text) · ",
          el("a", { href: "#/insights" }, "Guideline Clarity"), " (as Manager, draft a clearer rule) · ",
          el("a", { href: "#/kit", onclick: () => { kitTab = "import"; } }, "Brand Kit → Import"), " (as Manager, read a guideline PDF).")),
      el("div", { class: "row" },
        first ? el("button", { class: "primary", onclick: () => { location.hash = `#/s/${first.id}`; } }, "Start with a sample →") : null,
        el("button", { onclick: () => { location.hash = "#/new"; } }, "Check your own text or image"))),
    el("div", { class: "row", style: "justify-content:space-between;margin-bottom:10px" },
      el("h1", {}, "Content inbox"), el("span", { class: "muted small" }, `Brand: ${STATUS.brand} (fictional demo) · Brand Kit v${STATUS.kit_version}`)),
    subs.length ? el("div", { class: "cards" }, subs.map((s) =>
      el("div", { class: "card", onclick: () => { location.hash = `#/s/${s.id}`; } },
        s.thumb ? el("img", { class: "thumb", src: s.thumb, alt: "" }) : null,
        el("div", { class: "row", style: "justify-content:space-between" },
          el("span", { class: "t" }, s.title), el("span", { class: `pill st-${s.status}` }, STATUS_TEXT[s.status])),
        el("span", { class: "muted small" }, `${s.channel} · v${s.version} · ${when(s.updated_at)}`),
        el("span", { class: "p" }, s.preview + (s.preview.length >= 140 ? "…" : "")),
        counts(s.open_counts, s.verified),
        s.alerts ? el("span", { class: "cnt c-major" }, `⚠ ${s.alerts} provenance alert${s.alerts > 1 ? "s" : ""}: a cited fact changed or expired`) : null,
        s.ai_status === "running" ? el("span", { class: "small muted" }, el("span", { class: "spinner" }), "AI review running…") : null)))
      : el("div", { class: "empty panel" }, "No content yet. Click “+ New content”."));
}

let newMode = "text";
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
  const img = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp", style: "width:auto" });
  const preview = el("img", { class: "preview hidden", alt: "" });
  const alt = el("input", { placeholder: "Describe the image for screen-reader users (leave empty to see the check)" });
  img.addEventListener("change", () => {
    const file = img.files[0]; if (!file) return;
    if (file.size > 4000000) { toast("The image is larger than 4 MB.", true); img.value = ""; return; }
    preview.src = URL.createObjectURL(file); preview.classList.remove("hidden");
    if (!f.title.value) f.title.value = file.name.replace(/\.[a-z]+$/i, "");
  });
  let mode = newMode;
  const textPart = el("div", {}, el("label", {}, "Content"), f.content,
    el("div", { class: "row", style: "margin-top:6px" }, el("span", { class: "small muted" }, "or upload a Word file:"), file));
  const imagePart = el("div", {}, el("label", {}, "Image (PNG, JPG or WebP, up to 4 MB)"), img, preview,
    el("label", {}, "Alt text"), alt, el("label", {}, "Caption (optional)"));
  const tabs = el("div", { class: "tabs" });
  const setMode = (m) => {
    mode = newMode = m;
    tabs.replaceChildren(...[["text", "Text or Word file"], ["image", "Image (poster, social post)"]].map(([k, label]) =>
      el("button", { class: mode === k ? "on" : "", onclick: () => setMode(k) }, label)));
    textPart.classList.toggle("hidden", m !== "text");
    imagePart.classList.toggle("hidden", m !== "image");
    if (m === "image") imagePart.append(f.content); else textPart.insertBefore(f.content, textPart.children[1]);
    f.content.placeholder = m === "image" ? "The text that goes with the image (optional)" : "Paste your post, ad, email or document text here…";
  };
  setMode(mode);
  const submit = el("button", { class: "primary", onclick: async () => {
    submit.disabled = true; submit.textContent = "Checking…";
    try {
      let s;
      if (mode === "image") {
        if (!img.files[0]) throw new Error("Choose an image first.");
        const fd = new FormData();
        fd.append("file", img.files[0]); fd.append("title", f.title.value); fd.append("channel", f.channel.value);
        fd.append("caption", f.content.value); fd.append("alt", alt.value);
        fd.append("purpose", f.purpose.value); fd.append("audience", f.audience.value); fd.append("cta", f.cta.value);
        s = await post("/api/submissions/image", fd);
      } else {
        s = await post("/api/submissions", { title: f.title.value, channel: f.channel.value, content: f.content.value,
          intent: { purpose: f.purpose.value, audience: f.audience.value, cta: f.cta.value } });
      }
      location.hash = `#/s/${s.id}`;
    } catch (e) { toast(e.message, true); submit.disabled = false; submit.textContent = "Check against Brand Kit"; }
  } }, "Check against Brand Kit");
  return el("div", { class: "panel", style: "max-width:860px;margin:auto" },
    el("div", { class: "row", style: "justify-content:space-between" }, el("h1", {}, "New content"),
      el("button", { onclick: () => { setMode("text"); f.title.value = SAMPLE_NEW.title; f.channel.value = SAMPLE_NEW.channel; f.content.value = SAMPLE_NEW.content; f.purpose.value = SAMPLE_NEW.intent.purpose; f.audience.value = SAMPLE_NEW.intent.audience; f.cta.value = SAMPLE_NEW.intent.cta; } }, "Fill a sample with mistakes")),
    el("p", { class: "muted" }, "Paste text, upload a .docx or upload an image. BrandGuard checks it against the Brand Kit before it is published."),
    tabs,
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Title"), f.title), el("div", {}, el("label", {}, "Channel"), f.channel)),
    textPart, imagePart,
    el("h3", {}, "Intent (helps keep your message when fixes are suggested)"),
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Purpose"), f.purpose), el("div", {}, el("label", {}, "Audience"), f.audience)),
    el("label", {}, "Call to action"), f.cta,
    el("div", { class: "actions", style: "margin-top:14px" }, submit));
}

// ---------------- review screen
function segments(content, findings, verified, target = "text") {
  const spans = [];
  findings.filter((f) => f.status === "open" && f.end > f.start && (f.target || "text") === target).forEach((f) => spans.push({ s: f.start, e: f.end, cls: `hl-${f.severity}`, id: f.id, title: f.message }));
  verified.forEach((v) => { const i = content.indexOf(v.quote); if (i >= 0) spans.push({ s: i, e: i + v.quote.length, cls: "hl-ok", title: `Verified by ${v.fact_id}: ${v.statement}` }); });
  // Overlapping findings: cut the text at every boundary and show the most severe (then most specific) finding
  // for each piece, so a long-sentence hint never hides a critical claim inside it.
  const rank = { "hl-critical": 0, "hl-major": 1, "hl-ok": 2, "hl-minor": 3 };
  const cuts = [...new Set([0, content.length, ...spans.flatMap((sp) => [sp.s, sp.e])])].sort((a, b) => a - b);
  const out = [];
  for (let i = 0; i < cuts.length - 1; i++) {
    const a = cuts[i], b = cuts[i + 1], piece = content.slice(a, b);
    const cover = spans.filter((sp) => sp.s <= a && sp.e >= b)
      .sort((x, y) => rank[x.cls] - rank[y.cls] || (x.e - x.s) - (y.e - y.s));
    if (!cover.length) { out.push(piece); continue; }
    const sp = cover[0];
    out.push(el("span", { class: `hl ${sp.cls}`, title: cover.map((c) => c.title).join("\n"), "data-fid": sp.id || "", onclick: () => {
      if (!sp.id) return; const card = document.getElementById(`f-${sp.id}`);
      if (card) { card.scrollIntoView({ behavior: "smooth", block: "center" }); card.animate([{ background: "#e8eeff" }, { background: "#fff" }], 1200); }
    } }, piece));
  }
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
    f.target && f.target !== "text" ? el("div", { class: "where" }, { image: "🖼 In the image", alt: "♿ Alt text", image_text: "🖼 Text in the image" }[f.target] || "") : null,
    f.quote ? el("div", { style: "margin:4px 0" }, f.swatch ? el("span", { class: "swatch", style: `background:${f.swatch}` }) : null, el("span", { class: "quote" }, f.quote),
      f.suggest && f.rule_id === "VIS-01" ? el("span", {}, " → ", el("span", { class: "swatch", style: `background:${f.suggest};margin-left:4px` }), el("span", { class: "quote" }, f.suggest)) : null) : null,
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
  const ops = []; let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { ops.push(["=", x[i]]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) ops.push(["-", x[i++]]);
    else ops.push(["+", y[j++]]);
  }
  while (i < n) ops.push(["-", x[i++]]);
  while (j < m) ops.push(["+", y[j++]]);
  // Group each run of changes as "removed words" then "added words", so a rewrite reads as two phrases, not confetti.
  const out = []; let dels = [], adds = [];
  const flush = () => {
    const dr = dels.join(""), ar = adds.join(""), d = dr.trim(), a = ar.trim(), raw = ar || dr;
    if ((d || a) && /^\s/.test(raw)) out.push(" ");
    if (d) out.push(el("del", {}, d));
    if (d && a) out.push(" ");
    if (a) out.push(el("ins", {}, a));
    if ((d || a) && /\s$/.test(raw)) out.push(" ");
    dels = []; adds = [];
  };
  ops.forEach(([t, s], k) => {
    if (t !== "=") return (t === "-" ? dels : adds).push(s);
    const inRun = (dels.length || adds.length) && /^\s+$/.test(s) && ops[k + 1] && ops[k + 1][0] !== "=";
    if (inRun) { dels.push(s); adds.push(s); return; }
    flush(); out.push(s);
  });
  flush();
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
        const isImg = sub.kind === "image";
        const t = el("textarea", { style: `min-height:${isImg ? 90 : 200}px` }); t.value = sub.current.content;
        const altIn = el("input", { value: (sub.current.media || {}).alt || "" });
        modal(el("h2", {}, isImg ? "Edit caption and alt text" : "Edit text"), el("p", { class: "muted small" }, "Saving creates a new version that is checked again automatically."),
          isImg ? el("label", {}, "Caption") : null, t, isImg ? el("label", {}, "Alt text") : null, isImg ? altIn : null,
          el("div", { class: "actions" }, el("button", { class: "primary", onclick: async () => {
            try { await post(`/api/submissions/${sub.id}/edit`, isImg ? { text: t.value, alt: altIn.value } : { text: t.value }); closeModal(); toast("Saved as a new version."); route(); } catch (e) { toast(e.message, true); }
          } }, "Save and re-check"), el("button", { onclick: closeModal }, "Cancel")));
      } }, sub.kind === "image" ? "Edit caption / alt text" : "Edit text"),
      sub.kind === "image" ? (() => {
        const up = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp", class: "hidden" });
        up.addEventListener("change", async () => {
          if (!up.files[0]) return;
          const fd = new FormData(); fd.append("file", up.files[0]);
          try { await post(`/api/submissions/${sub.id}/image`, fd); toast("New image uploaded as a new version."); route(); } catch (e) { toast(e.message, true); }
        });
        return el("span", {}, up, el("button", { onclick: () => up.click() }, "Upload new image"));
      })() : null,
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

  const isImg = sub.kind === "image";
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
      isImg ? imageBlock(sub, cur, ai, running) : null,
      isImg ? el("h3", {}, "Caption") : null,
      el("div", { class: "content" }, isImg && !cur.content ? el("span", { class: "muted" }, "No caption.") : segments(cur.content, cur.findings, cur.verified)),
      isImg ? null : el("h3", {}, "Suggested version"),
      isImg ? null : running ? el("div", { class: "rewrite muted" }, el("span", { class: "spinner" }), "The AI Review Agent is checking tone, messaging and claims, and preparing a suggested version…")
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
        ai.mode === "ai" ? el("p", { class: "small muted" }, ai.cached
          ? "Reused the earlier AI review of this exact text and Brand Kit: no new API cost."
          : `API cost of this review: $${(ai.cost_usd || 0).toFixed(4)}.`) : null,
        el("div", { class: "tl" }, ai.tool_log.map((t) => el("div", {}, `${t.ok ? "✔" : "✖"} ${t.tool}(${Object.values(t.input).map((v) => String(v).slice(0, 60)).join(", ")})`))),
        ai.rejected && ai.rejected.length ? el("p", { class: "small" }, `Policy guard rejected ${ai.rejected.length} AI finding(s) that did not cite a valid rule or quote.`) : null) : null),
    el("div", { class: "panel", style: "margin-top:14px" },
      el("h2", {}, "Version history"),
      el("table", {}, el("tr", {}, el("th", {}, "Version"), el("th", {}, "By"), el("th", {}, "Change"), el("th", {}, "When"), el("th", {}, "")),
        sub.versions.slice().reverse().map((v) => el("tr", {}, el("td", {}, `v${v.n}`), el("td", {}, cap(v.author)), el("td", {}, v.note), el("td", { class: "small" }, when(v.created_at)),
          el("td", {}, v.n > 1 ? el("button", { onclick: () => { const prev = sub.versions.find((p) => p.n === v.n - 1); modal(el("h2", {}, `Changes in v${v.n}`),
            isImg ? el("h3", {}, "Caption") : null, diffWords(prev.content, v.content),
            isImg ? el("h3", {}, "Alt text") : null, isImg ? diffWords(prev.alt || "", v.alt || "") : null,
            el("div", { class: "actions" }, el("button", { onclick: closeModal }, "Close"))); } }, "Compare") : null)))),
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

function imageBlock(sub, cur, ai, running) {
  const m = cur.media || {};
  const boxes = ((ai && ai.elements) || []).map((e) => {
    const finding = cur.findings.find((f) => f.rule_id === "ACC-05" && f.quote === e.text.slice(0, 80) && f.status === "open");
    const bad = e.measure && !e.measure.pass;
    return el("div", { class: "box" + (bad ? "" : " ok"), "data-fid": finding ? finding.id : "",
      style: `left:${e.box.x * 100}%;top:${e.box.y * 100}%;width:${e.box.w * 100}%;height:${e.box.h * 100}%`,
      title: e.measure ? `“${e.text}”: contrast ${e.measure.ratio}:1 (needs ${e.measure.need}:1)` : `“${e.text}”`,
      onclick: () => { if (finding) { const c = document.getElementById(`f-${finding.id}`); if (c) c.scrollIntoView({ behavior: "smooth", block: "center" }); } } },
      bad ? el("span", { class: "boxlabel" }, `${e.measure.ratio}:1`) : null);
  });
  (m.checks || []).forEach((c) => {
    const finding = cur.findings.find((f) => f.rule_id === "ACC-05" && f.quote === `Marked text #${c.n}` && f.status === "open");
    boxes.push(el("div", { class: "box marked" + (c.measure.pass ? " ok" : ""), "data-fid": finding ? finding.id : "",
      style: `left:${c.box.x * 100}%;top:${c.box.y * 100}%;width:${c.box.w * 100}%;height:${c.box.h * 100}%`,
      title: `Marked text #${c.n}: contrast ${c.measure.ratio}:1 (needs ${c.measure.need}:1)` },
      el("span", { class: "boxlabel" + (c.measure.pass ? " ok" : "") }, `#${c.n} ${c.measure.ratio}:1`)));
  });
  const wrap = el("div", { class: "imgwrap" }, el("img", { src: `data:${m.mime};base64,${m.data}`, alt: m.alt || "", draggable: "false" }), boxes);
  const size = el("select", { style: "width:auto" }, el("option", { value: "normal" }, "normal text (4.5:1)"), el("option", { value: "large" }, "large headline (3:1)"));
  const hint = el("span", { class: "small muted" });
  let drawing = false;
  const checkBtn = el("button", { onclick: () => {
    drawing = !drawing; wrap.classList.toggle("drawing", drawing);
    checkBtn.textContent = drawing ? "Cancel" : "Check text contrast";
    hint.textContent = drawing ? "Drag a box tightly around one line of text on the image." : "";
  } }, "Check text contrast");
  let start = null, draft = null;
  const rel = (e) => { const r = wrap.getBoundingClientRect(); return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) }; };
  wrap.addEventListener("pointerdown", (e) => {
    if (!drawing) return; e.preventDefault(); wrap.setPointerCapture(e.pointerId); start = rel(e);
    draft = el("div", { class: "box draft" }); wrap.append(draft);
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!start || !draft) return; const p = rel(e);
    Object.assign(draft.style, { left: `${Math.min(p.x, start.x) * 100}%`, top: `${Math.min(p.y, start.y) * 100}%`, width: `${Math.abs(p.x - start.x) * 100}%`, height: `${Math.abs(p.y - start.y) * 100}%` });
  });
  wrap.addEventListener("pointerup", async (e) => {
    if (!start) return; const p = rel(e), s0 = start; start = null;
    const box = { x: Math.min(p.x, s0.x), y: Math.min(p.y, s0.y), w: Math.abs(p.x - s0.x), h: Math.abs(p.y - s0.y), size: size.value };
    try {
      const r = await post(`/api/submissions/${sub.id}/contrast`, box);
      toast(r.measure.pass ? `✔ Readable: ${r.measure.ratio}:1 (needs ${r.measure.need}:1).` : `✖ Too low: ${r.measure.ratio}:1 (needs ${r.measure.need}:1). Added as an issue.`, !r.measure.pass);
      route();
    } catch (err) { toast(err.message, true); if (draft) draft.remove(); }
  });
  const locked = sub.status === "approved";
  return el("div", {},
    wrap,
    el("div", { class: "small muted", style: "margin:4px 0 4px" }, `${m.width} × ${m.height} px · red boxes = text below WCAG contrast, green = readable`),
    locked ? null : el("div", { class: "row", style: "margin-bottom:8px" }, checkBtn, size, hint),
    el("h3", {}, "Main colours vs brand palette"),
    el("div", { class: "row" }, (m.colours || []).map((c) => el("span", { class: "colour " + (c.ok ? "ok" : "bad"), title: c.nearest ? `Nearest brand colour: ${c.nearest.name} ${c.nearest.hex} (ΔE ${c.nearest.delta_e})` : "" },
      el("span", { class: "swatch", style: `background:${c.hex}` }), `${c.hex} · ${Math.round(c.share * 100)}% · ${c.ok ? "✔ brand or neutral" : "✖ off-palette"}`))),
    el("h3", {}, "Alt text"),
    el("div", { class: m.alt ? "" : "c-major" }, m.alt ? `“${m.alt}”` : "Missing"),
    el("h3", {}, "Text in the image"),
    running ? el("div", { class: "small muted" }, el("span", { class: "spinner" }), "The Review Agent is reading the text in the image; contrast is measured in code…")
      : ai && ai.image_text ? el("div", { class: "content" }, segments(ai.image_text, cur.findings, ai.image_verified || [], "image_text"))
      : el("div", { class: "small muted" }, (ai && ai.note) || "No text found in the image."));
}

async function importPanel(isMgr) {
  const imports = await api("/api/kit/imports");
  const draft = imports.find((i) => i.status === "draft");
  const status = el("div", { class: "small muted" });
  const read = async (file) => {
    const fd = new FormData(); fd.append("file", file, file.name);
    status.replaceChildren(el("span", { class: "spinner" }), "Reading the document and checking every quote… this can take up to 30 seconds.");
    try { await post("/api/kit/import", fd); toast("Candidates ready. Review them below."); route(); }
    catch (e) { status.textContent = ""; toast(e.message, true); }
  };
  const input = el("input", { type: "file", accept: ".pdf,.docx", style: "width:auto" });
  input.addEventListener("change", () => { if (input.files[0]) read(input.files[0]); });
  const sample = el("button", { onclick: async () => {
    try { const r = await fetch("/samples/codecraft-brand-guidelines.pdf"); const blob = await r.blob(); read(new File([blob], "codecraft-brand-guidelines.pdf", { type: "application/pdf" })); }
    catch { toast("Could not load the sample PDF.", true); }
  } }, "Try with our sample guideline PDF");
  const head = el("div", {},
    el("p", {}, "Upload the brand guideline PDF or Word file your organisation already has. The Review Agent proposes rules, glossary terms and approved facts, ",
      "each with the page and the exact sentence it came from. Code keeps a candidate only if that sentence is really in the document, marks items already in the Brand Kit, ",
      "and nothing is added until a Manager selects it."),
    isMgr ? el("div", { class: "row" }, input, el("span", { class: "muted small" }, "or"), sample,
      el("a", { href: "/samples/codecraft-brand-guidelines.pdf", target: "_blank", class: "small" }, "view the sample PDF"))
      : el("div", { class: "intent" }, "Switch to Manager (top right) to import guidelines into the Brand Kit."),
    status);
  if (!draft) {
    const past = imports.filter((i) => i.status !== "draft");
    return el("div", {}, head, past.length ? el("div", {}, el("h3", {}, "Earlier imports"), past.map((i) =>
      el("div", { class: "small", style: "margin:4px 0" }, `${i.filename} · ${i.items.length} candidates · ${i.status}`, i.added ? ` · added ${i.added.join(", ")}` : ""))) : null);
  }
  const KIND = { rule: "Rule", term: "Glossary term", fact: "Approved fact" };
  const rows = draft.items.map((it) => {
    const d = it.data, box = el("input", { type: "checkbox", style: "width:auto" });
    if (!it.duplicate_of) box.checked = true;
    const f = {};
    if (it.kind === "rule") {
      f.title = el("input", { value: d.title }); f.text = el("textarea", { rows: 2 }, d.text);
      f.severity = el("select", {}, ["critical", "major", "minor"].map((v) => el("option", { selected: v === d.severity || null }, v)));
      f.patterns = el("input", { value: (d.patterns || []).join(", "), placeholder: "phrases (optional)" });
    } else if (it.kind === "term") {
      f.preferred = el("input", { value: d.preferred }); f.variants = el("input", { value: d.variants.join(", ") });
    } else {
      f.statement = el("input", { value: d.statement }); f.value = el("input", { value: d.value, type: "number", step: "any" });
      f.unit = el("input", { value: d.unit }); f.valid_until = el("input", { value: d.valid_until, type: "date" });
    }
    const fields = Object.entries(f).map(([k2, i]) => el("div", {}, el("label", {}, { title: "Title", text: "Rule", severity: "Severity", patterns: "Phrases it flags",
      preferred: "Use", variants: "Instead of (comma separated)", statement: "Fact", value: "Number", unit: "Unit", valid_until: "Valid until" }[k2]), i));
    const row = el("div", { class: "importrow" + (it.duplicate_of ? " dup" : "") },
      el("div", { class: "row", style: "justify-content:space-between" },
        el("label", { class: "row", style: "margin:0;color:var(--ink);font-size:13px" }, box, el("b", {}, KIND[it.kind])),
        el("span", { class: "small muted" }, `${it.page}`)),
      el("div", { class: "small", style: "margin:4px 0" }, "Source: ", el("span", { class: "quote" }, `“${it.quote}”`)),
      it.duplicate_of ? el("div", { class: "small c-major" }, `Already in the Brand Kit as ${it.duplicate_of}: not selected.`) : null,
      it.expiry_guessed ? el("div", { class: "small c-major" }, "The document gives no end date: check “Valid until”.") : null,
      el("div", { class: "grid2" }, fields));
    row._pick = () => box.checked ? { index: it.index, data: Object.fromEntries(Object.entries(f).map(([k2, i]) =>
      [k2, ["patterns", "variants"].includes(k2) ? i.value.split(",").map((x) => x.trim()).filter(Boolean) : i.value])) } : null;
    return row;
  });
  return el("div", {}, head,
    el("h3", {}, `Candidates from ${draft.filename} (${draft.pages} page${draft.pages > 1 ? "s" : ""})`),
    el("div", { class: "small muted" }, draft.source === "ai" ? `Read by the Review Agent · API cost $${(draft.cost_usd || 0).toFixed(4)}` : "Read in code", draft.note ? ` · ${draft.note}` : ""),
    draft.items.length ? rows : el("div", { class: "empty" }, "No rules, terms or facts were found."),
    draft.rejected.length ? el("details", {}, el("summary", {}, `${draft.rejected.length} candidate(s) dropped by the quote check`),
      el("ul", { class: "small" }, draft.rejected.map((r) => el("li", {}, `${r.kind || "item"} · ${r.page || "?"} · “${r.quote}” — ${r.reason}`)))) : null,
    isMgr ? el("div", { class: "actions" },
      el("button", { class: "good", onclick: async () => {
        const items = rows.map((r) => r._pick()).filter(Boolean);
        try { const res = await post(`/api/kit/imports/${draft.id}/apply`, { items }); KIT = null; toast(`Added ${res.added.length} item(s) as Brand Kit v${res.kit.version}. Drafts were re-checked.`); route(); }
        catch (e) { toast(e.message, true); }
      } }, "Add selected to the Brand Kit"),
      el("button", { class: "danger", onclick: async () => { try { await post(`/api/kit/imports/${draft.id}/discard`); toast("Import discarded."); route(); } catch (e) { toast(e.message, true); } } }, "Discard")) : null);
}

// ---------------- brand kit
let kitTab = "rules";
async function kitView() {
  const k = await kit(true);
  const isMgr = ROLE === "manager", today = new Date().toISOString().slice(0, 10);
  const del = (kind, id) => isMgr ? el("button", { class: "danger small", onclick: async () => {
    if (!confirm(`Delete ${id}?`)) return; try { await api(`/api/kit/${kind}/${id}`, { method: "DELETE" }); toast("Deleted."); route(); } catch (e) { toast(e.message, true); } } }, "Delete") : null;
  const tabs = el("div", { class: "tabs" }, [["rules", `Rules (${k.rules.length})`], ["facts", `Approved facts (${k.facts.length})`], ["glossary", `Glossary (${k.glossary.length})`], ["voice", "Voice & colours"], ["import", "⇪ Import from guidelines"]]
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
  } else if (kitTab === "import") {
    body = await importPanel(isMgr);
  } else {
    body = el("div", {}, el("p", {}, el("b", {}, "Voice: "), k.brand.voice), el("p", { class: "muted" }, k.brand.description),
      k.brand.palette.map((c) => el("div", { style: "margin:6px 0" }, el("span", { class: "swatch", style: `background:${c.hex}` }), `${c.name} · ${c.hex}`)));
  }
  let form = null;
  if (isMgr && kitTab !== "voice" && kitTab !== "import") {
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
  await kit(true);
  const data = await api("/api/clarity");
  const STATUS_PILL = { unclear: ["Possibly unclear", "st-changes_requested"], clear: ["Clear", "st-approved"], needs_data: ["Needs more data", "st-draft"], clarified: ["Rewritten: building a new score", "st-in_review"] };
  const card = (r) => {
    const [label, cls] = STATUS_PILL[r.status];
    const color = r.score === null ? "var(--muted)" : r.score < 60 ? "var(--bad)" : r.score < 80 ? "var(--major)" : "var(--good)";
    return el("div", { class: "panel clarity" + (r.status === "unclear" ? " unclear" : "") },
      el("div", { class: "row", style: "justify-content:space-between" },
        el("div", {}, el("span", { class: "rid", title: ruleInfo(r.rule_id) }, r.rule_id), " ", el("b", {}, r.title)),
        el("span", { class: `pill ${cls}` }, label)),
      r.text ? el("div", { class: "small muted", style: "margin:4px 0 8px" }, "Rule: ", r.text) : null,
      el("div", { class: "row" },
        el("div", { class: "score", style: `color:${color}` }, r.score === null ? "–" : r.score),
        el("div", { style: "flex:1;min-width:180px" },
          el("div", { class: "bar" }, el("span", { style: `width:${r.score || 0}%;background:${color}` })),
          el("div", { class: "small muted" }, `Clarity score: ${r.accepted} of ${r.decisions} findings accepted · ${r.dismissed} dismissed`,
            r.clarified ? ` · rewritten ${when(r.clarified)} (${r.decisions_before_rewrite} earlier decisions kept in history)` : ""))),
      r.themes.length || r.dismissed_phrases.length ? el("div", { class: "small", style: "margin-top:8px" },
        r.dismissed_phrases.length ? el("div", {}, el("b", {}, "Most dismissed: "), r.dismissed_phrases.map((p) => el("span", { class: "chip" }, `“${p.phrase}” ×${p.count}`))) : null,
        r.themes.length ? el("div", { style: "margin-top:4px" }, el("b", {}, "Words in dismissal reasons: "), r.themes.map((t) => el("span", { class: "chip" }, `${t.word} ×${t.count}`))) : null) : null,
      r.examples.length ? el("details", {}, el("summary", {}, `Why people dismissed it (${r.examples.length} recent)`),
        el("ul", { class: "small" }, r.examples.map((e) => el("li", {}, el("span", { class: "quote" }, e.quote), " — ", e.reason || "no reason")))) : null,
      r.is_rule && r.status === "unclear" ? el("div", { class: "actions" },
        ROLE === "manager"
          ? el("button", { class: "primary", onclick: async () => {
              try { const d = r.draft_id ? await api(`/api/clarity/proposals/${r.draft_id}`) : await post(`/api/clarity/${r.rule_id}/draft`); showDraft(d); } catch (e) { toast(e.message, true); }
            } }, r.draft_id ? "Open the draft" : "Draft a clearer rule")
          : el("span", { class: "small muted" }, "Switch to Manager (top right) to draft a clearer version of this rule.")) : null);
  };
  const flagged = data.scores.filter((r) => r.status === "unclear").length;
  return el("div", {},
    el("h1", {}, "Guideline Clarity Score"),
    el("p", { class: "muted" }, "Which brand rules people keep overriding. The score is the share of a rule's findings that people accepted. ",
      `A rule dismissed in ${Math.round(data.unclear_rate * 100)}% or more of at least ${data.min_decisions} decisions is flagged as possibly unclear. `,
      "A Manager can have the Review Agent draft a clearer version; code replays the past decisions to show its effect before it enters the Brand Kit."),
    el("div", { class: "intent" }, flagged ? `⚠ ${flagged} rule(s) look unclear. ` : "No rule looks unclear right now. ",
      "Scores include example decisions from the demo seed (fictional review history) plus every accept and dismiss you make."),
    data.scores.length ? el("div", { class: "clarity-list" }, data.scores.map(card)) : el("div", { class: "panel empty" }, "No decisions yet. Accept or dismiss findings on a post and the scores appear here."));
}

function showDraft(d) {
  const b = d.before, a = d.after, ev = d.evidence, done = d.status !== "draft";
  const f = { title: el("input", { value: a.title }), text: el("textarea", { rows: 3 }, a.text), good: el("input", { value: a.good || "" }),
    bad: el("input", { value: a.bad || "" }), patterns: el("input", { value: (a.patterns || []).join(", ") }) };
  if (done) Object.values(f).forEach((i) => i.setAttribute("disabled", ""));
  const chips = [...new Set([...(b.patterns || []), ...(a.patterns || [])])].map((p) => {
    const was = (b.patterns || []).includes(p), now = (a.patterns || []).includes(p);
    return el("span", { class: "chip " + (was && !now ? "removed" : !was && now ? "added" : "") }, p);
  });
  modal(
    el("h2", {}, `Clearer version of ${d.rule_id}`, " ", el("span", { class: "ai-tag" }, d.source === "ai" ? "Drafted by the Review Agent" : "Drafted in code")),
    d.note ? el("p", { class: "small muted" }, d.note) : null,
    el("h3", {}, "Rule text: before → after"), diffWords(b.text || "", a.text || ""),
    el("h3", {}, "Phrases it flags"), (b.patterns || []).length || (a.patterns || []).length ? el("div", {}, chips, el("div", { class: "small muted" }, "Red = removed, green = added.")) : el("div", { class: "small muted" }, "This rule has no phrase list."),
    el("h3", {}, "Evidence from past decisions (checked in code)"),
    ev.replayed ? el("div", { class: "verified" },
      `✔ ${ev.dismissed_no_longer_flagged} of ${ev.dismissed_total} dismissed cases would no longer be flagged. `,
      `${ev.accepted_still_caught} of ${ev.accepted_total} accepted cases are still caught.`,
      ev.accepted_lost.length ? el("div", { style: "color:var(--major)" }, "⚠ No longer caught: ", ev.accepted_lost.map((q) => `“${q}”`).join(", ")) : null)
      : el("div", { class: "small muted" }, ev.note),
    d.after.rationale ? el("p", { class: "small" }, el("b", {}, "Why: "), d.after.rationale) : null,
    el("h3", {}, done ? `This draft was ${d.status}.` : "Edit before applying (optional)"),
    el("label", {}, "Title"), f.title, el("label", {}, "Rule text"), f.text,
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "Good example"), f.good), el("div", {}, el("label", {}, "Bad example"), f.bad)),
    el("label", {}, "Phrases (comma separated)"), f.patterns,
    el("div", { class: "actions" },
      done ? null : el("button", { class: "good", onclick: async () => {
        try {
          await post(`/api/clarity/proposals/${d.id}/apply`, { title: f.title.value, text: f.text.value, good: f.good.value, bad: f.bad.value,
            patterns: f.patterns.value.split(",").map((x) => x.trim()).filter(Boolean) });
          closeModal(); KIT = null; toast("Rule updated: new Brand Kit version. Open drafts were re-checked."); route();
        } catch (e) { toast(e.message, true); }
      } }, "Apply to Brand Kit"),
      done ? null : el("button", { class: "danger", onclick: async () => { try { await post(`/api/clarity/proposals/${d.id}/discard`); closeModal(); toast("Draft discarded."); route(); } catch (e) { toast(e.message, true); } } }, "Discard draft"),
      el("button", { onclick: closeModal }, "Close")));
}

async function activityView() {
  const rows = await api("/api/audit");
  return el("div", {}, el("h1", {}, "Activity"), el("p", { class: "muted" }, "Every action by every role, newest first."),
    el("div", { class: "panel" }, el("table", {}, el("tr", {}, el("th", {}, "When"), el("th", {}, "Role"), el("th", {}, "Action"), el("th", {}, "Content"), el("th", {}, "Detail")),
      rows.map((a) => el("tr", {}, el("td", { class: "small" }, when(a.ts)), el("td", {}, cap(a.role)), el("td", {}, a.action),
        el("td", {}, a.submission_id ? el("a", { href: `#/s/${a.submission_id}` }, a.title || `#${a.submission_id}`) : "—"), el("td", { class: "small" }, a.detail))))));
}

function howView() {
  const step = (title, ...text) => el("li", {}, el("b", {}, title + " "), ...text);
  return el("div", { class: "panel how", style: "max-width:900px;margin:auto" },
    el("h1", {}, "How BrandGuard works"),
    el("p", { class: "muted" }, "Content is checked against the organisation's Brand Kit before it is published. Every issue names the rule or fact it breaks, comes with a fix, and a person decides."),
    el("ol", {},
      step("Brand Kit.", "Rules with IDs and examples, a glossary, approved facts (with source, owner and expiry date) and brand colours. Managers edit it, or ",
        el("a", { href: "#/kit", onclick: () => { kitTab = "import"; } }, "import an existing guideline PDF or Word file"), ": the agent proposes items with the page and sentence they came from, code checks that sentence is really in the document, and a Manager accepts each item."),
      step("Exact checks in code.", "Glossary terms, banned phrases (hype, pressure, guarantees, superlatives), readability, the call to action, and every number compared with approved facts: supported, contradicted, outdated (expired fact) or no source."),
      step("Images.", "Main colours are compared with the brand palette and alt text is checked in code. The AI reads the text in the image and where it is; code measures each text's contrast against WCAG (4.5:1, or 3:1 for large text), and the text goes through the same rule and fact checks."),
      step("AI Review Agent.", "Claude (Haiku 5.5) checks tone, messaging and claims in context with Brand Kit tools and writes a suggested version. A policy guard keeps only findings that cite a rule a tool returned and quote the text exactly; AI findings can never block approval on their own, and the suggested version must keep the call to action and brand name and add no unapproved number."),
      step("People decide.", "Creators accept, edit or dismiss each fix (dismissals need a reason). Reviewers approve; open critical issues need a Manager's sign-off. The agent has no approve or publish action."),
      step("Guidelines improve.", "Every accept and dismiss feeds the ", el("a", { href: "#/insights" }, "Guideline Clarity Score"),
        ". Rules people keep overriding are flagged; the agent drafts a clearer rule, code replays past decisions to show its effect, and a Manager applies it as a new Brand Kit version."),
      step("Traceable and safe.", "Every change is a version with a compare view, every action is in the audit log, approved content that relied on a changed fact gets a provenance alert, and AI cost is capped with a spend meter.")),
    el("h3", {}, "Good to know"),
    el("ul", { class: "small" },
      el("li", {}, "This browser has its own private demo workspace. “Reset demo data” in the footer restores the samples."),
      el("li", {}, "If the AI budget or the hourly limit is reached, BrandGuard keeps working in rules-only mode and says so on screen."),
      el("li", {}, "The demo brand “CodeCraft Academy”, its facts, sample posts, poster, guideline PDF and example review history are fictional.")),
    el("p", { class: "muted small" }, "Built by team Cite or Flag for CodeZen India 2026."));
}

// ------------------------------------------------------------------ router
async function route() {
  clearTimeout(pollTimer);
  const h = location.hash || "#/";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === h || (h.startsWith("#/s/") && a.dataset.nav === "inbox")));
  const app = $("#app");
  try {
    if (!STATUS || Date.now() - statusAt > 20000) await loadStatus();
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
