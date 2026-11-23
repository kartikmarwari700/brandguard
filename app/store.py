"""Persistence and review workflow (SQLite, standard library only).

Roles are chosen in the UI (creator / reviewer / manager) so judges can use the
demo without signing in; every action is still checked against the role and
written to the audit log.

Every browser gets its own demo workspace (an id sent in the X-Workspace header),
so one visitor's edits, approvals or resets never change what another visitor sees.
"""
import contextvars
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import agent, engine

SEED = Path(__file__).with_name("seed") / "codecraft.json"
ROLES = ("creator", "reviewer", "manager")
SCHEMA_VERSION = 3
AI_STUCK_AFTER = timedelta(seconds=150)

_lock = threading.RLock()
_db = None
_ws = contextvars.ContextVar("workspace", default="public")
_seeded = set()

SCHEMA = """
CREATE TABLE IF NOT EXISTS kit (ws TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT, title TEXT, channel TEXT, intent TEXT,
  status TEXT, created_at TEXT, updated_at TEXT, kind TEXT DEFAULT 'text');
CREATE INDEX IF NOT EXISTS submissions_ws ON submissions (ws);
CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, n INTEGER, content TEXT,
  author TEXT, note TEXT, created_at TEXT, kit_version INTEGER, findings TEXT, verified TEXT, ai TEXT, ai_status TEXT,
  media TEXT);
CREATE INDEX IF NOT EXISTS versions_sub ON versions (submission_id);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, version_n INTEGER,
  rule_id TEXT, quote TEXT, action TEXT, reason TEXT, role TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, role TEXT, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT, ts TEXT, role TEXT, action TEXT, submission_id INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT, kind TEXT, status TEXT, created_at TEXT,
  data TEXT);
"""


