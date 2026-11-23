"""Guideline Clarity Score (improvement I2).

Every accept and dismiss decision is already stored per rule. Here they become a score per rule, rules
that people keep overriding are flagged as "possibly unclear", and the Review Agent drafts a clearer
version of the rule. Code then replays the past decisions against the draft, so the manager sees evidence
("4 of 6 dismissed cases would no longer be flagged, every accepted case is still caught") before
approving it into the Brand Kit as a new version.
"""
import json
import re
from collections import Counter

from . import agent, engine, store

MIN_DECISIONS = 3          # below this a score is shown as "needs more data"
UNCLEAR_DISMISS_RATE = 0.4  # 40% or more of decisions dismissed -> possibly unclear
STOPWORDS = set("""a an the and or but if of to in on for with is are was were be been this that these those it its our
our we you your they their them as at by from not no so very just can cannot it's is not this that which who than then
there here into about also only too more most less fine ok okay""".split())


def _item_names(kit):
    names = {r["id"]: r["title"] for r in kit["rules"]}
    names.update({t["id"]: f"Use “{t['preferred']}”" for t in kit["glossary"]})
    return names


def _themes(reasons, top=4):
    """The words that come up most in dismissal reasons (a simple, explainable way to group them)."""
    words = Counter()
    for r in reasons:
        words.update({w for w in re.findall(r"[a-z][a-z'-]{2,}", (r or "").lower()) if w not in STOPWORDS})
    return [{"word": w, "count": c} for w, c in words.most_common(top) if c >= 2]


def scores():
    kit = store.get_kit()
    names = _item_names(kit)
    rules = {r["id"]: r for r in kit["rules"]}
    by_rule = store.decisions_by_rule()
    drafts = {p["rule_id"]: p for p in list_proposals("draft")}
    out = []
    for rule_id, rows in by_rule.items():
        if rule_id not in names:
            continue
        clarified = rules.get(rule_id, {}).get("clarified")
        before_count = 0
        if clarified:  # a rewritten rule starts a fresh score from the decisions made after the rewrite
            before_count = sum(1 for r in rows if r["created_at"] < clarified)
            rows = [r for r in rows if r["created_at"] >= clarified]
        accepted = [r for r in rows if r["action"] == "accepted"]
        dismissed = [r for r in rows if r["action"] == "rejected"]
        n = len(rows)
        rate = len(dismissed) / n if n else 0
        score = round(100 * len(accepted) / n) if n else None
        status = "needs_data" if n < MIN_DECISIONS else ("unclear" if rate >= UNCLEAR_DISMISS_RATE else "clear")
        if clarified and n < MIN_DECISIONS:
            status = "clarified"
        phrases = Counter(r["quote"].lower() for r in dismissed if len(r["quote"]) <= 40)
        out.append({
            "rule_id": rule_id, "title": names[rule_id], "text": rules.get(rule_id, {}).get("text"),
            "is_rule": rule_id in rules, "score": score, "decisions": n, "accepted": len(accepted),
            "dismissed": len(dismissed), "dismiss_rate": round(rate, 2), "status": status,
            "themes": _themes([r["reason"] for r in dismissed]),
            "dismissed_phrases": [{"phrase": p, "count": c} for p, c in phrases.most_common(6)],
            "examples": [{"quote": r["quote"][:160], "reason": r["reason"]} for r in dismissed[-4:]],
            "draft_id": drafts.get(rule_id, {}).get("id"),
            "clarified": clarified, "decisions_before_rewrite": before_count,
        })
    order = {"unclear": 0, "clarified": 1, "clear": 2, "needs_data": 3}
    out.sort(key=lambda s: (order[s["status"]], s["score"] if s["score"] is not None else 101, -s["decisions"]))
    return out


# ---------------------------------------------------------------- drafts

def _flags(patterns, quote):
    return any(re.search(engine._boundary_pattern(p), quote, re.IGNORECASE) for p in patterns)


def evidence(rule, draft, rows):
    """Replay past decisions on this rule against the drafted patterns (code, not AI)."""
    if not rule.get("patterns"):
        return {"replayed": False, "note": "This rule has no phrase list, so the draft changes its wording and examples only."}
    accepted = [r for r in rows if r["action"] == "accepted"]
    dismissed = [r for r in rows if r["action"] == "rejected"]
    still_caught = [r["quote"] for r in accepted if _flags(draft["patterns"], r["quote"])]
    no_longer = [r["quote"] for r in dismissed if not _flags(draft["patterns"], r["quote"])]
    return {"replayed": True, "accepted_total": len(accepted), "accepted_still_caught": len(still_caught),
            "accepted_lost": [r["quote"] for r in accepted if r["quote"] not in still_caught],
            "dismissed_total": len(dismissed), "dismissed_no_longer_flagged": len(no_longer),
            "removed_patterns": [p for p in rule["patterns"] if p not in draft["patterns"]],
            "added_patterns": [p for p in draft["patterns"] if p not in rule["patterns"]]}


