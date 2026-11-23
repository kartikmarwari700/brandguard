import json
import os
from pathlib import Path

import pytest

os.environ.pop("BRANDGUARD_ANTHROPIC_API_KEY", None)  # tests run in rules-only mode: deterministic and free

from app import engine  # noqa: E402

KIT = json.loads((Path(__file__).resolve().parents[1] / "app" / "seed" / "codecraft.json").read_text(encoding="utf-8"))
TODAY = "2026-11-17"


def rule_ids(text, intent=None):
    findings, verified = engine.run_rule_checks(text, KIT, intent, today=TODAY)
    return [f["rule_id"] for f in findings], verified, findings


# ---------------------------------------------------------------- engine

def test_superlative_and_wrong_number_are_critical():
    ids, verified, findings = rule_ids("Join India's #1 coding platform with 50,000 students.")
    assert "CLAIM-02" in ids
    wrong = next(f for f in findings if f["rule_id"] == "CLAIM-01")
    assert wrong["severity"] == "critical" and wrong["fix"] == "38,000" and "FACT-01" in wrong["cited"]


def test_supported_claim_is_verified_not_flagged():
    ids, verified, _ = rule_ids("Get 70% off the Winter Bootcamp. Learners rate us 4.6 out of 5.")
    assert "CLAIM-01" not in ids
    assert {v["fact_id"] for v in verified} == {"FACT-02", "FACT-03"}


def test_expired_fact_is_reported_as_outdated_with_current_value():
    _, _, findings = rule_ids("We work with 95 hiring partners.")
    f = next(f for f in findings if f["rule_id"] == "CLAIM-01")
    assert "expired" in f["message"] and f["fix"] == "120" and f["cited"] == ["CLAIM-01", "FACT-06", "FACT-04"]


def test_number_without_any_fact_has_no_source():
    _, _, findings = rule_ids("Our alumni earn 12 lakh on average.")
    f = next(f for f in findings if f["rule_id"] == "CLAIM-01")
    assert "No approved fact" in f["message"] and f["fix"] is None


def test_dates_and_years_are_not_claims():
    ids, _, _ = rule_ids("The offer ends on 30 Nov 2026.")
    assert "CLAIM-01" not in ids


def test_glossary_keeps_correct_spellings_and_fixes_variants():
    ids, _, findings = rule_ids("CodeCraft Academy is great. Codecraft Academy runs the winter boot camp. Signup now.")
    fixes = {f["quote"]: f["fix"] for f in findings if f["rule_id"].startswith("TERM")}
    assert fixes == {"Codecraft Academy": "CodeCraft Academy", "winter boot camp": "Winter Bootcamp", "Signup": "Sign up"}


def test_pressure_slang_guarantee_and_click_here():
    ids, _, _ = rule_ids("Hurry!!! u r gonna get guaranteed placement. Click here.")
    for rid in ("TONE-03", "TONE-02", "CLAIM-03", "ACC-02"):
        assert rid in ids


def test_long_sentence_and_missing_cta():
    long = "We " + "really " * 30 + "care."
    ids, _, _ = rule_ids(long, {"purpose": "x", "audience": "y", "cta": "book a seat"})
    assert "ACC-01" in ids and "MSG-01" in ids


def test_clean_content_passes():
    ids, verified, _ = rule_ids("Our mentors will help you build your first website in one weekend. "
                                "Learners rate us 4.6 out of 5. Book a seat on our website.",
                                {"purpose": "p", "audience": "a", "cta": "book a seat"})
    assert ids == [] and len(verified) == 1


def test_apply_fixes_and_intent_check():
    text = "Over 50,000 students learn with Codecraft Academy. LAST CHANCE!!! Signup today."
    intent = {"purpose": "p", "audience": "a", "cta": "sign up"}
    findings, _ = engine.run_rule_checks(text, KIT, intent, today=TODAY)
    fixed = engine.apply_fixes(text, findings)
    assert "38,000" in fixed and "CodeCraft Academy" in fixed and "LAST CHANCE" not in fixed and "Sign up" in fixed
    assert engine.verify_intent(text, fixed, intent, KIT)["ok"]


def test_intent_check_blocks_invented_numbers_and_lost_cta():
    intent = {"purpose": "p", "audience": "a", "cta": "sign up"}
    check = engine.verify_intent("Sign up at CodeCraft Academy.", "Join 99,999 people at CodeCraft Academy.", intent, KIT)
    assert not check["ok"] and len(check["problems"]) == 2


# ---------------------------------------------------------------- workflow (API)

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BRANDGUARD_DB", str(tmp_path / "test.db"))
    import importlib

    from fastapi.testclient import TestClient

    import app.main as main
    importlib.reload(main)
    return TestClient(main.app)


def H(role):
    return {"X-Role": role}


def test_seeded_demo_is_ready_for_judges(client):
    subs = client.get("/api/submissions").json()
    assert len(subs) == 6 and sorted(s["kind"] for s in subs) == ["image"] + ["text"] * 5
    poster = next(s for s in subs if s["kind"] == "image")
    assert poster["thumb"].startswith("data:image/jpeg;base64,")
    assert client.get("/api/status").json()["ai"] is False
    assert client.get("/").status_code == 200