class WorkflowError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path="brandguard.db"):
    global _db
    _db = sqlite3.connect(path, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    if _db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        # Demo data only: an older local database is rebuilt from the seed.
        for table in ("kit", "submissions", "versions", "decisions", "comments", "audit", "proposals"):
            _db.execute(f"DROP TABLE IF EXISTS {table}")
        _db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _db.executescript(SCHEMA)
    _seeded.clear()
    return _db


def q(sql, args=(), one=False):
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


# ---------------------------------------------------------------- workspaces

def use_workspace(ws):
    return _ws.set(ws)


def reset_workspace(token):
    _ws.reset(token)


def ws():
    return _ws.get()


def ensure_workspace():
    """Seed the current workspace with the demo Brand Kit and samples on first use."""
    w = ws()
    if w in _seeded:
        return
    with _lock:
        if w in _seeded:
            return
        if not q("SELECT 1 FROM kit WHERE ws = ?", (w,), one=True):
            seed(background=True)
        _seeded.add(w)


def audit(role, action, submission_id=None, detail=""):
    q("INSERT INTO audit (ws, ts, role, action, submission_id, detail) VALUES (?,?,?,?,?,?)",
      (ws(), now(), role, action, submission_id, detail))


# ---------------------------------------------------------------- brand kit

def get_kit():
    row = q("SELECT data, version FROM kit WHERE ws = ?", (ws(),), one=True)
    if not row:
        raise WorkflowError("This demo workspace is not ready. Refresh the page.", 503)
    kit = json.loads(row["data"])
    kit["version"] = row["version"]
    return kit


def save_kit(kit, role, action, detail):
    data = {k: v for k, v in kit.items() if k != "version"}
    q("UPDATE kit SET data = ?, version = version + 1 WHERE ws = ?", (json.dumps(data), ws()))
    audit(role, action, None, detail)
    recheck_open_content(detail)
    return get_kit()


def recheck_open_content(reason):
    """Claim provenance: when the Brand Kit changes, every unapproved draft is checked again automatically."""
    version = get_kit()["version"]
    for sub in q("SELECT id FROM submissions WHERE ws = ? AND status != 'approved'", (ws(),)):
        cur = _current(sub["id"])
        if cur["kit_version"] < version:
            create_version(sub["id"], cur["content"], "system", f"Re-checked automatically: Brand Kit v{version} ({reason})",
                           media=_media(cur))
            audit("system", "submission.recheck", sub["id"], f"Brand Kit v{version}: {reason}")


def recheck(submission_id, role):
    _require(role, ("creator", "reviewer", "manager"), "re-check content")
    _editable(submission_id)
    cur = _current(submission_id)
    audit(role, "submission.recheck", submission_id, "Re-checked against the latest Brand Kit and today's date")
    return create_version(submission_id, cur["content"], role, "Re-checked against the latest Brand Kit", media=_media(cur))


def update_fact(fact_id, patch, role):
    if role != "manager":
        raise WorkflowError("Only a manager can change the Brand Kit.", 403)
    kit = get_kit()
    fact = next((f for f in kit["facts"] if f["id"] == fact_id), None)
    if not fact:
        raise WorkflowError("Fact not found.", 404)
    before = f"{fact['value']} {fact['unit']}, valid until {fact['valid_until']}"
    for key in ("statement", "value", "valid_until", "source", "owner"):
        if patch.get(key) not in (None, ""):
            fact[key] = patch[key]
    if isinstance(fact["value"], float) and fact["value"].is_integer():
        fact["value"] = int(fact["value"])
    after = f"{fact['value']} {fact['unit']}, valid until {fact['valid_until']}"
    if before == after and not any(patch.get(k) for k in ("statement", "source", "owner")):
        raise WorkflowError("Nothing changed.")
    return save_kit(kit, role, "kit.update_facts", f"{fact_id} changed: {before} → {after}")


def provenance_alerts(verified, kit):
    """Verified claims whose approved fact has since changed, expired or been removed."""
    today = datetime.now(timezone.utc).date().isoformat()
    facts = {f["id"]: f for f in kit["facts"]}
    alerts = []
    for v in verified:
        fact = facts.get(v["fact_id"])
        if fact is None:
            alerts.append(f"“{v['quote']}” relied on {v['fact_id']}, which was removed from the Brand Kit.")
        elif fact["valid_until"] < today:
            alerts.append(f"“{v['quote']}” relied on {v['fact_id']}, which expired on {fact['valid_until']}.")
        elif "value" in v and float(fact["value"]) != float(v["value"]):
            alerts.append(f"“{v['quote']}” relied on {v['fact_id']}, which changed from {v['value']} to {fact['value']}.")
    return alerts


def fact_usage():
    """Which current content relies on each approved fact (verified claims and fact-based findings)."""
    usage = {}
    for sub in q("SELECT id, title, status FROM submissions WHERE ws = ? AND COALESCE(kind, 'text') != 'history'", (ws(),)):
        cur = _current(sub["id"])
        cited = {v["fact_id"] for v in json.loads(cur["verified"])}
        cited |= {c for f in json.loads(cur["findings"]) for c in f["cited"] if c.startswith("FACT")}
        for fid in cited:
            usage.setdefault(fid, []).append({"id": sub["id"], "title": sub["title"], "status": sub["status"]})
    return usage


def next_id(kit, kind, item):
    prefix = {"rules": None, "facts": "FACT", "glossary": "TERM"}[kind]
    if kind == "rules":
        prefix = item.get("category", "custom").upper()[:5]
    existing = {i["id"] for i in kit[kind]}
    n = 1
    while f"{prefix}-{n:02d}" in existing:
        n += 1
    return f"{prefix}-{n:02d}"


def clean_item(kind, item):
    if kind == "facts":
        item["value"] = float(item["value"]) if "." in str(item["value"]) else int(item["value"])
        item["keywords"] = [k.strip() for k in item.get("keywords", []) if k.strip()]
    if kind == "glossary":
        item["variants"] = [v.strip() for v in item.get("variants", []) if v.strip()]
    if kind == "rules":
        item["patterns"] = [p.strip() for p in item.get("patterns", []) if p.strip()]
    return item


def add_kit_item(kind, item, role):
    if role != "manager":
        raise WorkflowError("Only a manager can change the Brand Kit.", 403)
    kit = get_kit()
    item = clean_item(kind, {**item, "id": next_id(kit, kind, item)})
    kit[kind].append(item)
    return save_kit(kit, role, f"kit.add_{kind}", f"{item['id']}: {item.get('title') or item.get('statement') or item.get('preferred')}")


def delete_kit_item(kind, item_id, role):
    if role != "manager":
        raise WorkflowError("Only a manager can change the Brand Kit.", 403)
    kit = get_kit()
    if not any(i["id"] == item_id for i in kit[kind]):
        raise WorkflowError("That item is already gone. Refresh the page.", 404)
    kit[kind] = [i for i in kit[kind] if i["id"] != item_id]
    return save_kit(kit, role, f"kit.delete_{kind}", item_id)


# ---------------------------------------------------------------- reviews

def _dismissed(submission_id):
    rows = q("SELECT rule_id, quote, reason FROM decisions WHERE submission_id = ? AND action = 'rejected'", (submission_id,))
    return {(r["rule_id"], r["quote"]): r["reason"] for r in rows}


def _media(version_row):
    return json.loads(version_row["media"]) if version_row["media"] else None


def _offline(content, intent, kit, findings, note):
    return {"mode": "error", "status": "done", "note": note, "findings": [], "rewrite": engine.apply_fixes(content, findings),
            "rewrite_check": None, "tool_log": [], "rejected": [], "cost_usd": 0.0}


def _run_ai(version_id, text, intent, kit, findings, verified, media=None):
    try:
        if media:
            from . import images
            result = images.review_image(media, intent, kit, text, findings)
        else:
            result = agent.review_with_ai(text, intent, kit, findings, verified)
        status = "done"
    except Exception as exc:  # never leave a review stuck in "running"
        result = _offline(text, intent, kit, findings, f"AI review failed ({type(exc).__name__}); rule checks only.")
        status = "error"
    q("UPDATE versions SET ai = ?, ai_status = ? WHERE id = ?", (json.dumps(result), status, version_id))


def create_version(submission_id, content, author, note, background=True, media=None):
    sub = q("SELECT * FROM submissions WHERE id = ?", (submission_id,), one=True)
    intent = json.loads(sub["intent"])
    kit = get_kit()
    findings, verified = engine.run_rule_checks(content, kit, intent) if content.strip() or not media else ([], [])
    if media:
        from . import images
        findings = findings + images.check_image(media, kit)
    n = (q("SELECT MAX(n) AS m FROM versions WHERE submission_id = ?", (submission_id,), one=True)["m"] or 0) + 1
    ai_status = "running" if agent.ai_available() else "done"
    ai = None
    if not agent.ai_available():
        if media:
            from . import images
            ai = images.review_image(media, intent, kit, content, findings)
        else:
            ai = agent.review_with_ai(content, intent, kit, findings, verified)
    with _lock:
        cur = _db.execute(
            "INSERT INTO versions (submission_id, n, content, author, note, created_at, kit_version, findings, verified, ai,"
            " ai_status, media) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (submission_id, n, content, author, note, now(), kit["version"], json.dumps(findings), json.dumps(verified),
             json.dumps(ai) if ai else None, ai_status, json.dumps(media) if media else None))
        _db.commit()
        version_id = cur.lastrowid
    q("UPDATE submissions SET updated_at = ? WHERE id = ?", (now(), submission_id))
    if ai_status == "running":
        args = (version_id, content, intent, kit, findings, verified, media)
        if background:
            threading.Thread(target=_run_ai, args=args, daemon=True).start()
        else:
            _run_ai(*args)
    return n


