"""Deterministic checking tools used by the Review Agent.

Every function here is plain code: same input, same output, and every finding
names the Brand Kit item (rule, glossary term or fact) it is based on. The
agent may add AI judgment on top, but these tools are the source of evidence.
"""
import re
import uuid
from datetime import date

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}

# Obvious one-to-one replacements for text-speak and hype (TONE-01/02).
SIMPLE_FIXES = {
    "gonna": "going to", "wanna": "want to", "u r": "you are", "pls": "please",
    "plz": "please", "luv": "love",
    # CLAIM-03: describe the offer instead of promising an outcome
    "guarantee": "offer", "guaranteed": "", "100% placement": "placement support",
    "assured job": "career support", "job assured": "career support", "sure-shot": "", "sure shot": "",
}
CAPS_ALLOWLIST = {"AI", "ML", "UI", "UX", "HTML", "CSS", "API", "FAQ", "CTA", "PDF", "DSA", "IIT", "NIT", "JIIT", "USA", "UK", "OK", "CEO", "HR"}
MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december"


def _fid():
    return uuid.uuid4().hex[:8]


def _boundary_pattern(phrase):
    """Whole-word match that also works for phrases starting or ending with symbols (#1, !!!)."""
    left = r"(?<![\w])" if re.match(r"\w", phrase) else ""
    right = r"(?![\w])" if re.search(r"\w$", phrase) else ""
    return left + re.escape(phrase) + right


def _finding(rule_id, category, severity, start, end, quote, message, fix=None, fix_note=None, cited=None, source="rules"):
    return {
        "id": _fid(), "rule_id": rule_id, "category": category, "severity": severity,
        "start": start, "end": end, "quote": quote, "message": message,
        "fix": fix, "fix_note": fix_note, "cited": cited or [rule_id], "source": source,
    }


def _match_case(original, replacement):
    if original[:1].isupper() and replacement[:1].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


# ---------------------------------------------------------------- tools

def check_glossary(text, kit):
    """Wrong spellings or variants of glossary terms (TERM-xx)."""
    out = []
    for term in kit["glossary"]:
        preferred = term["preferred"]
        case_sensitive = preferred != preferred.lower()
        phrases = sorted({preferred, *term["variants"]}, key=len, reverse=True)
        regex = re.compile("|".join(_boundary_pattern(p) for p in phrases), re.IGNORECASE)
        for m in regex.finditer(text):
            found = m.group(0)
            ok = found == preferred if case_sensitive else found.lower() == preferred.lower()
            if ok:
                continue
            fix = preferred if case_sensitive else _match_case(found, preferred)
            out.append(_finding(term["id"], "terminology", "minor", m.start(), m.end(), found,
                                f"Use “{preferred}” instead of “{found}”. {term.get('note', '')}".strip(),
                                fix=fix, cited=[term["id"]]))
    return out


def check_patterns(text, kit):
    """Rule phrases that are never allowed: hype, slang, pressure, superlatives, guarantees, 'click here'."""
    out = []
    for rule in kit["rules"]:
        for phrase in rule.get("patterns", []):
            for m in re.finditer(_boundary_pattern(phrase), text, re.IGNORECASE):
                start, end = m.start(), m.end()
                fix = SIMPLE_FIXES.get(m.group(0).lower())
                fix_note = None
                if rule["id"] == "TONE-03":
                    end += len(re.match(r"[!.]*", text[end:]).group(0))  # remove "LAST CHANCE!!!" as one unit
                    fix, fix_note = "", "Remove the pressure phrase; give a real end date instead (see approved offer facts)."
                elif rule["id"] == "CLAIM-02":
                    fix, fix_note = "", "Remove the ranking, or replace it with an approved fact (for example the number of students)."
                elif rule["id"] == "CLAIM-03":
                    fix_note = "Describe what you offer (for example 'placement support') instead of promising a result."
                elif rule["id"] == "ACC-02":
                    fix_note = "Say where the link goes, for example 'Register on our website'."
                out.append(_finding(rule["id"], rule["category"], rule["severity"], start, end, text[start:end],
                                    f"{rule['title']}. {rule['text']}", fix=fix, fix_note=fix_note))
    pressure = [(f["start"], f["end"]) for f in out if f["rule_id"] == "TONE-03"]
    for m in re.finditer(r"!{2,}", text):
        if any(s <= m.start() < e for s, e in pressure):
            continue
        out.append(_finding("TONE-03", "tone", "major", m.start(), m.end(), m.group(0),
                            "No pressure tactics: avoid rows of exclamation marks.", fix="!"))
    return out


