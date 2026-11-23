"""Brand Kit import from existing guideline documents (improvement 3).

A manager uploads the brand guideline PDF or Word file the organisation already has. The Review Agent
proposes rules, glossary terms and approved facts, each with the page and the exact quote it came from.
Code keeps a candidate only if that quote really is on that page (no invented guidelines), flags items
that duplicate the current Brand Kit, and nothing enters the Brand Kit until a manager accepts it.
"""
import io
import json
import re
from datetime import date, timedelta

from . import agent, store

MAX_FILE = 5_000_000
MAX_PAGES = 40
MAX_CHARS = 40_000
CATEGORIES = ("tone", "claims", "messaging", "accessibility", "terminology", "visual")


def _err(msg, status=400):
    return store.WorkflowError(msg, status)


# ---------------------------------------------------------------- reading the document

def extract_pages(filename, data):
    name = (filename or "").lower()
    if len(data) > MAX_FILE:
        raise _err("The file is larger than 5 MB.")
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise _err("This PDF is password-protected. Please upload an unprotected copy.")
            pages = [(f"page {i + 1}", (p.extract_text() or "").strip()) for i, p in enumerate(reader.pages[:MAX_PAGES])]
        except (PdfReadError, ValueError, KeyError, OSError):
            raise _err("Could not read this PDF.")
    elif name.endswith(".docx"):
        import docx

        try:
            document = docx.Document(io.BytesIO(data))
        except Exception:
            raise _err("Could not read this Word file.")
        paras = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        for table in document.tables:  # guideline tables (for example "use / don't use") matter here
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    paras.append(" | ".join(dict.fromkeys(cells)))
        pages = [(f"part {i // 20 + 1}", "\n".join(paras[i:i + 20])) for i in range(0, len(paras), 20)]
    else:
        raise _err("Please upload a PDF or a Word (.docx) file.")
    pages = [(label, text) for label, text in pages if text]
    if not pages:
        raise _err("No text was found in this file. Scanned documents (images of text) are not supported yet.")
    total, kept = 0, []
    for label, text in pages:
        if total >= MAX_CHARS:
            break
        kept.append((label, text[:MAX_CHARS - total]))
        total += len(kept[-1][1])
    return kept


def _norm(s):
    """Compare quotes without quote marks or any whitespace: PDF text wraps lines and splits words at hyphens."""
    return re.sub(r"\s+", "", re.sub(r"[“”\"'‘’]", "", s or "")).lower()


# ---------------------------------------------------------------- candidates