def create_submission(title, channel, content, intent, role, background=True, media=None):
    if role != "creator":
        raise WorkflowError("Switch to the Creator role to submit new content.", 403)
    if not content.strip() and not media:
        raise WorkflowError("Content is empty. Paste some text or upload a file.")
    with _lock:
        cur = _db.execute("INSERT INTO submissions (ws, title, channel, intent, status, created_at, updated_at, kind)"
                          " VALUES (?,?,?,?,?,?,?,?)",
                          (ws(), title.strip() or "Untitled", channel, json.dumps(intent), "draft", now(), now(),
                           "image" if media else "text"))
        _db.commit()
        sid = cur.lastrowid
    audit(role, "submission.create", sid, title)
    create_version(sid, content.strip(), role, "Original draft", background, media=media)
    return sid


def _current(submission_id):
    row = q("SELECT v.* FROM versions v JOIN submissions s ON s.id = v.submission_id "
            "WHERE v.submission_id = ? AND s.ws = ? ORDER BY v.n DESC LIMIT 1", (submission_id, ws()), one=True)
    if not row:
        raise WorkflowError("Content not found. It may have been reset; go back to the inbox.", 404)
    return row


def _unstick(cur):
    """A review that has been 'running' for too long (for example after a network stall) is closed in rules-only mode."""
    if cur["ai_status"] != "running":
        return cur
    started = datetime.fromisoformat(cur["created_at"])
    if datetime.now(timezone.utc) - started < AI_STUCK_AFTER:
        return cur
    findings = json.loads(cur["findings"])
    result = _offline(cur["content"], {}, None, findings, "AI review took too long and was skipped; rule checks only.")
    q("UPDATE versions SET ai = ?, ai_status = 'error' WHERE id = ? AND ai_status = 'running'", (json.dumps(result), cur["id"]))
    return q("SELECT * FROM versions WHERE id = ?", (cur["id"],), one=True)