def test_full_workflow_creator_reviewer_manager(client):
    sid = client.post("/api/submissions", headers=H("creator"), json={
        "title": "Test post", "channel": "Instagram post",
        "content": "Join India's #1 coding school! 50,000 students. Signup today.",
        "intent": {"purpose": "promote", "audience": "students", "cta": "sign up"}}).json()["id"]
    sub = client.get(f"/api/submissions/{sid}").json()
    assert sub["approval"]["needs_manager"] is True

    # reviewer cannot approve a draft, and cannot approve open critical findings
    assert client.post(f"/api/submissions/{sid}/approve", headers=H("reviewer"), json={"text": ""}).status_code == 400
    assert client.post(f"/api/submissions/{sid}/submit", headers=H("creator")).status_code == 200
    r = client.post(f"/api/submissions/{sid}/approve", headers=H("reviewer"), json={"text": ""})
    assert r.status_code == 403 and "Manager" in r.json()["detail"]

    # creator accepts the number fix -> new version, back to draft
    number = next(f for f in sub["current"]["findings"] if f["rule_id"] == "CLAIM-01")
    sub = client.post(f"/api/submissions/{sid}/accept", headers=H("creator"), json={"finding_id": number["id"]}).json()
    assert sub["current"]["n"] == 2 and "38,000" in sub["current"]["content"] and sub["status"] == "draft"

    # only a manager may dismiss a critical finding, and a reason is required
    sup = next(f for f in sub["current"]["findings"] if f["rule_id"] == "CLAIM-02")
    assert client.post(f"/api/submissions/{sid}/dismiss", headers=H("creator"),
                       json={"finding_id": sup["id"], "reason": "fine"}).status_code == 403
    assert client.post(f"/api/submissions/{sid}/dismiss", headers=H("manager"),
                       json={"finding_id": sup["id"], "reason": ""}).status_code == 400

    # creator accepts the suggested version, resubmits; reviewer can now approve
    sub = client.post(f"/api/submissions/{sid}/apply-rewrite", headers=H("creator")).json()
    client.post(f"/api/submissions/{sid}/submit", headers=H("creator"))
    still_critical = client.get(f"/api/submissions/{sid}").json()["approval"]["needs_manager"]
    role = "manager" if still_critical else "reviewer"
    sub = client.post(f"/api/submissions/{sid}/approve", headers=H(role), json={"text": "ok"}).json()
    assert sub["status"] == "approved"
    actions = [a["action"] for a in sub["audit"]]
    assert actions[0] == "submission.create" and actions[-1] == "submission.approve"

    # approved content is locked
    assert client.post(f"/api/submissions/{sid}/edit", headers=H("creator"), json={"text": "x"}).status_code == 400


def test_request_changes_and_comments(client):
    sid = client.get("/api/submissions").json()[0]["id"]
    client.post(f"/api/submissions/{sid}/submit", headers=H("creator"))
    sub = client.post(f"/api/submissions/{sid}/request-changes", headers=H("reviewer"), json={"text": "Shorter please"}).json()
    assert sub["status"] == "changes_requested" and sub["comments"][-1]["text"] == "Shorter please"


def test_dismissal_feeds_rule_insights(client):
    sid = client.get("/api/submissions").json()[0]["id"]
    sub = client.get(f"/api/submissions/{sid}").json()
    minor = next(f for f in sub["current"]["findings"] if f["severity"] == "minor")
    client.post(f"/api/submissions/{sid}/dismiss", headers=H("creator"), json={"finding_id": minor["id"], "reason": "Name is quoted from a partner"})
    sub = client.get(f"/api/submissions/{sid}").json()
    assert any(f["status"] == "dismissed" for f in sub["current"]["findings"])
    stats = {s["rule_id"]: s for s in client.get("/api/insights").json()}
    seeded = {"TERM-01": 0, "TERM-02": 0, "TERM-03": 0, "ACC-03": 0}
    assert stats[minor["rule_id"]]["rejected"] == seeded.get(minor["rule_id"], stats[minor["rule_id"]]["rejected"] - 1) + 1


def test_only_manager_edits_brand_kit_and_new_fact_is_used(client):
    fact = {"statement": "Placement support is available for 6 months.", "value": 6, "unit": "months",
            "keywords": ["months", "placement support"], "source": "Careers handbook", "owner": "Careers",
            "valid_until": "2027-12-31"}
    assert client.post("/api/kit/facts", headers=H("creator"), json=fact).status_code == 403
    kit = client.post("/api/kit/facts", headers=H("manager"), json=fact).json()
    assert kit["version"] == 2 and kit["facts"][-1]["id"] == "FACT-08"
    sid = client.post("/api/submissions", headers=H("creator"), json={
        "title": "t", "content": "Get 6 months of placement support at CodeCraft Academy. Apply now.",
        "intent": {"cta": "apply"}}).json()["id"]
    verified = client.get(f"/api/submissions/{sid}").json()["current"]["verified"]
    assert verified and verified[0]["fact_id"] == "FACT-08"


# ---------------------------------------------------------------- claim provenance (I1)

def test_fact_update_rechecks_drafts_and_alerts_approved_content(client):
    subs = {s["title"]: s["id"] for s in client.get("/api/submissions").json()}
    ad, insta = subs["Ad: weekend workshop"], subs["Instagram: Winter Bootcamp launch"]
    client.post(f"/api/submissions/{ad}/submit", headers=H("creator"))
    assert client.post(f"/api/submissions/{ad}/approve", headers=H("reviewer"), json={"text": ""}).json()["status"] == "approved"

    # a manager changes two facts: the student count and the rating
    assert client.put("/api/kit/facts/FACT-01", headers=H("creator"), json={"value": 40000}).status_code == 403
    client.put("/api/kit/facts/FACT-01", headers=H("manager"), json={"value": 40000, "statement": "40,000 students have enrolled."})
    client.put("/api/kit/facts/FACT-03", headers=H("manager"), json={"value": 4.7})

    draft = client.get(f"/api/submissions/{insta}").json()
    assert draft["versions"][-1]["author"] == "system" and "Re-checked automatically" in draft["versions"][-1]["note"]
    number = next(f for f in draft["current"]["findings"] if f["rule_id"] == "CLAIM-01")
    assert number["fix"] == "40,000"

    approved = client.get(f"/api/submissions/{ad}").json()
    assert approved["status"] == "approved" and len(approved["versions"]) == 1  # approved content is never rewritten
    assert approved["provenance_alerts"] and "changed from 4.6 to 4.7" in approved["provenance_alerts"][0]
    assert next(s for s in client.get("/api/submissions").json() if s["id"] == ad)["alerts"] == 1
    usage = client.get("/api/kit/usage").json()
    assert any(u["id"] == ad for u in usage["FACT-03"])