ITEM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "page", "quote", "title", "text", "category", "severity", "patterns", "preferred", "variants",
                 "statement", "value", "unit", "valid_until"],
    "properties": {
        "kind": {"type": "string", "enum": ["rule", "term", "fact"]},
        "page": {"type": "string", "description": "The page label exactly as given, e.g. 'page 2'"},
        "quote": {"type": "string", "description": "The exact sentence from that page this item comes from"},
        "title": {"type": "string", "description": "rule: short title; otherwise empty"},
        "text": {"type": "string", "description": "rule: the rule in one or two plain sentences; otherwise empty"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
        "patterns": {"type": "array", "items": {"type": "string"}, "description": "rule: phrases that always break it, if the document names any"},
        "preferred": {"type": "string", "description": "term: the correct word or spelling; otherwise empty"},
        "variants": {"type": "array", "items": {"type": "string"}, "description": "term: wrong variants named in the document"},
        "statement": {"type": "string", "description": "fact: the approved fact as one sentence; otherwise empty"},
        "value": {"type": "number", "description": "fact: the number; otherwise 0"},
        "unit": {"type": "string", "description": "fact: unit such as students, %, weeks; otherwise empty"},
        "valid_until": {"type": "string", "description": "fact: YYYY-MM-DD if the document gives an end date; otherwise empty"},
    },
}
EXTRACT_TOOL = {"name": "propose_items", "strict": True,
                "description": "Submit every brand rule, glossary term and approved fact found in the document. Call once.",
                "input_schema": {"type": "object", "additionalProperties": False, "required": ["items"],
                                 "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}}}}
EXTRACT_PROMPT = """You turn an organisation's brand guideline document into structured Brand Kit items.
Find: rules (do / don't instructions about tone, claims, messaging, accessibility, terminology or visual identity),
glossary terms (a preferred word or spelling and the wrong variants the document names), and approved facts (numbers the
organisation states as true, with their unit and end date if given). For every item give the page label and copy the
exact sentence it comes from as the quote. Only use what the document says; do not invent rules or numbers.
Severity: critical for legal or factual risks (guarantees, unverified claims), major for most rules, minor for style.
Call propose_items once with all items."""


def _ai_extract(pages, kit):
    blocked = agent.budget.try_start()
    if blocked:
        return None, blocked, 0.0
    doc = "\n\n".join(f"=== {label} ===\n{text}" for label, text in pages)
    import anthropic

    try:
        response = agent.make_client().messages.create(
            model=agent.MODEL, max_tokens=8000, system=EXTRACT_PROMPT, tools=[EXTRACT_TOOL], output_config={"effort": "low"},
            messages=[{"role": "user", "content": f"Brand: {kit['brand']['name']}\n\n{doc}"}])
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        return None, f"AI extraction unavailable ({getattr(exc, 'status_code', 'network')}); simple extraction in code was used.", 0.0
    cost = agent.budget.add(agent.MODEL, response.usage)
    call = next((b for b in response.content if b.type == "tool_use" and b.name == "propose_items"), None)
    if not call:
        return None, "The AI returned no items; simple extraction in code was used.", cost
    return list(call.input.get("items") or [])[:80], None, cost


def _code_extract(pages):
    """Without AI: 'Use X, not Y' terms and do/don't sentences as rules. Facts need the AI."""
    items = []
    for label, text in pages:
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            s = sentence.strip()
            m = re.match(r"(?i)(?:always\s+)?(?:use|write)\s+[“\"']?([^“”\"',;]+?)[”\"']?(?:\s+as one word)?\s*,?\s*"
                         r"(?:not|never|instead of)\s+(.+)$", s)
            if m:
                variants = [v.strip(" .“”\"'") for v in re.split(r",|\bor\b", m.group(2)) if v.strip(" .“”\"'")]
                items.append({"kind": "term", "page": label, "quote": s, "preferred": m.group(1).strip(), "variants": variants})
            elif re.match(r"(?i)(do not|don't|never|avoid|always|every|no )", s) and 25 <= len(s) <= 300:
                low = s.lower()
                cat = next((c for c, words in (("claims", ("claim", "guarantee", "promise", "number", "fact")),
                                              ("accessibility", ("alt text", "contrast", "readable", "screen reader")),
                                              ("visual", ("colour", "color", "logo", "font")),
                                              ("messaging", ("call to action", "offer", "end date"))) if any(w in low for w in words)), "tone")
                items.append({"kind": "rule", "page": label, "quote": s, "title": s[:70].rstrip(" ,.;") + ("…" if len(s) > 70 else ""),
                              "text": s, "category": cat, "severity": "major", "patterns": []})
    return items


def _similar(a, b):
    wa, wb = set(re.findall(r"[a-z]{3,}", a.lower())), set(re.findall(r"[a-z]{3,}", b.lower()))
    return len(wa & wb) / max(1, len(wa | wb))


def _check(raw, pages, kit):
    """Ground each candidate in its page and shape it like a Brand Kit item; returns (item, None) or (None, reason)."""
    page_text = dict(pages)
    kind = raw.get("kind")
    label = str(raw.get("page", "")).strip().lower()
    quote = str(raw.get("quote", "")).strip()
    if label not in page_text:
        label = next((k for k, t in page_text.items() if quote and _norm(quote) in _norm(t)), label)
    if not quote or label not in page_text or _norm(quote) not in _norm(page_text[label]):
        return None, f"The quote was not found on {raw.get('page') or 'the page'} of the document."
    item = {"kind": kind, "page": label, "quote": quote[:400]}
    if kind == "rule":
        title, text = str(raw.get("title", "")).strip()[:120], str(raw.get("text", "")).strip()[:500]
        if not title or not text:
            return None, "A rule needs a title and a text."
        item["data"] = {"category": raw.get("category") if raw.get("category") in CATEGORIES else "tone",
                        "severity": raw.get("severity") if raw.get("severity") in ("critical", "major", "minor") else "major",
                        "title": title, "text": text, "good": "", "bad": "",
                        "patterns": [p.strip()[:40] for p in raw.get("patterns", []) if 2 <= len(p.strip()) <= 40][:10]}
        dup = next((r["id"] for r in kit["rules"] if _similar(r["title"] + " " + r["text"], title + " " + text) >= 0.5), None)
    elif kind == "term":
        preferred = str(raw.get("preferred", "")).strip()[:60]
        variants = [v.strip()[:60] for v in raw.get("variants", []) if v.strip() and v.strip().lower() != preferred.lower()][:8]
        if not preferred or not variants:
            return None, "A glossary term needs the preferred word and at least one wrong variant."
        item["data"] = {"preferred": preferred, "variants": variants, "note": f"From the guideline document ({label})."}
        dup = next((t["id"] for t in kit["glossary"] if t["preferred"].lower() == preferred.lower()), None)
    elif kind == "fact":
        statement, unit = str(raw.get("statement", "")).strip()[:300], str(raw.get("unit", "")).strip()[:30]
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            value = None
        if not statement or value is None or not unit:
            return None, "A fact needs a statement, a number and a unit."
        until = str(raw.get("valid_until", "")).strip()
        guessed = not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until)
        item["data"] = {"statement": statement, "value": int(value) if value.is_integer() else value, "unit": unit,
                        "keywords": sorted({w for w in re.findall(r"[a-z]{4,}", statement.lower())} - {"have", "with", "that", "from", "this", "codecraft", "academy"})[:6],
                        "source": f"Brand guideline document, {label}", "owner": "Brand team",
                        "valid_until": (date.today() + timedelta(days=365)).isoformat() if guessed else until}
        item["expiry_guessed"] = guessed
        dup = next((f["id"] for f in kit["facts"] if float(f["value"]) == value and f["unit"].lower() == unit.lower()), None)
    else:
        return None, "Unknown item type."
    item["duplicate_of"] = dup
    return item, None


def extract(filename, data, role):
    if role != "manager":
        raise _err("Only a manager can import guidelines into the Brand Kit. Switch to Manager.", 403)
    pages = extract_pages(filename, data)
    kit = store.get_kit()
    raw, note, cost, source = None, None, 0.0, "ai"
    if agent.ai_available():
        raw, note, cost = _ai_extract(pages, kit)
    else:
        note = "AI review is off, so a simple extraction in code was used (rules and terms only; facts need the AI)."
    if raw is None:
        raw, source = _code_extract(pages), "code"
    items, rejected, seen = [], [], set()
    for r in raw:
        item, reason = _check(r, pages, kit)
        if item is None:
            rejected.append({"kind": r.get("kind"), "page": r.get("page"), "quote": str(r.get("quote", ""))[:200], "reason": reason})
            continue
        key = json.dumps(item["data"], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    for i, item in enumerate(items):
        item["index"] = i
    data_ = {"filename": filename, "pages": len(pages), "chars": sum(len(t) for _, t in pages), "source": source, "note": note,
             "cost_usd": round(cost, 5), "items": items, "rejected": rejected, "kit_version": kit["version"]}
    with store._lock:
        cur = store._db.execute("INSERT INTO proposals (ws, kind, status, created_at, data) VALUES (?,?,?,?,?)",
                                (store.ws(), "import", "draft", store.now(), json.dumps(data_)))
        store._db.commit()
    store.audit(role, "kit.import_read", None, f"{filename}: {len(items)} candidate items, {len(rejected)} rejected by the quote check")
    return get_import(cur.lastrowid)


def get_import(pid):
    row = store.q("SELECT * FROM proposals WHERE id = ? AND ws = ? AND kind = 'import'", (pid, store.ws()), one=True)
    if not row:
        raise _err("Import not found. Refresh the page.", 404)
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"], **json.loads(row["data"])}


def list_imports():
    rows = store.q("SELECT id FROM proposals WHERE ws = ? AND kind = 'import' ORDER BY id DESC LIMIT 10", (store.ws(),))
    return [get_import(r["id"]) for r in rows]


def apply_import(pid, accepted, role):
    """accepted: list of {"index": i, "data": {...edited fields}}. Adds them as one new Brand Kit version."""
    if role != "manager":
        raise _err("Only a manager can change the Brand Kit. Switch to Manager.", 403)
    imp = get_import(pid)
    if imp["status"] != "draft":
        raise _err(f"This import was already {imp['status']}.", 409)
    if not accepted:
        raise _err("Select at least one item to add.")
    kit = store.get_kit()
    by_index = {it["index"]: it for it in imp["items"]}
    added = []
    for a in accepted:
        item = by_index.get(a.get("index"))
        if not item:
            raise _err("An item in this import no longer exists. Refresh the page.", 409)
        kind = {"rule": "rules", "term": "glossary", "fact": "facts"}[item["kind"]]
        data = {**item["data"], **{k: v for k, v in (a.get("data") or {}).items() if k in item["data"]}}
        if kind == "facts":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(data.get("valid_until", ""))):
                raise _err(f"Fact “{data.get('statement', '')[:40]}…” needs a valid-until date (YYYY-MM-DD).")
            try:
                v = float(data["value"])
            except (TypeError, ValueError):
                raise _err("A fact value must be a number.")
            data["value"] = int(v) if v.is_integer() else v
        new = store.clean_item(kind, {**data, "id": store.next_id(kit, kind, data), "imported_from": f"{imp['filename']}, {item['page']}"})
        kit[kind].append(new)
        added.append(new["id"])
    imp_row = store.q("SELECT data FROM proposals WHERE id = ?", (pid,), one=True)
    store.q("UPDATE proposals SET status = 'applied', data = ? WHERE id = ?",
            (json.dumps({**json.loads(imp_row["data"]), "added": added}), pid))
    store.save_kit(kit, role, "kit.import", f"{len(added)} items imported from {imp['filename']}: {', '.join(added)}")
    return added


def discard_import(pid, role):
    if role != "manager":
        raise _err("Only a manager can discard an import.", 403)
    imp = get_import(pid)
    if imp["status"] != "draft":
        raise _err(f"This import was already {imp['status']}.", 409)
    store.q("UPDATE proposals SET status = 'discarded' WHERE id = ?", (pid,))
    store.audit(role, "kit.import_discard", None, f"{imp['filename']}: import discarded")
