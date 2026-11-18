"""Persistence and review workflow (SQLite, standard library only).

Roles are chosen in the UI (creator / reviewer / manager) so judges can use the
demo without signing in; every action is still checked against the role and
written to the audit log.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import agent, engine

SEED = Path(__file__).with_name("seed") / "codecraft.json"
ROLES = ("creator", "reviewer", "manager")

_lock = threading.RLock()
_db = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kit (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, channel TEXT, intent TEXT,
  status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, n INTEGER, content TEXT,
  author TEXT, note TEXT, created_at TEXT, kit_version INTEGER, findings TEXT, verified TEXT, ai TEXT, ai_status TEXT);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, version_n INTEGER,
  rule_id TEXT, quote TEXT, action TEXT, reason TEXT, role TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, role TEXT, text TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, action TEXT, submission_id INTEGER, detail TEXT);
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
    _db.executescript(SCHEMA)
    if not _db.execute("SELECT 1 FROM kit").fetchone():
        seed(background=True)
    return _db


def q(sql, args=(), one=False):
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def audit(role, action, submission_id=None, detail=""):
    q("INSERT INTO audit (ts, role, action, submission_id, detail) VALUES (?,?,?,?,?)",
      (now(), role, action, submission_id, detail))


# ---------------------------------------------------------------- brand kit

def get_kit():
    row = q("SELECT data, version FROM kit WHERE id = 1", one=True)
    kit = json.loads(row["data"])
    kit["version"] = row["version"]
    return kit


def save_kit(kit, role, action, detail):
    data = {k: v for k, v in kit.items() if k != "version"}
    q("UPDATE kit SET data = ?, version = version + 1 WHERE id = 1", (json.dumps(data),))
    audit(role, action, None, detail)
    recheck_open_content(detail)
    return get_kit()


def recheck_open_content(reason):
    """Claim provenance: when the Brand Kit changes, every unapproved draft is checked again automatically."""
    version = get_kit()["version"]
    for sub in q("SELECT id FROM submissions WHERE status != 'approved'"):
        cur = _current(sub["id"])
        if cur["kit_version"] < version:
            create_version(sub["id"], cur["content"], "system", f"Re-checked automatically: Brand Kit v{version} ({reason})")
            audit("system", "submission.recheck", sub["id"], f"Brand Kit v{version}: {reason}")


def recheck(submission_id, role):
    _require(role, ("creator", "reviewer", "manager"), "re-check content")
    _editable(submission_id)
    cur = _current(submission_id)
    audit(role, "submission.recheck", submission_id, "Re-checked against the latest Brand Kit and today's date")
    return create_version(submission_id, cur["content"], role, "Re-checked against the latest Brand Kit")


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
    for sub in q("SELECT id, title, status FROM submissions"):
        cur = _current(sub["id"])
        cited = {v["fact_id"] for v in json.loads(cur["verified"])}
        cited |= {c for f in json.loads(cur["findings"]) for c in f["cited"] if c.startswith("FACT")}
        for fid in cited:
            usage.setdefault(fid, []).append({"id": sub["id"], "title": sub["title"], "status": sub["status"]})
    return usage


def add_kit_item(kind, item, role):
    if role != "manager":
        raise WorkflowError("Only a manager can change the Brand Kit.", 403)
    kit = get_kit()
    prefix = {"rules": None, "facts": "FACT", "glossary": "TERM"}[kind]
    if kind == "rules":
        prefix = item.get("category", "custom").upper()[:5]
    existing = [i["id"] for i in kit[kind]]
    n = 1
    while f"{prefix}-{n:02d}" in existing:
        n += 1
    item = {**item, "id": f"{prefix}-{n:02d}"}
    if kind == "facts":
        item["value"] = float(item["value"]) if "." in str(item["value"]) else int(item["value"])
        item["keywords"] = [k.strip() for k in item.get("keywords", []) if k.strip()]
    if kind == "glossary":
        item["variants"] = [v.strip() for v in item.get("variants", []) if v.strip()]
    if kind == "rules":
        item["patterns"] = [p.strip() for p in item.get("patterns", []) if p.strip()]
    kit[kind].append(item)
    return save_kit(kit, role, f"kit.add_{kind}", f"{item['id']}: {item.get('title') or item.get('statement') or item.get('preferred')}")


def delete_kit_item(kind, item_id, role):
    if role != "manager":
        raise WorkflowError("Only a manager can change the Brand Kit.", 403)
    kit = get_kit()
    kit[kind] = [i for i in kit[kind] if i["id"] != item_id]
    return save_kit(kit, role, f"kit.delete_{kind}", item_id)


# ---------------------------------------------------------------- reviews

def _dismissed(submission_id):
    rows = q("SELECT rule_id, quote, reason FROM decisions WHERE submission_id = ? AND action = 'rejected'", (submission_id,))
    return {(r["rule_id"], r["quote"]): r["reason"] for r in rows}


def _run_ai(version_id, text, intent, kit, findings, verified):
    try:
        result = agent.review_with_ai(text, intent, kit, findings, verified)
        status = "done"
    except Exception as exc:  # never leave a review stuck in "running"
        result = {"mode": "error", "status": "done", "note": f"AI review failed: {type(exc).__name__}", "findings": [],
                  "rewrite": engine.apply_fixes(text, findings), "rewrite_check": None, "tool_log": [], "rejected": []}
        status = "error"
    q("UPDATE versions SET ai = ?, ai_status = ? WHERE id = ?", (json.dumps(result), status, version_id))


def create_version(submission_id, content, author, note, background=True):
    sub = q("SELECT * FROM submissions WHERE id = ?", (submission_id,), one=True)
    intent = json.loads(sub["intent"])
    kit = get_kit()
    findings, verified = engine.run_rule_checks(content, kit, intent)
    n = (q("SELECT MAX(n) AS m FROM versions WHERE submission_id = ?", (submission_id,), one=True)["m"] or 0) + 1
    ai_status = "running" if agent.ai_available() else "done"
    ai = None if agent.ai_available() else agent.review_with_ai(content, intent, kit, findings, verified)
    with _lock:
        cur = _db.execute(
            "INSERT INTO versions (submission_id, n, content, author, note, created_at, kit_version, findings, verified, ai, ai_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (submission_id, n, content, author, note, now(), kit["version"], json.dumps(findings), json.dumps(verified),
             json.dumps(ai) if ai else None, ai_status))
        _db.commit()
        version_id = cur.lastrowid
    q("UPDATE submissions SET updated_at = ? WHERE id = ?", (now(), submission_id))
    if ai_status == "running":
        args = (version_id, content, intent, kit, findings, verified)
        if background:
            threading.Thread(target=_run_ai, args=args, daemon=True).start()
        else:
            _run_ai(*args)
    return n


def create_submission(title, channel, content, intent, role, background=True):
    if role != "creator":
        raise WorkflowError("Switch to the Creator role to submit new content.", 403)
    if not content.strip():
        raise WorkflowError("Content is empty.")
    with _lock:
        cur = _db.execute("INSERT INTO submissions (title, channel, intent, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                          (title.strip() or "Untitled", channel, json.dumps(intent), "draft", now(), now()))
        _db.commit()
        sid = cur.lastrowid
    audit(role, "submission.create", sid, title)
    create_version(sid, content.strip(), role, "Original draft", background)
    return sid


def _current(submission_id):
    return q("SELECT * FROM versions WHERE submission_id = ? ORDER BY n DESC LIMIT 1", (submission_id,), one=True)


def _all_findings(version_row, dismissed):
    ai = json.loads(version_row["ai"]) if version_row["ai"] else None
    items = json.loads(version_row["findings"]) + ((ai or {}).get("findings") or [])
    for f in items:
        reason = dismissed.get((f["rule_id"], f["quote"]))
        f["status"] = "dismissed" if reason is not None else "open"
        f["dismiss_reason"] = reason
    items.sort(key=lambda f: (f["status"] != "open", engine.SEVERITY_ORDER[f["severity"]], f["start"]))
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


def get_submission(submission_id):
    sub = q("SELECT * FROM submissions WHERE id = ?", (submission_id,), one=True)
    if not sub:
        raise WorkflowError("Submission not found.", 404)
    cur = _current(submission_id)
    findings, ai = _all_findings(cur, _dismissed(submission_id))
    versions = q("SELECT n, content, author, note, created_at FROM versions WHERE submission_id = ? ORDER BY n", (submission_id,))
    kit = get_kit()
    return {
        "provenance_alerts": provenance_alerts(json.loads(cur["verified"]), kit),
        "kit_version_now": kit["version"],
        "id": sub["id"], "title": sub["title"], "channel": sub["channel"], "intent": json.loads(sub["intent"]),
        "status": sub["status"], "created_at": sub["created_at"], "updated_at": sub["updated_at"],
        "current": {"n": cur["n"], "content": cur["content"], "author": cur["author"], "kit_version": cur["kit_version"],
                    "findings": findings, "verified": json.loads(cur["verified"]), "ai": ai, "ai_status": cur["ai_status"]},
        "approval": approval_state(findings, cur["ai_status"]),
        "versions": [dict(v) for v in versions],
        "decisions": [dict(d) for d in q("SELECT version_n, rule_id, quote, action, reason, role, created_at FROM decisions "
                                         "WHERE submission_id = ? ORDER BY id", (submission_id,))],
        "comments": [dict(c) for c in q("SELECT role, text, created_at FROM comments WHERE submission_id = ? ORDER BY id", (submission_id,))],
        "audit": [dict(a) for a in q("SELECT ts, role, action, detail FROM audit WHERE submission_id = ? ORDER BY id", (submission_id,))],
    }


def list_submissions():
    out = []
    kit = get_kit()
    for sub in q("SELECT * FROM submissions ORDER BY updated_at DESC, id DESC"):
        cur = _current(sub["id"])
        findings, _ = _all_findings(cur, _dismissed(sub["id"]))
        state = approval_state(findings, cur["ai_status"])
        out.append({"id": sub["id"], "title": sub["title"], "channel": sub["channel"], "status": sub["status"],
                    "version": cur["n"], "updated_at": sub["updated_at"], "open_counts": state["open_counts"],
                    "verified": len(json.loads(cur["verified"])), "ai_status": cur["ai_status"],
                    "alerts": len(provenance_alerts(json.loads(cur["verified"]), kit)),
                    "preview": cur["content"][:140]})
    return out


def _find(submission_id, finding_id):
    cur = _current(submission_id)
    findings, _ = _all_findings(cur, _dismissed(submission_id))
    match = next((f for f in findings if f["id"] == finding_id), None)
    if not match:
        raise WorkflowError("That finding belongs to an older version. Refresh the page.", 409)
    return cur, match


def _require(role, allowed, what):
    if role not in allowed:
        names = " or ".join(a.capitalize() for a in allowed)
        raise WorkflowError(f"Only the {names} can {what}. Switch role at the top right.", 403)


def _editable(submission_id):
    sub = q("SELECT status FROM submissions WHERE id = ?", (submission_id,), one=True)
    if sub["status"] == "approved":
        raise WorkflowError("This content is already approved. Create new content to change it.")


def accept_fix(submission_id, finding_id, role):
    _require(role, ("creator",), "accept fixes")
    _editable(submission_id)
    cur, f = _find(submission_id, finding_id)
    if f.get("fix") is None:
        raise WorkflowError("This finding has no automatic fix. Use Edit to change the text yourself.")
    content = engine.tidy(cur["content"][:f["start"]] + f["fix"] + cur["content"][f["end"]:])
    q("INSERT INTO decisions (submission_id, version_n, rule_id, quote, action, reason, role, created_at) VALUES (?,?,?,?,?,?,?,?)",
      (submission_id, cur["n"], f["rule_id"], f["quote"], "accepted", None, role, now()))
    q("UPDATE submissions SET status = 'draft' WHERE id = ?", (submission_id,))
    audit(role, "finding.accept", submission_id, f"{f['rule_id']}: “{f['quote']}” → “{f['fix']}”")
    return create_version(submission_id, content, role, f"Accepted fix for {f['rule_id']}")


def apply_rewrite(submission_id, role):
    _require(role, ("creator",), "accept the suggested version")
    _editable(submission_id)
    cur = _current(submission_id)
    ai = json.loads(cur["ai"]) if cur["ai"] else None
    if not ai or not ai.get("rewrite"):
        raise WorkflowError("The suggested version is not ready yet.")
    if ai["rewrite"].strip() == cur["content"].strip():
        raise WorkflowError("The suggested version is the same as the current text.")
    q("UPDATE submissions SET status = 'draft' WHERE id = ?", (submission_id,))
    audit(role, "rewrite.accept", submission_id, f"Accepted suggested version of v{cur['n']}")
    return create_version(submission_id, ai["rewrite"], role, "Accepted the suggested version")


def edit_content(submission_id, content, role):
    _require(role, ("creator",), "edit the content")
    _editable(submission_id)
    if not content.strip():
        raise WorkflowError("Content is empty.")
    q("UPDATE submissions SET status = 'draft' WHERE id = ?", (submission_id,))
    audit(role, "content.edit", submission_id, "Edited the text by hand")
    return create_version(submission_id, content.strip(), role, "Edited by hand")


def dismiss(submission_id, finding_id, reason, role):
    _require(role, ("creator", "reviewer", "manager"), "dismiss findings")
    _editable(submission_id)
    if not reason or len(reason.strip()) < 3:
        raise WorkflowError("Please give a short reason. Reasons help improve unclear brand rules.")
    cur, f = _find(submission_id, finding_id)
    if f["severity"] == "critical" and role != "manager":
        raise WorkflowError("Critical findings can only be dismissed by a Manager.", 403)
    q("INSERT INTO decisions (submission_id, version_n, rule_id, quote, action, reason, role, created_at) VALUES (?,?,?,?,?,?,?,?)",
      (submission_id, cur["n"], f["rule_id"], f["quote"], "rejected", reason.strip(), role, now()))
    audit(role, "finding.dismiss", submission_id, f"{f['rule_id']}: “{f['quote']}” — {reason.strip()}")


def submit_for_review(submission_id, role):
    _require(role, ("creator",), "submit content for review")
    _editable(submission_id)
    q("UPDATE submissions SET status = 'in_review', updated_at = ? WHERE id = ?", (now(), submission_id))
    audit(role, "submission.submit", submission_id, "Sent for review")


def comment(submission_id, text, role):
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
    q("UPDATE submissions SET status = 'approved', updated_at = ? WHERE id = ?", (now(), submission_id))
    detail = f"Approved v{sub['current']['n']}" + (f" — {note.strip()}" if note and note.strip() else "")
    if sub["approval"]["needs_manager"]:
        detail += " (manager sign-off with open critical findings)"
    audit(role, "submission.approve", submission_id, detail)


def global_audit(limit=200):
    rows = q("SELECT a.ts, a.role, a.action, a.detail, a.submission_id, s.title FROM audit a "
             "LEFT JOIN submissions s ON s.id = a.submission_id ORDER BY a.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def rule_insights():
    """Early view of the Guideline Clarity Score data: how often each rule's findings are dismissed."""
    kit = get_kit()
    names = {r["id"]: r["title"] for r in kit["rules"]} | {t["id"]: f"Term: {t['preferred']}" for t in kit["glossary"]}
    rows = q("SELECT rule_id, action, COUNT(*) AS n FROM decisions GROUP BY rule_id, action")
    stats = {}
    for r in rows:
        s = stats.setdefault(r["rule_id"], {"rule_id": r["rule_id"], "title": names.get(r["rule_id"], r["rule_id"]),
                                            "accepted": 0, "rejected": 0})
        s[r["action"]] = r["n"]
    for s in stats.values():
        total = s["accepted"] + s["rejected"]
        s["dismiss_rate"] = round(s["rejected"] / total, 2) if total else 0
    return sorted(stats.values(), key=lambda s: (-s["dismiss_rate"], -s["rejected"]))


def seed(background=False):
    data = json.loads(SEED.read_text(encoding="utf-8"))
    samples = data.pop("samples")
    with _lock:
        for table in ("kit", "submissions", "versions", "decisions", "comments", "audit"):
            _db.execute(f"DELETE FROM {table}")
        _db.execute("DELETE FROM sqlite_sequence")
        _db.execute("INSERT INTO kit (id, data, version) VALUES (1, ?, 1)", (json.dumps(data),))
        _db.commit()
    data["samples"] = samples
    for s in reversed(samples):
        create_submission(s["title"], s["channel"], s["content"], s["intent"], "creator", background)
    audit("system", "demo.reset", None, "Demo data loaded")
