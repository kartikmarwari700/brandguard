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
    assert len(subs) == 4
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
    stats = client.get("/api/insights").json()
    assert stats[0]["rule_id"] == minor["rule_id"] and stats[0]["rejected"] == 1


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
    assert first["model"] == "claude-haiku-5-5" and first["output_config"] == {"effort": "low"}
    assert first["max_tokens"] == 4000 and first["cache_control"] == {"type": "ephemeral"}
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
