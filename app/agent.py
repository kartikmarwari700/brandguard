"""The Review Agent: deterministic tools first, then (optionally) Claude with tool calling.

Rule checks always run in code. When BRANDGUARD_ANTHROPIC_API_KEY is set, Claude plans which
further checks the content needs (tone, messaging, claims in context), calls the
tools below for evidence, records findings, and proposes a rewrite. The policy
guard only keeps findings that cite an ID returned by a tool in the same run,
and the rewrite must pass verify_intent in code. The key is read only from
BRANDGUARD_ANTHROPIC_API_KEY. Without it the agent runs in rules-only mode, so
the product still works end to end.

Cost control (the demo runs on a small, fixed API budget):
- Claude Haiku 5.5 (medium effort), a short tool loop and a per-response token cap;
- prompt caching on the shared system prompt and the growing conversation;
- identical reviews are reused instead of paid for twice (same text, intent, Brand Kit, model and day);
- a spend meter computed from every response's token usage stops AI review at BRANDGUARD_AI_BUDGET_USD,
  plus an hourly cap on new AI reviews and a length cap on the text sent to the model.
When a limit is reached the review continues in rules-only mode and says why.
"""
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import date

from . import engine

log = logging.getLogger("brandguard.agent")

MODEL = os.environ.get("BRANDGUARD_MODEL", "claude-haiku-5-5")
MAX_TOOL_CALLS = 8
MAX_TURNS = 6
MAX_TOKENS = 6000      # per response; thinking counts toward it
EFFORT = "medium"      # Haiku 5.5 default; "low" saved little and produced weaker rewrites
MAX_REWRITES = 3       # submit_rewrite attempts per review
AI_MAX_CHARS = 3000    # longer texts get rule checks only
CACHE_SIZE = 256

# USD per million tokens (input, output) for prompts under 100K tokens. Cache reads are billed at 0.1x input
# (0.05x on the 5.5 Sonnet/Opus models, so 0.1x over-estimates there) and 5-minute cache writes at 1.25x input.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}

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
   Your findings are advisory, so use severity major or minor. A suggested_fix with fix_kind "replace" must be
   ready-to-publish text that replaces the quote: never an instruction and never a placeholder such as [date].
   Use fix_kind "remove" to delete the quote, or "none" when the fix needs information you do not have.
4. Finally call submit_rewrite with one improved version of the full content that fixes every finding (the code's and
   yours, including minor ones), keeps the creator's purpose, audience and call to action, keeps the brand name, and
   uses only numbers that are approved facts. Write it as polished, natural copy in the brand voice that a brand manager
   could publish as it is: no rhetorical tricks, notes, brackets or placeholders. Keep it about as long as the original.
   Add nothing that is not in the original content or the Brand Kit: no new offers, services, features, dates or claims.
   If submit_rewrite reports problems, fix them and submit again.