def _all_findings(version_row, dismissed):
    ai = json.loads(version_row["ai"]) if version_row["ai"] else None
    items = json.loads(version_row["findings"]) + ((ai or {}).get("findings") or [])
    for f in items:
        reason = dismissed.get((f["rule_id"], f["quote"]))
        f["status"] = "dismissed" if reason is not None else "open"
        f["dismiss_reason"] = reason
    items.sort(key=lambda f: (f["status"] != "open", engine.SEVERITY_ORDER[f["severity"]], f.get("target", ""), f["start"]))
    return items, ai


def approval_state(findings, ai_status):
    open_ = [f for f in findings if f["status"] == "open"]
    counts = {s: sum(1 for f in open_ if f["severity"] == s) for s in ("critical", "major", "minor")}
    return {
        "open_counts": counts,
        "needs_manager": counts["critical"] > 0,
        "reviewer_can_approve": counts["critical"] == 0,
        "ai_running": ai_status == "running",
    }


def _sub(submission_id):
    sub = q("SELECT * FROM submissions WHERE id = ? AND ws = ?", (submission_id, ws()), one=True)
    if not sub:
        raise WorkflowError("Content not found. It may have been reset; go back to the inbox.", 404)
    return sub


def get_submission(submission_id):
    sub = _sub(submission_id)
    cur = _unstick(_current(submission_id))
    findings, ai = _all_findings(cur, _dismissed(submission_id))
    versions = q("SELECT n, content, author, note, created_at, media FROM versions WHERE submission_id = ? ORDER BY n",
                 (submission_id,))
    kit = get_kit()
    media = _media(cur)
    return {
        "provenance_alerts": provenance_alerts(json.loads(cur["verified"]), kit),
        "kit_version_now": kit["version"],
        "id": sub["id"], "title": sub["title"], "channel": sub["channel"], "intent": json.loads(sub["intent"]),
        "kind": sub["kind"] or "text",
        "status": sub["status"], "created_at": sub["created_at"], "updated_at": sub["updated_at"],
        "current": {"n": cur["n"], "content": cur["content"], "author": cur["author"], "kit_version": cur["kit_version"],
                    "findings": findings, "verified": json.loads(cur["verified"]), "ai": ai, "ai_status": cur["ai_status"],
                    "media": media},
        "approval": approval_state(findings, cur["ai_status"]),
        "versions": [{**{k: v[k] for k in ("n", "content", "author", "note", "created_at")},
                      "alt": (json.loads(v["media"]) or {}).get("alt") if v["media"] else None} for v in versions],
        "decisions": [dict(d) for d in q("SELECT version_n, rule_id, quote, action, reason, role, created_at FROM decisions "
                                         "WHERE submission_id = ? ORDER BY id", (submission_id,))],
        "comments": [dict(c) for c in q("SELECT role, text, created_at FROM comments WHERE submission_id = ? ORDER BY id", (submission_id,))],
        "audit": [dict(a) for a in q("SELECT ts, role, action, detail FROM audit WHERE submission_id = ? ORDER BY id", (submission_id,))],
    }


