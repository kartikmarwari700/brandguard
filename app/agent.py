"""The Review Agent: deterministic tools first, then (optionally) Claude with tool calling.

Rule checks always run in code. When ANTHROPIC_API_KEY is set, Claude plans which
further checks the content needs (tone, messaging, claims in context), calls the
tools below for evidence, records findings, and proposes a rewrite. The policy
guard only keeps findings that cite an ID returned by a tool in the same run,
and the rewrite must pass verify_intent in code. The key is read only from
BRANDGUARD_ANTHROPIC_API_KEY. Without it the agent runs in rules-only mode, so
the product still works end to end.
"""
import json
import os
import re

from . import engine

MODEL = os.environ.get("BRANDGUARD_MODEL", "claude-opus-5-5")
MAX_TOOL_CALLS = 10
MAX_TURNS = 8

SYSTEM_PROMPT = """You are BrandGuard's Review Agent. You review a piece of marketing content before it is published,
against the organisation's Brand Kit. Code has already run exact checks (glossary, banned phrases, numbers vs approved
facts, readability); those findings are given to you. Your job is the judgment the code cannot do:
- tone and voice that breaks a rule even without a banned word,
- messaging problems (missing or unclear call to action, message that does not fit the stated purpose or audience),
- claims or comparisons the code did not catch.

How to work:
1. Call search_rules with the themes you want to check, so you only cite rules that exist.
2. Call lookup_fact for any claim you are unsure about.
3. Call add_finding once per NEW problem. Do not repeat problems that the code already found.
   Every finding must cite a rule_id that search_rules returned, and quote text copied exactly from the content.
4. Finally call submit_rewrite with one improved version of the full content that fixes every finding (the code's and
   yours), keeps the creator's purpose, audience and call to action, keeps the brand name, and uses only numbers that are
   approved facts. If submit_rewrite reports problems, fix them and submit once more.
Be concise. Do not invent facts, rules, statistics or offers. If the content is already good, say so and submit it unchanged."""

TOOLS = [
    {"name": "search_rules", "strict": True,
     "description": "Find Brand Kit rules relevant to a theme or passage. Returns rule ids, titles and text. "
                    "Only rule ids returned here may be cited in add_finding.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                      "properties": {"query": {"type": "string", "description": "Theme or passage, e.g. 'tone hype' or 'call to action'"}}}},
    {"name": "lookup_fact", "strict": True,
     "description": "Check a claim or number against approved facts. Returns supported / contradicted / outdated / no_source "
                    "with the fact id, statement, source and expiry.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["claim"],
                      "properties": {"claim": {"type": "string", "description": "The claim text, including its number and context words"}}}},
    {"name": "add_finding", "strict": True,
     "description": "Record one new problem. rule_id must come from search_rules; quote must be copied exactly from the content.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "required": ["rule_id", "quote", "severity", "explanation", "suggested_fix"],
                      "properties": {
                          "rule_id": {"type": "string"},
                          "quote": {"type": "string", "description": "Exact text from the content (may be empty for a missing element)"},
                          "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                          "explanation": {"type": "string", "description": "One or two plain sentences"},
                          "suggested_fix": {"type": "string", "description": "Replacement text for the quote, or empty if none"}}}},
    {"name": "submit_rewrite", "strict": True,
     "description": "Submit the full improved content. Code checks it: call to action and brand name kept, no unapproved "
                    "numbers, no remaining rule violations. Returns ok or a list of problems.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["text"],
                      "properties": {"text": {"type": "string"}}}},
]


API_KEY_VAR = "BRANDGUARD_ANTHROPIC_API_KEY"  # dedicated variable: never pick up credentials meant for other tools


def ai_available():
    return bool(os.environ.get(API_KEY_VAR))