def check_caps(text, taken):
    """All-caps words (ACC-03), skipping spans another finding already covers."""
    out = []
    for m in re.finditer(r"\b[A-Z]{3,}\b", text):
        word = m.group(0)
        if word in CAPS_ALLOWLIST or any(s <= m.start() < e for s, e in taken):
            continue
        out.append(_finding("ACC-03", "accessibility", "minor", m.start(), m.end(), word,
                            "Avoid ALL-CAPS words: they read as shouting and some screen readers spell them out.",
                            fix=word.capitalize()))
    return out


def check_readability(text):
    """Sentences longer than 25 words (ACC-01)."""
    out = []
    for m in re.finditer(r"[^.!?]+[.!?]*", text):
        sentence = m.group(0).strip()
        words = re.findall(r"\b\w[\w'-]*\b", sentence)
        if len(words) > 25:
            start = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
            out.append(_finding("ACC-01", "accessibility", "minor", start, start + len(sentence), sentence,
                                f"This sentence has {len(words)} words. Keep sentences under 25 words.",
                                fix_note="Split it into two or three short sentences."))
    return out


NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s?(?:%|k\b|K\b|\+|/5|out of 5))?")


def extract_claims(text):
    """Numbers used as promotional claims, with the words around them."""
    claims = []
    for m in NUMBER_RE.finditer(text):
        raw, suffix = m.group(1), (m.group(2) or "").strip()
        after = text[m.end():m.end() + 12].lower()
        if re.match(rf"\s*({MONTHS})\b", after) or re.fullmatch(r"(19|20)\d\d", raw):
            continue  # dates and years are not claims
        if re.match(r"\s*(a\.?m\.?|p\.?m\.?)\b|:\d\d", after) or re.search(r"\d:$", text[max(0, m.start() - 3):m.start()]):
            continue  # clock times ("10 AM", "6:30") are not claims
        digits_around = re.sub(r"[\s-]", "", text[max(0, m.start() - 15):m.end() + 15])
        if re.search(r"\+?\d{8,}", digits_around) and re.search(r"(call|phone|whatsapp|mobile|contact|\+\d)",
                                                              text[max(0, m.start() - 40):m.end() + 40], re.I) \
                or len(re.sub(r"\D", "", raw)) >= 8:
            continue  # phone numbers are not claims
        value = float(raw.replace(",", ""))
        if suffix.lower() == "k":
            value *= 1000
        window = text[max(0, m.start() - 60):m.end() + 60].lower()
        claims.append({"start": m.start(), "end": m.end(), "quote": m.group(0), "number_text": raw,
                       "value": value, "suffix": suffix, "context": window})
    return claims


def _unit_ok(claim, fact):
    suffix = claim["suffix"].lower()
    if suffix == "%":
        return fact["unit"] == "%"
    if suffix in ("/5", "out of 5"):
        return fact["unit"] == "rating"
    return fact["unit"] not in ("%",)


def _format_like(number_text, value):
    if float(value).is_integer():
        value = int(value)
        return f"{value:,}" if "," in number_text or value >= 10000 else str(value)
    return str(value)


def lookup_fact(claim, kit, today=None):
    """Classify a numeric claim against approved facts: supported, outdated, contradicted or no_source."""
    today = today or date.today().isoformat()
    scored = []
    for fact in kit["facts"]:
        score = sum(1 for k in fact["keywords"] if k.lower() in claim["context"])
        if score and _unit_ok(claim, fact):
            scored.append((score, fact))
    scored.sort(key=lambda sf: -sf[0])
    valid = [f for _, f in scored if f["valid_until"] >= today]
    expired = [f for _, f in scored if f["valid_until"] < today]
    value = claim["value"]
    at_least = claim["suffix"] == "+"
    for fact in valid:
        if abs(fact["value"] - value) < 1e-9 or (at_least and value <= fact["value"]):
            return {"status": "supported", "fact": fact}
    for fact in expired:
        if abs(fact["value"] - value) < 1e-9:
            current = next((f for f in valid if f["unit"] == fact["unit"]), None)
            return {"status": "outdated", "fact": fact, "current": current}
    if valid:
        return {"status": "contradicted", "fact": valid[0]}
    return {"status": "no_source", "fact": None}