def list_submissions():
    out = []
    kit = get_kit()
    for sub in q("SELECT * FROM submissions WHERE ws = ? AND COALESCE(kind, 'text') != 'history' "
                  "ORDER BY updated_at DESC, id DESC", (ws(),)):
        cur = _unstick(_current(sub["id"]))
        findings, _ = _all_findings(cur, _dismissed(sub["id"]))
        state = approval_state(findings, cur["ai_status"])
        media = _media(cur)
        out.append({"id": sub["id"], "title": sub["title"], "channel": sub["channel"], "status": sub["status"],
                    "kind": sub["kind"] or "text", "version": cur["n"], "updated_at": sub["updated_at"],
                    "open_counts": state["open_counts"], "verified": len(json.loads(cur["verified"])),
                    "ai_status": cur["ai_status"], "alerts": len(provenance_alerts(json.loads(cur["verified"]), kit)),
                    "preview": (cur["content"] or (media or {}).get("alt") or "Image")[:140],
                    "thumb": (media or {}).get("thumb")})
    return out


def _find(submission_id, finding_id):
    cur = _current(submission_id)
    findings, _ = _all_findings(cur, _dismissed(submission_id))
    match = next((f for f in findings if f["id"] == finding_id), None)
    if not match:
        raise WorkflowError("That finding belongs to an older version. The page has been refreshed.", 409)
    return cur, match


def _require(role, allowed, what):
    if role not in allowed:
        names = " or ".join(a.capitalize() for a in allowed)
        raise WorkflowError(f"Only the {names} can {what}. Switch role at the top right.", 403)


def _editable(submission_id):
    if _sub(submission_id)["status"] == "approved":
        raise WorkflowError("This content is already approved. Create new content to change it.")


def _ai_ready(cur):
    if cur["ai_status"] == "running":
        raise WorkflowError("The AI review of this version is still running. Wait a few seconds and try again.", 409)


def accept_fix(submission_id, finding_id, role):
    _require(role, ("creator",), "accept fixes")
    _editable(submission_id)
    cur, f = _find(submission_id, finding_id)
    if f["status"] == "dismissed":
        raise WorkflowError("This finding was dismissed.", 409)
    if f.get("fix") is None:
        raise WorkflowError("This finding has no automatic fix. Use Edit to change the text yourself.")
    media = _media(cur)
    content = cur["content"]
    if f.get("target") == "alt":
        media = {**media, "alt": f["fix"]}
    elif f.get("target") == "image":
        raise WorkflowError("Image issues are fixed by uploading a new version of the image.")
    else:
        content = engine.tidy(cur["content"][:f["start"]] + f["fix"] + cur["content"][f["end"]:])
    q("INSERT INTO decisions (submission_id, version_n, rule_id, quote, action, reason, role, created_at) VALUES (?,?,?,?,?,?,?,?)",
      (submission_id, cur["n"], f["rule_id"], f["quote"], "accepted", None, role, now()))
    q("UPDATE submissions SET status = 'draft' WHERE id = ?", (submission_id,))
    audit(role, "finding.accept", submission_id, f"{f['rule_id']}: “{f['quote']}” → “{f['fix']}”")
    return create_version(submission_id, content, role, f"Accepted fix for {f['rule_id']}", media=media)


def apply_rewrite(submission_id, role):
    _require(role, ("creator",), "accept the suggested version")
    _editable(submission_id)
    cur = _current(submission_id)
    _ai_ready(cur)
    ai = json.loads(cur["ai"]) if cur["ai"] else None
    if not ai or not ai.get("rewrite"):
        raise WorkflowError("There is no suggested version for this content.")
    if ai["rewrite"].strip() == cur["content"].strip():
        raise WorkflowError("The suggested version is the same as the current text.")
    q("UPDATE submissions SET status = 'draft' WHERE id = ?", (submission_id,))
    audit(role, "rewrite.accept", submission_id, f"Accepted suggested version of v{cur['n']}")
    return create_version(submission_id, ai["rewrite"], role, "Accepted the suggested version", media=_media(cur))