class _Run:
    """State for one review: what the tools returned, so citations can be validated."""

    def __init__(self, text, intent, kit, rule_findings):
        self.text, self.intent, self.kit = text, intent, kit
        self.rule_findings = rule_findings
        self.returned_ids = {f["rule_id"] for f in rule_findings}
        self.ai_findings, self.rejected, self.tool_log = [], [], []
        self.rewrite, self.rewrite_check = None, None

    # ---- tools
    def search_rules(self, query):
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        scored = []
        for r in self.kit["rules"]:
            hay = f"{r['id']} {r['category']} {r['title']} {r['text']}".lower()
            score = sum(hay.count(w) for w in words)
            if score:
                scored.append((score, r))
        hits = [r for _, r in sorted(scored, key=lambda s: -s[0])[:5]] or self.kit["rules"][:3]
        self.returned_ids.update(r["id"] for r in hits)
        return [{"rule_id": r["id"], "title": r["title"], "text": r["text"], "severity": r["severity"],
                 "good_example": r.get("good"), "bad_example": r.get("bad")} for r in hits]

    def lookup_fact(self, claim):
        claims = engine.extract_claims(claim)
        if not claims:
            words = set(re.findall(r"\w+", claim.lower()))
            facts = [f for f in self.kit["facts"] if words & {k.lower() for k in f["keywords"]}]
            self.returned_ids.update(f["id"] for f in facts)
            return {"status": "no_number", "related_facts": facts[:3]}
        c = claims[0]
        c["context"] = claim.lower()
        result = engine.lookup_fact(c, self.kit)
        out = {"status": result["status"]}
        for key in ("fact", "current"):
            if result.get(key):
                self.returned_ids.add(result[key]["id"])
                out[key] = result[key]
        return out

    def add_finding(self, rule_id, quote, severity, explanation, suggested_fix):
        if rule_id not in self.returned_ids:
            self.rejected.append({"rule_id": rule_id, "reason": "cited an id no tool returned"})
            return {"ok": False, "error": f"{rule_id} was not returned by search_rules; search first, then cite."}
        start = self.text.find(quote) if quote else len(self.text)
        if quote and start < 0:
            self.rejected.append({"rule_id": rule_id, "reason": "quote not found in content"})
            return {"ok": False, "error": "quote must be copied exactly from the content"}
        if any(f["rule_id"] == rule_id and f["quote"] == quote for f in self.rule_findings + self.ai_findings):
            return {"ok": False, "error": "already reported"}
        self.ai_findings.append(engine._finding(
            rule_id, next((r["category"] for r in self.kit["rules"] if r["id"] == rule_id), "messaging"),
            severity, start, start + len(quote), quote, explanation,
            fix=suggested_fix if (quote and suggested_fix) else None,
            fix_note=None if quote else (suggested_fix or None), source="ai"))
        return {"ok": True}

    def submit_rewrite(self, text):
        check = engine.verify_intent(self.text, text, self.intent, self.kit)
        remaining, _ = engine.run_rule_checks(text, self.kit, self.intent)
        serious = [f"{f['rule_id']}: {f['quote']}" for f in remaining if f["severity"] in ("critical", "major")]
        problems = check["problems"] + [f"still violates {s}" for s in serious]
        self.rewrite, self.rewrite_check = text, {"ok": not problems, "problems": problems}
        return self.rewrite_check

    def call(self, name, args):
        fn = {"search_rules": self.search_rules, "lookup_fact": self.lookup_fact,
              "add_finding": self.add_finding, "submit_rewrite": self.submit_rewrite}[name]
        result = fn(**args)
        self.tool_log.append({"tool": name, "input": args, "ok": not (isinstance(result, dict) and result.get("ok") is False)})
        return result


def _offline_result(text, intent, kit, rule_findings, reason):
    rewrite = engine.apply_fixes(text, rule_findings)
    check = engine.verify_intent(text, rewrite, intent, kit)
    return {"mode": "rules-only", "status": "done", "note": reason, "findings": [],
            "rewrite": rewrite, "rewrite_check": check, "tool_log": [], "rejected": []}


def review_with_ai(text, intent, kit, rule_findings, verified):
    """Run the Claude Review Agent. Returns dict with ai findings, rewrite and an auditable tool log."""
    if not ai_available():
        return _offline_result(text, intent, kit, rule_findings,
                               "AI review is off (no API key configured). Rule checks and fact checks ran in code.")
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ[API_KEY_VAR], base_url="https://api.anthropic.com")
    run = _Run(text, intent, kit, rule_findings)
    brief = {
        "content": text, "intent": intent, "brand": kit["brand"]["name"], "voice": kit["brand"]["voice"],
        "code_findings": [{"rule_id": f["rule_id"], "quote": f["quote"], "message": f["message"], "fix": f["fix"]}
                          for f in rule_findings],
        "verified_claims": verified,
    }
    messages = [{"role": "user", "content": "Review this content.\n" + json.dumps(brief, ensure_ascii=False, indent=1)}]
    calls = 0
    try:
        for _ in range(MAX_TURNS):
            response = client.beta.messages.create(
                model=MODEL, max_tokens=16000, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            if response.stop_reason == "refusal":
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for block in tool_uses:
                calls += 1
                if calls > MAX_TOOL_CALLS:
                    out, is_error = {"error": "tool budget used up; submit_rewrite now or stop"}, True
                else:
                    try:
                        out, is_error = run.call(block.name, block.input), False
                    except (TypeError, KeyError) as exc:
                        out, is_error = {"error": f"bad tool input: {exc}"}, True
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str), "is_error": is_error})
            messages.append({"role": "user", "content": results})
    except anthropic.APIStatusError as exc:
        return _offline_result(text, intent, kit, rule_findings, f"AI review unavailable ({exc.status_code}); rule checks only.")
    except anthropic.APIConnectionError:
        return _offline_result(text, intent, kit, rule_findings, "AI review unavailable (network); rule checks only.")

    rewrite, check = run.rewrite, run.rewrite_check
    if rewrite is None or not check["ok"]:
        # The policy guard never shows an unverified rewrite; fall back to the code-applied fixes.
        fallback = engine.apply_fixes(text, rule_findings)
        rewrite, check = fallback, engine.verify_intent(text, fallback, intent, kit)
    return {"mode": "ai", "model": MODEL, "status": "done", "note": None, "findings": run.ai_findings,
            "rewrite": rewrite, "rewrite_check": check, "tool_log": run.tool_log, "rejected": run.rejected}