def check_claims(text, kit, taken, today=None):
    """CLAIM-01 findings plus a list of verified claims (claim provenance)."""
    findings, verified = [], []
    for claim in extract_claims(text):
        if any(s <= claim["start"] < e for s, e in taken):
            continue  # already covered by a guarantee or superlative finding
        result = lookup_fact(claim, kit, today)
        fact = result["fact"]
        span = (claim["start"], claim["start"] + len(claim["number_text"]))
        if result["status"] == "supported":
            verified.append({"quote": claim["quote"], "fact_id": fact["id"], "statement": fact["statement"],
                             "value": fact["value"], "source": fact["source"], "valid_until": fact["valid_until"]})
        elif result["status"] == "contradicted":
            fix = _format_like(claim["number_text"], fact["value"])
            findings.append(_finding("CLAIM-01", "claims", "critical", *span, claim["number_text"],
                                     f"“{claim['quote']}” does not match the approved fact {fact['id']}: "
                                     f"{fact['statement']} (source: {fact['source']}).",
                                     fix=fix, cited=["CLAIM-01", fact["id"]]))
        elif result["status"] == "outdated":
            current = result.get("current")
            msg = (f"“{claim['quote']}” matches {fact['id']}, which expired on {fact['valid_until']}.")
            fix = None
            cited = ["CLAIM-01", fact["id"]]
            if current:
                msg += f" The current approved figure is {current['id']}: {current['statement']}"
                fix = _format_like(claim["number_text"], current["value"])
                cited.append(current["id"])
            findings.append(_finding("CLAIM-01", "claims", "critical", *span, claim["number_text"], msg,
                                     fix=fix, cited=cited))
        else:
            findings.append(_finding("CLAIM-01", "claims", "critical", claim["start"], claim["end"], claim["quote"],
                                     f"No approved fact supports “{claim['quote']}”. Add a fact to the Brand Kit "
                                     "or remove the number.", fix_note="Remove the number or ask the fact owner to approve it."))
    return findings, verified


def normalise(text, kit):
    """Text with glossary variants replaced by preferred terms (used for intent checks)."""
    out = text
    for term in kit["glossary"]:
        for v in sorted(term["variants"], key=len, reverse=True):
            out = re.sub(_boundary_pattern(v), term["preferred"], out, flags=re.IGNORECASE)
    return out.lower()


def check_cta(text, intent, kit):
    """MSG-01: the creator's stated call to action must be present."""
    cta = (intent or {}).get("cta", "").strip().lower()
    if not cta or cta in normalise(text, kit):
        return []
    end = len(text)
    return [_finding("MSG-01", "messaging", "major", end, end, "",
                     f"The stated call to action “{cta}” is missing. End with one clear next step.",
                     fix_note=f"Add a closing line such as “{cta.capitalize()} today.”")]


def verify_intent(original, rewrite, intent, kit):
    """A rewrite must keep the call to action and the brand name, and must not invent numbers."""
    problems = []
    cta = (intent or {}).get("cta", "").strip().lower()
    if cta and cta in normalise(original, kit) and cta not in normalise(rewrite, kit):
        problems.append(f"call to action “{cta}” was removed")
    brand = kit["brand"]["name"].lower()
    if brand in normalise(original, kit) and brand not in normalise(rewrite, kit):
        problems.append("brand name was removed")
    allowed = {f["value"] for f in kit["facts"]}
    for claim in extract_claims(rewrite):
        if claim["value"] not in allowed and claim["quote"] not in original:
            problems.append(f"new number “{claim['quote']}” is not an approved fact")
    return {"ok": not problems, "problems": problems}


def apply_fixes(text, findings):
    """Apply every finding that has a concrete replacement, from the end of the text backwards."""
    fixable = sorted((f for f in findings if f.get("fix") is not None and f["end"] > f["start"]),
                     key=lambda f: f["start"], reverse=True)
    out, last_start = text, len(text) + 1
    for f in fixable:
        if f["end"] > last_start:
            continue  # overlapping span; the earlier (rightmost) fix wins
        out = out[:f["start"]] + f["fix"] + out[f["end"]:]
        last_start = f["start"]
    return tidy(out)


def tidy(text):
    """Clean up gaps left by removed phrases without changing the author's own punctuation."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([.,!?])", r"\1", text)
    text = re.sub(r",([.!?])", r"\1", text)
    text = re.sub(r"([.!?])[ \t]*\.(?!\.)", r"\1", text)
    return text.strip()


def run_rule_checks(text, kit, intent=None, today=None):
    """All deterministic checks; returns (findings, verified_claims)."""
    findings = check_patterns(text, kit) + check_glossary(text, kit)
    taken = [(f["start"], f["end"]) for f in findings if f["rule_id"] in ("CLAIM-02", "CLAIM-03", "TONE-03")]
    claim_findings, verified = check_claims(text, kit, taken, today)
    findings += claim_findings
    findings += check_caps(text, [(f["start"], f["end"]) for f in findings])
    findings += check_readability(text)
    findings += check_cta(text, intent, kit)
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["start"]))
    return findings, verified