def edit_content(submission_id, content, role, media=None, alt=None):
    _require(role, ("creator",), "edit the content")
    _editable(submission_id)
    cur = _current(submission_id)
    old_media = _media(cur)
    if old_media is not None:
        media = media or dict(old_media)
        if alt is not None:
            media = {**media, "alt": alt.strip()}
    elif not content.strip():
        raise WorkflowError("Content is empty.")
    if content.strip() == (cur["content"] or "").strip() and media == old_media:
        raise WorkflowError("Nothing changed.")
    q("UPDATE submissions SET status = 'draft' WHERE id = ?", (submission_id,))
    audit(role, "content.edit", submission_id, "Uploaded a new image" if media and media is not old_media and
          media.get("sha") != (old_media or {}).get("sha") else "Edited by hand")
    return create_version(submission_id, content.strip(), role, "Edited by hand", media=media)


def dismiss(submission_id, finding_id, reason, role):
    _require(role, ("creator", "reviewer", "manager"), "dismiss findings")
    _editable(submission_id)
    if not reason or len(reason.strip()) < 3:
        raise WorkflowError("Please give a short reason. Reasons help improve unclear brand rules.")
    cur, f = _find(submission_id, finding_id)
    if f["status"] == "dismissed":
        raise WorkflowError("This finding is already dismissed.", 409)
    if f["severity"] == "critical" and role != "manager":
        raise WorkflowError("Critical findings can only be dismissed by a Manager.", 403)
    q("INSERT INTO decisions (submission_id, version_n, rule_id, quote, action, reason, role, created_at) VALUES (?,?,?,?,?,?,?,?)",
      (submission_id, cur["n"], f["rule_id"], f["quote"], "rejected", reason.strip(), role, now()))
    audit(role, "finding.dismiss", submission_id, f"{f['rule_id']}: “{f['quote']}” — {reason.strip()}")


def submit_for_review(submission_id, role):
    _require(role, ("creator",), "submit content for review")
    _editable(submission_id)
    if _sub(submission_id)["status"] == "in_review":
        raise WorkflowError("Already sent for review.", 409)
    q("UPDATE submissions SET status = 'in_review', updated_at = ? WHERE id = ?", (now(), submission_id))
    audit(role, "submission.submit", submission_id, "Sent for review")


def comment(submission_id, text, role):
    _sub(submission_id)
    if not text.strip():
        raise WorkflowError("Comment is empty.")
    q("INSERT INTO comments (submission_id, role, text, created_at) VALUES (?,?,?,?)", (submission_id, role, text.strip(), now()))
    audit(role, "comment.add", submission_id, text.strip()[:120])


def request_changes(submission_id, text, role):
    _require(role, ("reviewer", "manager"), "request changes")
    _editable(submission_id)
    if not text.strip():
        raise WorkflowError("Tell the creator what to change.")
    comment(submission_id, text, role)
    q("UPDATE submissions SET status = 'changes_requested', updated_at = ? WHERE id = ?", (now(), submission_id))
    audit(role, "submission.request_changes", submission_id, text.strip()[:120])


def approve(submission_id, note, role):
    _require(role, ("reviewer", "manager"), "approve content")
    _editable(submission_id)
    sub = get_submission(submission_id)
    if sub["status"] != "in_review":
        raise WorkflowError("The creator must submit the content for review first.")
    if sub["approval"]["ai_running"]:
        raise WorkflowError("Wait until the AI review of the latest version has finished.")
    if sub["approval"]["needs_manager"] and role != "manager":
        raise WorkflowError("Open critical findings need a Manager's sign-off. Switch to Manager or fix them first.", 403)
    q("UPDATE submissions SET status = 'approved', updated_at = ? WHERE id = ? AND status = 'in_review'", (now(), submission_id))
    detail = f"Approved v{sub['current']['n']}" + (f" — {note.strip()}" if note and note.strip() else "")
    if sub["approval"]["needs_manager"]:
        detail += " (manager sign-off with open critical findings)"
    audit(role, "submission.approve", submission_id, detail)