# ---------------------------------------------------------------- Review Agent with a simulated model

from types import SimpleNamespace as NS  # noqa: E402


def _tool(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def _usage(inp=2000, out=400):
    return NS(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _fake_client(script, calls):
    def create(**kwargs):
        calls.append(kwargs)
        response = script.pop(0)
        if not hasattr(response, "usage"):
            response.usage = _usage()
        return response
    class Fake:
        def __init__(self, **kw):
            self.messages = NS(create=create)
    return Fake


AGENT_TEXT = "Our amazing mentors help you learn. Sign up at CodeCraft Academy."


def _run_agent(monkeypatch, script, text=AGENT_TEXT, fresh=True):
    import anthropic

    from app import agent
    if fresh:
        agent.budget.reset()
        agent.clear_cache()
    calls = []
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _fake_client(script, calls))
    intent = {"purpose": "p", "audience": "a", "cta": "sign up"}
    findings, verified = engine.run_rule_checks(text, KIT, intent, today=TODAY)
    return agent.review_with_ai(text, intent, KIT, findings, verified), calls


def test_agent_policy_guard_keeps_only_cited_findings(monkeypatch):
    script = [
        NS(stop_reason="tool_use", content=[_tool("t1", "add_finding", rule_id="TONE-01", quote="amazing", severity="major",
                                                  explanation="Hype word.", suggested_fix="")]),
        NS(stop_reason="tool_use", content=[_tool("t2", "search_rules", query="tone hype")]),
        NS(stop_reason="tool_use", content=[
            _tool("t3", "add_finding", rule_id="TONE-01", quote="amazing", severity="major", explanation="Hype word.", suggested_fix="experienced"),
            _tool("t4", "add_finding", rule_id="TONE-01", quote="not in the text", severity="minor", explanation="x", suggested_fix=""),
            _tool("t5", "submit_rewrite", text="Our experienced mentors help you learn. Sign up at CodeCraft Academy.")]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="Done.")]),
    ]
    result, calls = _run_agent(monkeypatch, script)
    assert result["mode"] == "ai"
    assert [f["rule_id"] for f in result["findings"]] == ["TONE-01"] and result["findings"][0]["source"] == "ai"
    assert result["findings"][0]["fix"] == "experienced"
    assert {r["reason"] for r in result["rejected"]} == {"cited an id no tool returned", "quote not found in content"}
    assert result["rewrite"].startswith("Our experienced mentors") and result["rewrite_check"]["ok"]
    assert [t["tool"] for t in result["tool_log"]] == ["add_finding", "search_rules", "add_finding", "add_finding", "submit_rewrite"]
    first = calls[0]
    assert first["model"] == "claude-haiku-5-5" and first["output_config"] == {"effort": "medium"}
    assert first["max_tokens"] == 6000 and first["cache_control"] == {"type": "ephemeral"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in first and "betas" not in first  # Haiku 5.5 has no server-side refusal fallback
    # tool results go back in one user message, matched by id
    batches = [[b["tool_use_id"] for b in m["content"]] for m in calls[-1]["messages"]
               if m["role"] == "user" and isinstance(m["content"], list)]
    assert batches == [["t1"], ["t2"], ["t3", "t4", "t5"]]
    # a verified rewrite ends the loop: the closing "Done." turn is never paid for
    assert len(calls) == 3
    assert result["cost_usd"] == pytest.approx(3 * (2000 * 0.10 + 400 * 0.50) / 1e6)


def test_agent_findings_are_advisory_and_fixes_are_publishable(monkeypatch):
    script = [
        NS(stop_reason="tool_use", content=[_tool("t1", "search_rules", query="tone hype claims")]),
        NS(stop_reason="tool_use", content=[
            _tool("t2", "add_finding", rule_id="TONE-01", quote="amazing", severity="critical", explanation="Hype.",
                  fix_kind="replace", suggested_fix="Remove the word."),
            _tool("t3", "add_finding", rule_id="TONE-01", quote="help you learn", severity="minor", explanation="Vague.",
                  fix_kind="replace", suggested_fix="help you learn by [date]"),
            _tool("t4", "add_finding", rule_id="TONE-01", quote="Our", severity="minor", explanation="x",
                  fix_kind="replace", suggested_fix="Rephrase to sound warmer"),
            _tool("t5", "add_finding", rule_id="TONE-01", quote="mentors", severity="minor", explanation="y",
                  fix_kind="remove", suggested_fix=""),
            _tool("t6", "add_finding", rule_id="TONE-01", quote="amazing mentors", severity="major", explanation="dup",
                  fix_kind="none", suggested_fix="")]),
        NS(stop_reason="end_turn", content=[]),
    ]
    result, _ = _run_agent(monkeypatch, script)
    by_quote = {f["quote"]: f for f in result["findings"]}
    assert "amazing mentors" not in by_quote and len(result["findings"]) == 4  # overlapping repeat of a rule is refused
    assert by_quote["amazing"]["severity"] == "major" and by_quote["amazing"]["fix"] == ""   # capped, removal
    assert by_quote["help you learn"]["fix"] is None and "[date]" in by_quote["help you learn"]["fix_note"]
    assert by_quote["Our"]["fix"] is None and by_quote["mentors"]["fix"] == ""
    assert engine.tidy("more than 300 hours last month, .") == "more than 300 hours last month."


def test_agent_reuses_identical_review_without_new_cost(monkeypatch):
    from app import agent
    script = [NS(stop_reason="tool_use", content=[_tool("t1", "submit_rewrite", text="Our experienced mentors help you learn. Sign up at CodeCraft Academy.")])]
    first, calls = _run_agent(monkeypatch, script)
    assert first["mode"] == "ai" and not first["cached"] and len(calls) == 1
    again, calls2 = _run_agent(monkeypatch, [], fresh=False)
    assert again["cached"] and again["cost_usd"] == 0 and calls2 == []
    assert again["rewrite"] == first["rewrite"]
    snap = agent.budget.snapshot()
    assert snap["reviews"] == 1 and snap["reused"] == 1 and snap["api_calls"] == 1


def test_agent_budget_cap_switches_to_rules_only(monkeypatch):
    monkeypatch.setenv("BRANDGUARD_AI_BUDGET_USD", "0.002")
    script = [NS(stop_reason="tool_use", content=[_tool("t1", "search_rules", query="tone")], usage=_usage(20000, 2000))]
    result, calls = _run_agent(monkeypatch, script)
    # the first response used up the budget: the loop stops before paying for another turn
    assert len(calls) == 1 and result["mode"] == "ai" and "budget ran out" in result["note"]
    nxt, calls2 = _run_agent(monkeypatch, [], text="Learn Python at CodeCraft Academy. Sign up today.", fresh=False)
    assert nxt["mode"] == "rules-only" and "budget" in nxt["note"] and calls2 == []


def test_agent_hourly_cap_and_length_cap(monkeypatch):
    monkeypatch.setenv("BRANDGUARD_AI_MAX_PER_HOUR", "1")
    ok = [NS(stop_reason="end_turn", content=[])]
    _run_agent(monkeypatch, ok)
    capped, calls = _run_agent(monkeypatch, [], text="Learn Python at CodeCraft Academy. Sign up today.", fresh=False)
    assert capped["mode"] == "rules-only" and "per hour" in capped["note"] and calls == []
    long_text = "Learn Python with CodeCraft Academy mentors. " * 80 + "Sign up today."
    skipped, calls = _run_agent(monkeypatch, [], text=long_text)
    assert skipped["mode"] == "rules-only" and "3,000 characters" in skipped["note"] and calls == []


def test_status_reports_ai_budget(client, monkeypatch):
    from app import agent
    agent.budget.reset()
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    status = client.get("/api/status").json()
    assert status["ai"] and status["model"] == "claude-haiku-5-5"
    assert status["ai_budget"]["limit_usd"] == 1.0 and status["ai_budget"]["paused"] is None


def test_agent_rewrite_with_invented_number_is_not_shown(monkeypatch):
    script = [
        NS(stop_reason="tool_use", content=[_tool("t1", "submit_rewrite", text="Join 99,999 learners at CodeCraft Academy. Sign up today.")]),
        NS(stop_reason="end_turn", content=[]),
    ]
    result, _ = _run_agent(monkeypatch, script)
    assert "99,999" not in result["rewrite"] and result["rewrite_check"]["ok"]


def test_agent_falls_back_to_rules_only_on_api_error(monkeypatch):
    import anthropic
    import httpx

    def boom(**kwargs):
        raise anthropic.APIStatusError("unavailable", response=httpx.Response(503, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    class Fake:
        def __init__(self, **kw):
            self.messages = NS(create=boom)
    from app import agent
    agent.budget.reset()
    agent.clear_cache()
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    result = agent.review_with_ai("LAST CHANCE!!! Sign up at CodeCraft Academy.", {"cta": "sign up"}, KIT,
                                  *engine.run_rule_checks("LAST CHANCE!!! Sign up at CodeCraft Academy.", KIT, {"cta": "sign up"}))
    assert result["mode"] == "rules-only" and "503" in result["note"] and "LAST CHANCE" not in result["rewrite"]


def test_agent_polish_feedback_does_not_block_a_safe_rewrite(monkeypatch):
    script = [
        NS(stop_reason="tool_use", content=[_tool("t1", "search_rules", query="tone hype")]),
        NS(stop_reason="tool_use", content=[
            _tool("t2", "add_finding", rule_id="TONE-01", quote="amazing", severity="major", explanation="Hype.",
                  fix_kind="replace", suggested_fix="experienced"),
            _tool("t3", "submit_rewrite", text="Our amazing mentors help you learn. Sign up at CodeCraft Academy today.")]),
        NS(stop_reason="end_turn", content=[]),
    ]
    result, calls = _run_agent(monkeypatch, script)
    feedback = json.loads(calls[-1]["messages"][-1]["content"][-1]["content"])
    # the agent is told to polish its own unaddressed finding, but the safe rewrite is still shown
    assert feedback["ok"] is False and any("amazing" in p for p in feedback["problems"])
    assert result["rewrite_check"]["ok"] and result["rewrite"].endswith("today.")


# ---------------------------------------------------------------- workspaces and robustness

def W(ws, role="creator"):
    return {"X-Workspace": ws, "X-Role": role}


def test_each_browser_gets_its_own_workspace(client):
    a, b = "visitor-aaaa-1111", "visitor-bbbb-2222"
    sid = client.get("/api/submissions", headers=W(a)).json()[0]["id"]
    client.post(f"/api/submissions/{sid}/submit", headers=W(a))
    assert client.get(f"/api/submissions/{sid}", headers=W(a)).json()["status"] == "in_review"
    # another visitor cannot see or change it, and has its own untouched samples
    assert client.get(f"/api/submissions/{sid}", headers=W(b)).status_code == 404
    assert client.post(f"/api/submissions/{sid}/submit", headers=W(b)).status_code == 404
    assert {s["status"] for s in client.get("/api/submissions", headers=W(b)).json()} == {"draft"}
    # a Brand Kit change and a reset stay inside one workspace
    client.post("/api/kit/facts", headers=W(b, "manager"), json={"statement": "x", "value": 5, "unit": "x", "source": "s",
                                                                    "valid_until": "2030-01-01"})
    assert client.get("/api/kit", headers=W(b)).json()["version"] == 2
    assert client.get("/api/kit", headers=W(a)).json()["version"] == 1
    client.post("/api/reset", headers=W(b))
    assert client.get(f"/api/submissions/{sid}", headers=W(a)).json()["status"] == "in_review"


def test_repeated_actions_and_bad_input_get_clear_messages(client):
    h = W("visitor-cccc-3333")
    sub = client.get("/api/submissions", headers=h).json()
    sid = next(s["id"] for s in sub if s["title"].startswith("Email"))
    detail = client.get(f"/api/submissions/{sid}", headers=h).json()
    minor = next(f for f in detail["current"]["findings"] if f["severity"] != "critical")
    body = {"finding_id": minor["id"], "reason": "Fine for this audience"}
    assert client.post(f"/api/submissions/{sid}/dismiss", headers=h, json=body).status_code == 200
    again = client.post(f"/api/submissions/{sid}/dismiss", headers=h, json=body)
    assert again.status_code == 409 and "already dismissed" in again.json()["detail"]
    assert client.post(f"/api/submissions/{sid}/submit", headers=h).status_code == 200
    twice = client.post(f"/api/submissions/{sid}/submit", headers=h)
    assert twice.status_code == 409 and "Already sent" in twice.json()["detail"]
    too_long = client.post("/api/submissions", headers=h, json={"title": "x", "content": "a" * 9000})
    assert too_long.status_code == 422 and too_long.json()["detail"] == "Content is too long (max 8000 characters)."
    empty = client.post("/api/submissions", headers=h, json={"title": "x", "content": "   "})
    assert empty.status_code == 400 and "empty" in empty.json()["detail"]
    assert client.get("/api/submissions/999999", headers=h).status_code == 404


# ---------------------------------------------------------------- Guideline Clarity Score (I2)

def test_clarity_score_flags_the_vague_rule_first(client):
    scores = client.get("/api/clarity").json()["scores"]
    top = scores[0]
    assert top["rule_id"] == "TONE-04" and top["status"] == "unclear"
    assert top["decisions"] == 8 and top["dismissed"] == 6 and top["score"] == 25
    assert {"phrase": "hey", "count": 2} in top["dismissed_phrases"]
    assert any(t["word"] in ("friendly", "greeting") for t in top["themes"])
    assert next(s for s in scores if s["rule_id"] == "TONE-02")["status"] == "clear"


def test_clarity_draft_replays_history_and_applies_as_new_kit_version(client):
    assert client.post("/api/clarity/TONE-04/draft", headers=H("creator")).status_code == 403
    draft = client.post("/api/clarity/TONE-04/draft", headers=H("manager")).json()
    assert draft["source"] == "code" and draft["status"] == "draft"
    assert draft["after"]["patterns"] == ["guys", "awesome"]   # dismissed-and-never-accepted phrases removed
    ev = draft["evidence"]
    assert ev["accepted_still_caught"] == ev["accepted_total"] == 2
    assert ev["dismissed_no_longer_flagged"] == 5 and ev["dismissed_total"] == 6
    # asking again returns the same open draft instead of a duplicate
    assert client.post("/api/clarity/TONE-04/draft", headers=H("manager")).json()["id"] == draft["id"]
    whatsapp = next(s["id"] for s in client.get("/api/submissions").json() if s["title"].startswith("WhatsApp"))
    before = client.get(f"/api/submissions/{whatsapp}").json()["current"]["findings"]
    assert sum(f["rule_id"] == "TONE-04" for f in before) == 3
    applied = client.post(f"/api/clarity/proposals/{draft['id']}/apply", headers=H("manager"),
                          json={"text": "Be warm and friendly. Greetings like 'hey' are fine in community chats; avoid 'guys' and filler hype."})
    assert applied.json()["status"] == "applied"
    kit = client.get("/api/kit").json()
    rule = next(r for r in kit["rules"] if r["id"] == "TONE-04")
    assert kit["version"] == 2 and rule["patterns"] == ["guys", "awesome"] and "community chats" in rule["text"]
    after = client.get(f"/api/submissions/{whatsapp}").json()
    assert after["current"]["author"] == "system" and not any(f["rule_id"] == "TONE-04" for f in after["current"]["findings"])
    assert client.post(f"/api/clarity/proposals/{draft['id']}/apply", headers=H("manager"), json={}).status_code == 409
    rescored = next(s for s in client.get("/api/clarity").json()["scores"] if s["rule_id"] == "TONE-04")
    assert rescored["status"] == "clarified" and rescored["decisions_before_rewrite"] == 8


def test_clarity_draft_with_simulated_model(client, monkeypatch):
    import anthropic

    from app import agent
    agent.budget.reset()
    client.get("/api/clarity")  # seed the workspace before the (simulated) key is set
    proposal = NS(type="tool_use", id="p1", name="propose_rule", input={
        "title": "Warm, not careless", "text": "Be warm. Casual greetings are fine in community channels; avoid slangy group words.",
        "good": "Hi everyone, the workshop is back this Saturday.", "bad": "Sup guys!!", "patterns": ["guys", "sup", "x"],
        "rationale": "Greetings were dismissed as fine. Only group slang stays."})
    calls = []
    monkeypatch.setattr(anthropic, "Anthropic", _fake_client([NS(stop_reason="tool_use", content=[proposal])], calls))
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    draft = client.post("/api/clarity/TONE-04/draft", headers=H("manager")).json()
    assert draft["source"] == "ai" and draft["after"]["patterns"] == ["guys", "sup"]   # too-short phrase dropped
    assert draft["evidence"]["accepted_lost"] == ["awesome"]   # code shows what the draft would stop catching
    assert calls[0]["tools"][0]["name"] == "propose_rule" and calls[0]["model"] == "claude-haiku-5-5"


# ---------------------------------------------------------------- images: brand colours, contrast, alt text (I3)

POSTER = Path(__file__).resolve().parents[1] / "app" / "seed" / "poster-winter-bootcamp.png"


def test_image_upload_runs_colour_and_alt_checks_in_code(client):
    h = W("visitor-img-0001")
    bad = client.post("/api/submissions/image", headers=h, files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400 and "not a readable" in bad.json()["detail"]
    sub = client.post("/api/submissions/image", headers=h, files={"file": ("poster.png", POSTER.read_bytes(), "image/png")},
                      data={"title": "Poster", "caption": "Sign up today.", "alt": "", "cta": "sign up"}).json()
    assert sub["kind"] == "image" and sub["current"]["media"]["width"] == 1024
    rules = {f["rule_id"]: f for f in sub["current"]["findings"]}
    assert rules["VIS-01"]["quote"].startswith("#D8") and rules["VIS-01"]["target"] == "image"
    assert rules["ACC-04"]["severity"] == "major" and rules["ACC-04"]["target"] == "alt"
    assert any(c["ok"] for c in sub["current"]["media"]["colours"])       # white counts as a neutral
    assert "off" in sub["current"]["ai"]["note"]                           # rules-only: text in the image needs the AI
    edited = client.post(f"/api/submissions/{sub['id']}/edit", headers=h,
                         json={"text": "Sign up today.", "alt": "Winter Bootcamp poster with a magenta header and the offer text."}).json()
    assert edited["current"]["n"] == 2 and not any(f["rule_id"] == "ACC-04" for f in edited["current"]["findings"])
    accept = client.post(f"/api/submissions/{sub['id']}/accept", headers=h, json={"finding_id": rules["VIS-01"]["id"]})
    assert accept.status_code == 409   # stale finding from v1: the screen refreshes
    v2_vis = next(f for f in edited["current"]["findings"] if f["rule_id"] == "VIS-01")
    no_fix = client.post(f"/api/submissions/{sub['id']}/accept", headers=h, json={"finding_id": v2_vis["id"]})
    assert no_fix.status_code == 400 and "no automatic fix" in no_fix.json()["detail"]


def test_image_review_reads_text_measures_contrast_and_suggests_alt(client, monkeypatch):
    import anthropic

    from app import agent, images
    h = W("visitor-img-0002")
    client.get("/api/submissions", headers=h)      # seed first, without the simulated key
    agent.budget.reset()
    images._cache.clear()
    read = NS(type="tool_use", id="r1", name="report_image", input={"alt_text": "Winter Bootcamp poster: 70% off for college students.", "texts": [
        {"text": "WINTER BOOTCAMP", "x": 0.2, "y": 0.1, "w": 0.6, "h": 0.3, "size": "large"},
        {"text": "India's #1 coding platform", "x": 0.1, "y": 0.42, "w": 0.8, "h": 0.08, "size": "normal"},
        {"text": "50,000+ students already enrolled", "x": 0.1, "y": 0.64, "w": 0.8, "h": 0.07, "size": "normal"},
        {"text": "70% OFF · LAST CHANCE!!!", "x": 0.1, "y": 0.73, "w": 0.8, "h": 0.08, "size": "large"},
        {"text": "Sign up at codecraft.example", "x": 0.15, "y": 0.855, "w": 0.7, "h": 0.06, "size": "normal"}]})
    calls = []
    monkeypatch.setattr(anthropic, "Anthropic", _fake_client([NS(stop_reason="tool_use", content=[read])], calls))
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    sub = client.post("/api/submissions/image", headers=h, files={"file": ("poster.png", POSTER.read_bytes(), "image/png")},
                      data={"title": "Poster", "caption": "Our Winter Bootcamp is open.", "alt": "poster", "cta": "sign up"}).json()
    sid = sub["id"]
    import time
    for _ in range(50):
        sub = client.get(f"/api/submissions/{sid}", headers=h).json()
        if sub["current"]["ai_status"] != "running":
            break
        time.sleep(0.1)
    ai = sub["current"]["ai"]
    assert calls[0]["messages"][0]["content"][0]["type"] == "image"
    failing = {e["text"]: e["measure"]["pass"] for e in ai["elements"]}
    assert failing == {"WINTER BOOTCAMP": True, "India's #1 coding platform": True, "50,000+ students already enrolled": False,
                       "70% OFF · LAST CHANCE!!!": False, "Sign up at codecraft.example": True}
    found = {(f["rule_id"], f["quote"]) for f in sub["current"]["findings"]}
    assert ("ACC-05", "50,000+ students already enrolled") in found and ("CLAIM-02", "#1") in found
    assert ("CLAIM-01", "50,000") in found and not any(f[0] == "MSG-01" for f in found)   # the CTA is in the image
    claim = next(f for f in sub["current"]["findings"] if f["rule_id"] == "CLAIM-01")
    assert claim["target"] == "image_text" and claim["fix"] is None and "38,000" in claim["fix_note"]
    alt = next(f for f in sub["current"]["findings"] if f["rule_id"] == "ACC-04")
    assert alt["fix"] == "Winter Bootcamp poster: 70% off for college students." and alt["source"] == "ai"
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "")
    after = client.post(f"/api/submissions/{sid}/accept", headers=h, json={"finding_id": alt["id"]}).json()
    assert after["current"]["media"]["alt"].startswith("Winter Bootcamp poster")


# ---------------------------------------------------------------- Brand Kit import from guideline documents

GUIDE = Path(__file__).resolve().parents[1] / "app" / "static" / "samples" / "codecraft-brand-guidelines.pdf"


def _upload_guide(client, h, name="guidelines.pdf", data=None):
    return client.post("/api/kit/import", headers=h, files={"file": (name, data if data is not None else GUIDE.read_bytes(), "application/pdf")})


def test_import_without_ai_extracts_rules_and_terms_and_needs_a_manager(client):
    m = W("visitor-imp-0001", "manager")
    assert _upload_guide(client, W("visitor-imp-0001", "creator")).status_code == 403
    bad = _upload_guide(client, m, "notes.txt", b"hello")
    assert bad.status_code == 400 and "PDF or a Word" in bad.json()["detail"]
    broken = _upload_guide(client, m, "broken.pdf", b"%PDF-1.4 nonsense")
    assert broken.status_code == 400
    imp = _upload_guide(client, m).json()
    assert imp["source"] == "code" and imp["pages"] == 3 and imp["status"] == "draft"
    terms = {i["data"]["preferred"]: i for i in imp["items"] if i["kind"] == "term"}
    assert terms["online course"]["data"]["variants"] == ["e-course", "ecourse"]
    assert terms["hackathon"]["data"]["variants"] == ["hack-a-thon", "hack a thon"] and terms["hackathon"]["page"] == "page 2"
    assert terms["mentors"]["duplicate_of"] == "TERM-05" and terms["online course"]["duplicate_of"] is None
    picks = [{"index": terms["online course"]["index"], "data": {}}, {"index": terms["hackathon"]["index"], "data": {"variants": ["hack-a-thon"]}}]
    res = client.post(f"/api/kit/imports/{imp['id']}/apply", headers=m, json={"items": picks}).json()
    assert len(res["added"]) == 2 and res["kit"]["version"] == 2
    added = {t["preferred"]: t for t in res["kit"]["glossary"] if t["id"] in res["added"]}
    assert added["hackathon"]["variants"] == ["hack-a-thon"] and "page 2" in added["online course"]["imported_from"]
    again = client.post(f"/api/kit/imports/{imp['id']}/apply", headers=m, json={"items": picks})
    assert again.status_code == 409
    sub = client.post("/api/submissions", headers=W("visitor-imp-0001"), json={"title": "t", "content": "Join our e-course at CodeCraft Academy. Sign up today.",
                                                                                "intent": {"cta": "sign up"}}).json()
    assert any(f["rule_id"] == added["online course"]["id"] and f["fix"] == "online course" for f in sub["current"]["findings"])


def test_import_with_simulated_model_keeps_only_quotes_found_in_the_document(client, monkeypatch):
    import anthropic

    from app import agent
    m = W("visitor-imp-0002", "manager")
    client.get("/api/kit", headers=m)   # seed before the simulated key is set
    agent.budget.reset()
    blank = {"title": "", "text": "", "category": "tone", "severity": "major", "patterns": [], "preferred": "", "variants": [],
             "statement": "", "value": 0, "unit": "", "valid_until": ""}
    items = [
        {**blank, "kind": "rule", "page": "page 1", "quote": "Avoid corporate jargon such as synergy, leverage and paradigm shift.",
         "title": "No corporate jargon", "text": "Avoid corporate jargon.", "patterns": ["synergy", "leverage", "paradigm shift"]},
        {**blank, "kind": "fact", "page": "page 2", "quote": "Approved figures: CodeCraft Academy has 45 mentors (source: HR roster, valid until 2027-06-30).",
         "statement": "CodeCraft Academy has 45 mentors.", "value": 45, "unit": "mentors", "valid_until": "2027-06-30"},
        {**blank, "kind": "fact", "page": "page 2", "quote": "The Summer Bootcamp runs for 6 weeks.", "statement": "The Summer Bootcamp runs for 6 weeks.",
         "value": 6, "unit": "weeks"},
        {**blank, "kind": "fact", "page": "page 2", "quote": "CodeCraft Academy has 120 hiring partners.", "statement": "CodeCraft Academy has 120 hiring partners.",
         "value": 120, "unit": "partners"},
        {**blank, "kind": "rule", "page": "page 1", "quote": "Always use exclamation marks to sound exciting.", "title": "Be exciting", "text": "Use exclamation marks."},
    ]
    calls = []
    monkeypatch.setattr(anthropic, "Anthropic", _fake_client([NS(stop_reason="tool_use", content=[
        NS(type="tool_use", id="i1", name="propose_items", input={"items": items})])], calls))
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    imp = _upload_guide(client, m).json()
    assert imp["source"] == "ai" and "=== page 2 ===" in calls[0]["messages"][0]["content"]
    assert [r["quote"] for r in imp["rejected"]] == ["Always use exclamation marks to sound exciting."]   # invented: not in the document
    by = {i["data"].get("title") or i["data"].get("statement"): i for i in imp["items"]}
    assert by["CodeCraft Academy has 45 mentors."]["data"]["valid_until"] == "2027-06-30"
    assert by["The Summer Bootcamp runs for 6 weeks."]["expiry_guessed"] is True
    assert by["CodeCraft Academy has 120 hiring partners."]["duplicate_of"] == "FACT-04"
    assert by["No corporate jargon"]["data"]["patterns"] == ["synergy", "leverage", "paradigm shift"]
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "")
    res = client.post(f"/api/kit/imports/{imp['id']}/apply", headers=m, json={"items": [
        {"index": by["No corporate jargon"]["index"]}, {"index": by["CodeCraft Academy has 45 mentors."]["index"]}]}).json()
    fact = next(f for f in res["kit"]["facts"] if f["id"] in res["added"])
    assert fact["value"] == 45 and fact["unit"] == "mentors"
    check = client.post("/api/submissions", headers=W("visitor-imp-0002"), json={"title": "t", "content": "Our 60 mentors bring synergy. Sign up today.",
                                                                                  "intent": {"cta": "sign up"}}).json()
    found = {(f["rule_id"], f["quote"]) for f in check["current"]["findings"]}
    assert ("CLAIM-01", "60") in found and any(q == "synergy" for _, q in found)


# ---------------------------------------------------------------- labelled evaluation set (runs in CI)

def test_evaluation_set_precision_and_recall():
    from eval.run_eval import evaluate
    res = evaluate()
    t = res["total"]
    assert res["cases"] >= 50
    assert t["tp"] / (t["tp"] + t["fp"]) >= 0.95 and t["tp"] / (t["tp"] + t["fn"]) >= 0.95, (res["extras"], res["misses"])
    assert res["verified"][0] == res["verified"][1]


def test_account_without_credit_pauses_ai_and_says_why(monkeypatch):
    import anthropic
    import httpx

    from app import agent
    agent.budget.reset()
    agent.clear_cache()
    calls = []

    def no_credit(**kwargs):
        calls.append(kwargs)
        raise anthropic.BadRequestError("no credit", response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
                                        body={"type": "error", "error": {"type": "invalid_request_error",
                                                                         "message": "Your credit balance is too low to access the Anthropic API."}})
    class Fake:
        def __init__(self, **kw):
            self.messages = NS(create=no_credit)
    monkeypatch.setenv("BRANDGUARD_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    text = "Join 38,000 students at CodeCraft Academy. Sign up today."
    first = agent.review_with_ai(text, {"cta": "sign up"}, KIT, *engine.run_rule_checks(text, KIT, {"cta": "sign up"}))
    assert first["mode"] == "rules-only" and "no credit left" in first["note"] and len(calls) == 1
    other = "Learn Python at CodeCraft Academy. Sign up today."
    second = agent.review_with_ai(other, {"cta": "sign up"}, KIT, *engine.run_rule_checks(other, KIT, {"cta": "sign up"}))
    assert second["mode"] == "rules-only" and "unavailable" in second["note"] and len(calls) == 1   # no new API call
    snap = agent.budget.snapshot()
    assert snap["outage"] and "no credit" in snap["paused"]
    agent.budget.reset()


def test_import_without_ai_also_finds_stated_facts(client):
    m = W("visitor-imp-0003", "manager")
    imp = _upload_guide(client, m).json()
    facts = {i["data"]["statement"]: i for i in imp["items"] if i["kind"] == "fact"}
    assert facts["CodeCraft Academy has 45 mentors."]["data"]["valid_until"] == "2027-06-30"
    assert facts["CodeCraft Academy has 45 mentors."]["data"]["unit"] == "mentors" and facts["CodeCraft Academy has 45 mentors."]["data"]["value"] == 45
    assert facts["The Summer Bootcamp runs for 6 weeks."]["expiry_guessed"] is True
    assert facts["CodeCraft Academy has 120 hiring partners."]["duplicate_of"] == "FACT-04"
    assert not any("4.5" in str(i["data"].get("value")) for i in imp["items"] if i["kind"] == "fact")   # a threshold, not a fact


def test_manual_contrast_check_on_the_poster_without_ai(client):
    h = W("visitor-con-0001")
    subs = client.get("/api/submissions", headers=h).json()
    poster = next(s["id"] for s in subs if s["kind"] == "image")
    text = next(s["id"] for s in subs if s["kind"] == "text")
    yellow = client.post(f"/api/submissions/{poster}/contrast", headers=h, json={"x": 0.1, "y": 0.64, "w": 0.8, "h": 0.07, "size": "normal"}).json()
    assert yellow["measure"]["pass"] is False and yellow["measure"]["ratio"] < 2
    card = next(f for f in yellow["submission"]["current"]["findings"] if f["rule_id"] == "ACC-05")
    assert card["quote"] == "Marked text #1" and card["target"] == "image" and "Ink" in card["fix_note"]
    ink = client.post(f"/api/submissions/{poster}/contrast", headers=h, json={"x": 0.15, "y": 0.855, "w": 0.7, "h": 0.06, "size": "normal"}).json()
    assert ink["measure"]["pass"] is True and sum(f["rule_id"] == "ACC-05" for f in ink["submission"]["current"]["findings"]) == 1
    assert [c["n"] for c in ink["submission"]["current"]["media"]["checks"]] == [1, 2]
    tiny = client.post(f"/api/submissions/{poster}/contrast", headers=h, json={"x": 0.1, "y": 0.1, "w": 0.005, "h": 0.005, "size": "normal"})
    assert tiny.status_code == 400
    assert client.post(f"/api/submissions/{text}/contrast", headers=h, json={"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.1}).status_code == 400
    dismiss = client.post(f"/api/submissions/{poster}/dismiss", headers=W("visitor-con-0001", "manager"),
                          json={"finding_id": card["id"], "reason": "The designer is fixing the colour"})
    assert dismiss.status_code == 200