Do not invent facts, rules, statistics or offers. If the content is already good, say so and submit it unchanged."""

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
                      "required": ["rule_id", "quote", "severity", "explanation", "fix_kind", "suggested_fix"],
                      "properties": {
                          "rule_id": {"type": "string"},
                          "quote": {"type": "string", "description": "Exact text from the content (may be empty for a missing element)"},
                          "severity": {"type": "string", "enum": ["major", "minor"]},
                          "explanation": {"type": "string", "description": "One or two plain sentences"},
                          "fix_kind": {"type": "string", "enum": ["replace", "remove", "none"],
                                       "description": "replace: suggested_fix replaces the quote; remove: delete the quote; "
                                                      "none: no automatic fix (put a short tip in suggested_fix)"},
                          "suggested_fix": {"type": "string", "description": "For replace: exact ready-to-publish replacement "
                                                                             "text. Otherwise a short tip or empty."}}}},
    {"name": "submit_rewrite", "strict": True,
     "description": "Submit the full improved content. Code checks it: call to action and brand name kept, no unapproved "
                    "numbers, no remaining rule violations. Returns ok or a list of problems.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["text"],
                      "properties": {"text": {"type": "string"}}}},
]


API_KEY_VAR = "BRANDGUARD_ANTHROPIC_API_KEY"  # dedicated variable: never pick up credentials meant for other tools

_PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>|\bTBD\b|\bX{2,}\b")
_REMOVE = re.compile(r"^(remove|delete|drop|cut)\b[^.]*\.?$", re.I)
_INSTRUCTION = re.compile(r"^(replace|rewrite|rephrase|add|change|consider|mention|specify|include)\b", re.I)


def ai_available():
    return bool(os.environ.get(API_KEY_VAR))


def make_client():
    """Dedicated key and endpoint only; a short timeout so a stalled network call can never hang a review."""
    import anthropic

    return anthropic.Anthropic(api_key=os.environ[API_KEY_VAR], base_url="https://api.anthropic.com",
                               timeout=45.0, max_retries=1)


def api_error(exc):
    """Short, key-free reason for an API failure, shown in the review note."""
    code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    msg = ""
    if isinstance(body, dict):
        msg = str((body.get("error") or {}).get("message") or "")
    low = msg.lower()
    if "credit balance" in low:
        reason = "the API account has no credit left"
    elif "spend limit" in low or "usage limit" in low or "limit" in low and code in (400, 429):
        reason = "the API account reached its spend limit"
    elif code == 401:
        reason = "the API key was rejected"
    elif code == 429:
        reason = "too many requests right now"
    elif code and code >= 500:
        reason = "the AI service is busy"
    else:
        reason = msg[:120] or "request failed"
    # account problems will not fix themselves in seconds: stop calling the API for a while (fast rules-only reviews)
    if code in (401, 403) or "credit" in reason or "spend limit" in reason:
        budget.pause(reason, 600)
    elif code == 429:
        budget.pause(reason, 60)
    return f"{code or 'network'}: {reason}"


REVIEW_DEADLINE = 100  # seconds for one whole review; the UI shows a spinner meanwhile


def cost_usd(model, usage):
    """Cost of one response from its usage block (input_tokens excludes cached tokens)."""
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5-5"])  # unknown model: assume the dearest

    def n(key):
        return getattr(usage, key, 0) or 0

    return (n("input_tokens") * price_in + n("cache_creation_input_tokens") * price_in * 1.25
            + n("cache_read_input_tokens") * price_in * 0.1 + n("output_tokens") * price_out) / 1_000_000


class Budget:
    """Spend meter for this server process. The hard cap is the Anthropic Console spend limit; this keeps the
    demo well below it and switches to rules-only mode before it is reached."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.spent, self.calls, self.reviews, self.reused = 0.0, 0, 0, 0
            self._recent = deque()
            self._outage = None  # (reason, until): the API account cannot be used right now

    def pause(self, reason, seconds):
        with self.lock:
            self._outage = (reason, time.time() + seconds)

    @staticmethod
    def limit():
        return float(os.environ.get("BRANDGUARD_AI_BUDGET_USD", "1.00"))

    @staticmethod
    def per_hour():
        return int(os.environ.get("BRANDGUARD_AI_MAX_PER_HOUR", "30"))

    def _blocked(self):
        if self._outage and time.time() < self._outage[1]:
            return (f"AI is unavailable right now ({self._outage[0]}). Rule checks and fact checks still ran in code, "
                    "and BrandGuard works fully in rules-only mode.")
        if self.spent >= self.limit():
            return (f"The AI budget for this demo is used up (${self.spent:.2f} of ${self.limit():.2f}). "
                    "Rule checks and fact checks still ran in code.")
        cutoff = time.time() - 3600
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        if len(self._recent) >= self.per_hour():
            return (f"AI review is limited to {self.per_hour()} new reviews per hour on this demo. "
                    "Rule checks and fact checks still ran in code; try again later.")
        return None

    def try_start(self):
        """Reserve one review slot. Returns None when allowed, otherwise the reason it is not."""
        with self.lock:
            reason = self._blocked()
            if reason is None:
                self._recent.append(time.time())
                self.reviews += 1
            return reason

    def add(self, model, usage):
        cost = cost_usd(model, usage)
        with self.lock:
            self.spent += cost
            self.calls += 1
        return cost

    def count_reuse(self):
        with self.lock:
            self.reused += 1

    def exhausted(self):
        with self.lock:
            return self.spent >= self.limit()

    def snapshot(self):
        with self.lock:
            return {"spent_usd": round(self.spent, 4), "limit_usd": self.limit(), "reviews": self.reviews,
                    "reused": self.reused, "api_calls": self.calls, "per_hour": self.per_hour(),
                    "paused": self._blocked(), "outage": bool(self._outage and time.time() < self._outage[1])}