def global_audit(limit=200):
    rows = q("SELECT a.ts, a.role, a.action, a.detail, a.submission_id, s.title FROM audit a "
             "LEFT JOIN submissions s ON s.id = a.submission_id WHERE a.ws = ? ORDER BY a.id DESC LIMIT ?", (ws(), limit))
    return [dict(r) for r in rows]


def decisions_by_rule():
    """Every accept/dismiss decision in this workspace, grouped by rule (input for the Guideline Clarity Score)."""
    rows = q("SELECT d.rule_id, d.quote, d.action, d.reason, d.created_at, d.submission_id FROM decisions d "
             "JOIN submissions s ON s.id = d.submission_id WHERE s.ws = ? ORDER BY d.id", (ws(),))
    out = {}
    for r in rows:
        out.setdefault(r["rule_id"], []).append(dict(r))
    return out


def rule_insights():
    """Early view of the Guideline Clarity Score data: how often each rule's findings are dismissed."""
    kit = get_kit()
    names = {r["id"]: r["title"] for r in kit["rules"]} | {t["id"]: f"Term: {t['preferred']}" for t in kit["glossary"]}
    stats = {}
    for rule_id, rows in decisions_by_rule().items():
        s = stats.setdefault(rule_id, {"rule_id": rule_id, "title": names.get(rule_id, rule_id), "accepted": 0, "rejected": 0})
        for r in rows:
            s[r["action"]] = s.get(r["action"], 0) + 1
    for s in stats.values():
        total = s["accepted"] + s["rejected"]
        s["dismiss_rate"] = round(s["rejected"] / total, 2) if total else 0
    return sorted(stats.values(), key=lambda s: (-s["dismiss_rate"], -s["rejected"]))


def seed(background=False):
    """Load the demo Brand Kit, samples and example review history into the current workspace (replacing it)."""
    data = json.loads(SEED.read_text(encoding="utf-8"))
    samples = data.pop("samples")
    history = data.pop("history", [])
    w = ws()
    with _lock:
        ids = [r["id"] for r in _db.execute("SELECT id FROM submissions WHERE ws = ?", (w,)).fetchall()]
        for table in ("versions", "decisions", "comments"):
            _db.executemany(f"DELETE FROM {table} WHERE submission_id = ?", [(i,) for i in ids])
        for table in ("submissions", "audit", "kit", "proposals"):
            _db.execute(f"DELETE FROM {table} WHERE ws = ?", (w,))
        _db.execute("INSERT INTO kit (ws, data, version) VALUES (?, ?, 1)", (w, json.dumps(data)))
        _db.commit()
    for s in reversed(samples):
        media = None
        if s.get("image"):
            from . import images
            media = images.load_seed_image(s["image"], s.get("alt", ""))
        create_submission(s["title"], s["channel"], s["content"], s["intent"], "creator", background, media=media)
    _seed_history(history)
    audit("system", "demo.reset", None, "Demo data loaded")


def _seed_history(history):
    """Example review decisions from the demo seed (fictional), so Insights and the Guideline Clarity Score have data."""
    if not history:
        return
    with _lock:
        cur = _db.execute("INSERT INTO submissions (ws, title, channel, intent, status, created_at, updated_at, kind)"
                          " VALUES (?,?,?,?,?,?,?,?)",
                          (ws(), "Earlier reviews (demo history)", "History", json.dumps({}), "approved",
                           (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"),
                           (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"), "history"))
        sid = cur.lastrowid
        _db.executemany(
            "INSERT INTO decisions (submission_id, version_n, rule_id, quote, action, reason, role, created_at) VALUES (?,?,?,?,?,?,?,?)",
            [(sid, 1, h["rule_id"], h["quote"], h["action"], h.get("reason"), h.get("role", "creator"),
              (datetime.now(timezone.utc) - timedelta(days=30 - i % 28)).isoformat(timespec="seconds"))
             for i, h in enumerate(history)])
        _db.commit()