def _code_draft(rule, rows):
    """Offline draft: drop phrases people dismissed and never accepted; keep everything else."""
    accepted = {r["quote"].lower() for r in rows if r["action"] == "accepted"}
    dismissed = Counter(r["quote"].lower() for r in rows if r["action"] == "rejected")
    keep = [p for p in rule.get("patterns", []) if not (dismissed.get(p.lower()) and p.lower() not in accepted)]
    reasons = list(dict.fromkeys(r["reason"].rstrip(". ") for r in rows if r["action"] == "rejected" and r["reason"]))[:3]
    text = rule["text"] + (" It does not apply when: " + "; ".join(reasons) + "." if reasons else "")
    if len(text) > 500:
        text = text[:497].rsplit(" ", 1)[0] + "…"
    return {"title": rule["title"], "text": text, "good": rule.get("good", ""), "bad": rule.get("bad", ""),
            "patterns": keep, "rationale": "Drafted in code (AI review is off): phrases that were dismissed and never accepted "
                                           "are removed, and the dismissal reasons are added as exceptions."}


PROPOSE_TOOL = {
    "name": "propose_rule", "strict": True,
    "description": "Submit the clearer version of the brand rule. Call exactly once.",
    "input_schema": {"type": "object", "additionalProperties": False,
                     "required": ["title", "text", "good", "bad", "patterns", "rationale"],
                     "properties": {
                         "title": {"type": "string", "description": "Short rule title"},
                         "text": {"type": "string", "description": "The rule in one to three plain sentences, including when it does NOT apply"},
                         "good": {"type": "string", "description": "One example that follows the rule"},
                         "bad": {"type": "string", "description": "One example that breaks the rule"},
                         "patterns": {"type": "array", "items": {"type": "string"},
                                      "description": "Phrases that always break the rule (may be empty). Only phrases that are wrong in every context."},
                         "rationale": {"type": "string", "description": "Two sentences: what was unclear and what changed"}}},
}

DRAFT_PROMPT = """You improve brand guidelines. A rule is being overridden often by the people who use it, which means it is
unclear or too broad. You get the current rule, the phrases it flags, and real decisions: what reviewers accepted and what
they dismissed with their reasons. Write a clearer version that keeps the brand's intent, states plainly when the rule
does not apply, and keeps only phrases that are wrong in every context (phrases people accepted should usually stay;
phrases people dismissed with good reasons should usually go). Do not invent facts about the brand. Call propose_rule once."""