budget = Budget()
_results = OrderedDict()  # review cache: identical input -> the identical, already paid-for result
_results_lock = threading.Lock()


def _cache_key(text, intent, kit):
    kit_data = {k: v for k, v in kit.items() if k != "version"}
    blob = json.dumps([MODEL, text, intent, kit_data, date.today().isoformat()], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def clear_cache():
    with _results_lock:
        _results.clear()


class _Run:
    """State for one review: what the tools returned, so citations can be validated."""

    def __init__(self, text, intent, kit, rule_findings):
        self.text, self.intent, self.kit = text, intent, kit
        self.rule_findings = rule_findings
        self.returned_ids = {f["rule_id"] for f in rule_findings}
        self.ai_findings, self.rejected, self.tool_log = [], [], []
        self.rewrite, self.rewrite_check = None, None
        self.attempts, self.clean = 0, False

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

    def add_finding(self, rule_id, quote, severity, explanation, suggested_fix, fix_kind="replace"):
        if rule_id not in self.returned_ids:
            self.rejected.append({"rule_id": rule_id, "reason": "cited an id no tool returned"})
            return {"ok": False, "error": f"{rule_id} was not returned by search_rules; search first, then cite."}
        start = self.text.find(quote) if quote else len(self.text)
        if quote and start < 0:
            self.rejected.append({"rule_id": rule_id, "reason": "quote not found in content"})
            return {"ok": False, "error": "quote must be copied exactly from the content"}
        end = start + len(quote)
        if any(f["rule_id"] == rule_id and (f["quote"] == quote or (quote and f["start"] < end and start < f["end"]))
               for f in self.rule_findings + self.ai_findings):
            return {"ok": False, "error": "already reported: overlaps an existing finding for the same rule"}
        # Policy guard: AI judgment is advisory. Only exact checks in code can raise a critical (approval-blocking) issue,
        # and an automatic fix must be publishable text, not an instruction or a placeholder.
        severity = "minor" if severity == "minor" else "major"
        tip = (suggested_fix or "").strip()
        fix, fix_note = None, None
        if not quote:
            fix_note = tip or None
        elif fix_kind == "remove" or (fix_kind == "replace" and _REMOVE.match(tip)):
            fix = ""
        elif fix_kind == "replace" and tip and not _PLACEHOLDER.search(tip) and not _INSTRUCTION.match(tip):
            fix = tip
        else:
            fix_note = tip or None
        self.ai_findings.append(engine._finding(
            rule_id, next((r["category"] for r in self.kit["rules"] if r["id"] == rule_id), "messaging"),
            severity, start, start + len(quote), quote, explanation, fix=fix, fix_note=fix_note, source="ai"))
        return {"ok": True, "severity": severity, "automatic_fix": fix is not None}

    def submit_rewrite(self, text):
        """Blocking problems (intent lost, unapproved numbers, critical/major rules) decide whether the rewrite may be
        shown. Minor rules and the agent's own unaddressed findings are fed back so it can polish, without blocking."""
        self.attempts += 1
        check = engine.verify_intent(self.text, text, self.intent, self.kit)
        remaining, _ = engine.run_rule_checks(text, self.kit, self.intent)
        blocking = check["problems"] + [f"still violates {f['rule_id']}: {f['quote']}" for f in remaining
                                        if f["severity"] in ("critical", "major")]
        polish = [f"still violates {f['rule_id']} (minor): {f['quote']}" for f in remaining if f["severity"] == "minor"]
        polish += [f"still contains “{f['quote']}”, which you flagged under {f['rule_id']}" for f in self.ai_findings
                   if len(f["quote"]) >= 4 and f["quote"] in text]
        if not blocking or self.rewrite is None or not self.rewrite_check["ok"]:
            self.rewrite, self.rewrite_check = text, {"ok": not blocking, "problems": blocking}  # keep the best version
        self.clean = not blocking and not polish
        todo = blocking + polish
        return {"ok": not todo, "problems": todo,
                "note": "Fix these and submit again." if todo else "Verified. You are done."}

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
            "rewrite": rewrite, "rewrite_check": check, "tool_log": [], "rejected": [], "cost_usd": 0.0}


