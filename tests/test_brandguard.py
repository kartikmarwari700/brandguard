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