def _ai_draft(rule, rows, kit):
    blocked = agent.budget.try_start()
    if blocked:
        return None, blocked, 0.0
    brief = {"brand": kit["brand"]["name"], "voice": kit["brand"]["voice"],
             "rule": {k: rule.get(k) for k in ("id", "title", "text", "good", "bad", "patterns", "severity")},
             "accepted": [r["quote"] for r in rows if r["action"] == "accepted"][:20],
             "dismissed": [{"quote": r["quote"][:200], "reason": r["reason"]} for r in rows if r["action"] == "rejected"][:20]}
    import anthropic

    try:
        response = agent.make_client().messages.create(
            model=agent.MODEL, max_tokens=3000, system=DRAFT_PROMPT, tools=[PROPOSE_TOOL],
            messages=[{"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
            output_config={"effort": "low"})
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        return None, f"AI drafting unavailable ({agent.api_error(exc)}); drafted in code instead.", 0.0
    cost = agent.budget.add(agent.MODEL, response.usage)
    call = next((b for b in response.content if b.type == "tool_use" and b.name == "propose_rule"), None)
    if not call:
        return None, "The AI did not return a draft; drafted in code instead.", cost
    d = call.input
    draft = {"title": str(d.get("title", ""))[:120] or rule["title"], "text": str(d.get("text", ""))[:500] or rule["text"],
             "good": str(d.get("good", ""))[:200], "bad": str(d.get("bad", ""))[:200],
             "patterns": list(dict.fromkeys(p.strip()[:40] for p in d.get("patterns", []) if 2 <= len(p.strip()) <= 40))[:12],
             "rationale": str(d.get("rationale", ""))[:400]}
    return draft, None, cost


def draft_rule(rule_id, role):
    if role != "manager":
        raise store.WorkflowError("Only a manager can draft changes to the Brand Kit. Switch to Manager.", 403)
    kit = store.get_kit()
    rule = next((r for r in kit["rules"] if r["id"] == rule_id), None)
    if not rule:
        raise store.WorkflowError("Rule not found. Only rules (not glossary terms) can be redrafted here.", 404)
    rows = store.decisions_by_rule().get(rule_id, [])
    if rule.get("clarified"):
        rows = [r for r in rows if r["created_at"] >= rule["clarified"]]
    if len(rows) < MIN_DECISIONS:
        raise store.WorkflowError(f"This rule needs at least {MIN_DECISIONS} review decisions before it can be scored.")
    existing = next((p for p in list_proposals("draft") if p["rule_id"] == rule_id), None)
    if existing:
        return existing
    draft, note, cost, source = None, None, 0.0, "ai"
    if agent.ai_available():
        draft, note, cost = _ai_draft(rule, rows, kit)
    else:
        note = "AI review is off; drafted in code."
    if draft is None:
        draft, source = _code_draft(rule, rows), "code"
    data = {"rule_id": rule_id, "kit_version": kit["version"], "source": source, "note": note, "cost_usd": round(cost, 5),
            "before": {k: rule.get(k) for k in ("title", "text", "good", "bad", "patterns")},
            "after": draft, "evidence": evidence(rule, draft, rows)}
    with store._lock:
        cur = store._db.execute("INSERT INTO proposals (ws, kind, status, created_at, data) VALUES (?,?,?,?,?)",
                                (store.ws(), "rule", "draft", store.now(), json.dumps(data)))
        store._db.commit()
    store.audit(role, "clarity.draft", None, f"{rule_id}: clearer version drafted ({'AI' if source == 'ai' else 'code'})")
    return get_proposal(cur.lastrowid)


def get_proposal(pid):
    row = store.q("SELECT * FROM proposals WHERE id = ? AND ws = ? AND kind = 'rule'", (pid, store.ws()), one=True)
    if not row:
        raise store.WorkflowError("Draft not found. Refresh the page.", 404)
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"], **json.loads(row["data"])}


def list_proposals(status=None):
    rows = store.q("SELECT id FROM proposals WHERE ws = ? AND kind = 'rule'" + (" AND status = ?" if status else "") +
                   " ORDER BY id DESC", (store.ws(), status) if status else (store.ws(),))
    return [get_proposal(r["id"]) for r in rows]


def apply_proposal(pid, edited, role):
    if role != "manager":
        raise store.WorkflowError("Only a manager can change the Brand Kit. Switch to Manager.", 403)
    p = get_proposal(pid)
    if p["status"] != "draft":
        raise store.WorkflowError(f"This draft was already {p['status']}.", 409)
    kit = store.get_kit()
    rule = next((r for r in kit["rules"] if r["id"] == p["rule_id"]), None)
    if not rule:
        raise store.WorkflowError("The rule no longer exists.", 404)
    after = {**p["after"], **{k: v for k, v in (edited or {}).items() if k in ("title", "text", "good", "bad", "patterns") and v is not None}}
    after["patterns"] = [s.strip() for s in after.get("patterns", []) if s and s.strip()]
    if not after.get("title", "").strip() or not after.get("text", "").strip():
        raise store.WorkflowError("The rule needs a title and a text.")
    rule.update({k: after[k] for k in ("title", "text", "good", "bad", "patterns")})
    rule["clarified"] = store.now()
    _set_status(pid, "applied", {"applied": after})
    return store.save_kit(kit, role, "kit.clarify_rule", f"{p['rule_id']} rewritten from the Guideline Clarity Score")


def discard_proposal(pid, role):
    if role != "manager":
        raise store.WorkflowError("Only a manager can discard a draft.", 403)
    p = get_proposal(pid)
    if p["status"] != "draft":
        raise store.WorkflowError(f"This draft was already {p['status']}.", 409)
    _set_status(pid, "discarded")
    store.audit(role, "clarity.discard", None, f"{p['rule_id']}: draft discarded")


def _set_status(pid, status, extra=None):
    row = store.q("SELECT data FROM proposals WHERE id = ?", (pid,), one=True)
    data = {**json.loads(row["data"]), **(extra or {})}
    store.q("UPDATE proposals SET status = ?, data = ? WHERE id = ?", (status, json.dumps(data), pid))