def review_with_ai(text, intent, kit, rule_findings, verified):
    """Run the Claude Review Agent. Returns dict with ai findings, rewrite and an auditable tool log."""
    if not ai_available():
        return _offline_result(text, intent, kit, rule_findings,
                               "AI review is off (no API key configured). Rule checks and fact checks ran in code.")
    if len(text) > AI_MAX_CHARS:
        return _offline_result(text, intent, kit, rule_findings,
                               f"AI review runs on texts up to {AI_MAX_CHARS:,} characters to keep costs low. "
                               "Rule checks and fact checks ran in code on the full text.")
    key = _cache_key(text, intent, kit)
    with _results_lock:
        hit = _results.get(key)
        if hit is not None:
            _results.move_to_end(key)
    if hit is not None:
        budget.count_reuse()
        result = copy.deepcopy(hit)
        result.update(cached=True, cost_usd=0.0)
        return result
    blocked = budget.try_start()
    if blocked:
        return _offline_result(text, intent, kit, rule_findings, blocked)

    import anthropic

    client = make_client()
    run = _Run(text, intent, kit, rule_findings)
    brief = {
        "content": text, "intent": intent, "brand": kit["brand"]["name"], "voice": kit["brand"]["voice"],
        "code_findings": [{"rule_id": f["rule_id"], "quote": f["quote"], "message": f["message"], "fix": f["fix"]}
                          for f in rule_findings],
        "verified_claims": verified,
    }
    messages = [{"role": "user", "content": "Review this content.\n" + json.dumps(brief, ensure_ascii=False)}]
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    calls, spent, note = 0, 0.0, None
    try:
        started = time.time()
        for turn in range(MAX_TURNS):
            if turn and time.time() - started > REVIEW_DEADLINE:
                note = "The AI review hit its time limit; the remaining AI checks were skipped."
                break
            if turn and budget.exhausted():
                note = "The AI budget ran out during this review; the remaining AI checks were skipped."
                break
            response = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS, system=system, tools=TOOLS, messages=messages,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},  # automatic caching of the growing tool loop
            )
            spent += budget.add(MODEL, response.usage)
            # refusal, max_tokens or a final answer: stop here (a cut-off tool call is never executed)
            if response.stop_reason != "tool_use":
                break
            messages.append({"role": "assistant", "content": response.content})  # thinking blocks kept unmodified
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
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
            if run.clean or run.attempts >= MAX_REWRITES:
                break  # clean rewrite (or attempts used up): no need to pay for a closing message
    except anthropic.APIStatusError as exc:
        log.warning("Review Agent API error %s: %s", exc.status_code, exc.message)
        return _offline_result(text, intent, kit, rule_findings, f"AI review unavailable ({api_error(exc)}); rule checks only.")
    except anthropic.APIConnectionError:
        log.warning("Review Agent could not reach the API")
        return _offline_result(text, intent, kit, rule_findings, "AI review unavailable (network); rule checks only.")

    rewrite, check = run.rewrite, run.rewrite_check
    if rewrite is None or not check["ok"]:
        # The policy guard never shows an unverified rewrite; fall back to the code-applied fixes.
        fallback = engine.apply_fixes(text, rule_findings)
        rewrite, check = fallback, engine.verify_intent(text, fallback, intent, kit)
    result = {"mode": "ai", "model": MODEL, "status": "done", "note": note, "findings": run.ai_findings,
              "rewrite": rewrite, "rewrite_check": check, "tool_log": run.tool_log, "rejected": run.rejected,
              "cost_usd": round(spent, 5), "cached": False}
    if note is None:
        with _results_lock:
            _results[key] = copy.deepcopy(result)
            while len(_results) > CACHE_SIZE:
                _results.popitem(last=False)
    return result
