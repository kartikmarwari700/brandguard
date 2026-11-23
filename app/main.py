"""BrandGuard web app: JSON API under /api and the single-page UI under /."""
import io
import os
import re
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, clarity, images, store

app = FastAPI(title="BrandGuard API", version="1.0.0",
              description="Explainable brand governance before publishing. Team Cite or Flag, CodeZen India 2026.")
store.connect(os.environ.get("BRANDGUARD_DB", "brandguard.db"))


@app.exception_handler(store.WorkflowError)
def workflow_error(_: Request, exc: store.WorkflowError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


FIELD_NAMES = {"content": "Content", "title": "Title", "text": "Text", "reason": "Reason", "statement": "Statement",
               "value": "Value", "unit": "Unit", "source": "Source", "valid_until": "Valid until", "preferred": "Preferred term"}


@app.exception_handler(RequestValidationError)
def validation_error(_: Request, exc: RequestValidationError):
    """Plain-language validation messages instead of raw JSON."""
    msgs = []
    for e in exc.errors():
        field = next((str(x) for x in reversed(e.get("loc", [])) if isinstance(x, str) and x != "body"), "input")
        name = FIELD_NAMES.get(field, field.replace("_", " ").capitalize())
        if e.get("type") == "string_too_long":
            msgs.append(f"{name} is too long (max {e.get('ctx', {}).get('max_length')} characters).")
        elif e.get("type") == "missing":
            msgs.append(f"{name} is required.")
        elif "pattern" in e.get("type", ""):
            msgs.append(f"{name} has the wrong format" + (" (use YYYY-MM-DD)." if field == "valid_until" else "."))
        else:
            msgs.append(f"{name}: {e.get('msg', 'invalid value')}.")
    return JSONResponse({"detail": " ".join(dict.fromkeys(msgs)) or "Invalid input."}, status_code=422)


WS_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")


@app.middleware("http")
async def workspace(request: Request, call_next):
    """Each browser sends its own workspace id; the API docs and other clients share the 'public' workspace."""
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    raw = request.headers.get("x-workspace", "")
    token = store.use_workspace(raw if WS_PATTERN.fullmatch(raw) else "public")
    try:
        await run_in_threadpool(store.ensure_workspace)
        return await call_next(request)
    finally:
        store.reset_workspace(token)


def role_of(x_role: Optional[str]):
    role = (x_role or "creator").lower()
    if role not in store.ROLES:
        raise HTTPException(400, "Unknown role")
    return role


class Intent(BaseModel):
    purpose: str = ""
    audience: str = ""
    cta: str = ""


class NewSubmission(BaseModel):
    title: str = Field("", max_length=120)
    channel: str = Field("Social post", max_length=60)
    content: str = Field(..., max_length=8000)
    intent: Intent = Intent()


MAX_IMAGE_READ = images.MAX_UPLOAD + 1


class TextBody(BaseModel):
    text: str = Field("", max_length=8000)
    alt: Optional[str] = Field(None, max_length=300)


class FindingBody(BaseModel):
    finding_id: str
    reason: str = Field("", max_length=500)


class Fact(BaseModel):
    statement: str = Field(..., max_length=300)
    value: float
    unit: str = Field(..., max_length=30)
    keywords: List[str] = []
    source: str = Field(..., max_length=120)
    owner: str = Field("", max_length=60)
    valid_until: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class Rule(BaseModel):
    category: str = Field("tone", pattern=r"^(tone|claims|messaging|accessibility|terminology|visual)$")
    severity: str = Field("major", pattern=r"^(critical|major|minor)$")
    title: str = Field(..., max_length=120)
    text: str = Field(..., max_length=500)
    good: str = Field("", max_length=200)
    bad: str = Field("", max_length=200)
    patterns: List[str] = []


class Term(BaseModel):
    preferred: str = Field(..., max_length=60)
    variants: List[str] = []
    note: str = Field("", max_length=200)


@app.get("/api/status")
def status():
    kit = store.get_kit()
    on = agent.ai_available()
    return {"ai": on, "model": agent.MODEL if on else None, "ai_budget": agent.budget.snapshot() if on else None,
            "brand": kit["brand"]["name"], "kit_version": kit["version"]}


@app.get("/api/kit")
def kit():
    return store.get_kit()


@app.post("/api/kit/{kind}")
def add_kit_item(kind: str, body: dict, x_role: Optional[str] = Header(None)):
    model = {"facts": Fact, "rules": Rule, "glossary": Term}.get(kind)
    if not model:
        raise HTTPException(404, "Unknown Brand Kit section")
    return store.add_kit_item(kind, model(**body).model_dump(), role_of(x_role))


class FactPatch(BaseModel):
    statement: Optional[str] = Field(None, max_length=300)
    value: Optional[float] = None
    valid_until: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    source: Optional[str] = Field(None, max_length=120)
    owner: Optional[str] = Field(None, max_length=60)


@app.put("/api/kit/facts/{fact_id}")
def update_fact(fact_id: str, body: FactPatch, x_role: Optional[str] = Header(None)):
    return store.update_fact(fact_id, body.model_dump(), role_of(x_role))


@app.get("/api/kit/usage")
def fact_usage():
    return store.fact_usage()


@app.delete("/api/kit/{kind}/{item_id}")
def delete_kit_item(kind: str, item_id: str, x_role: Optional[str] = Header(None)):
    if kind not in ("facts", "rules", "glossary"):
        raise HTTPException(404, "Unknown Brand Kit section")
    return store.delete_kit_item(kind, item_id, role_of(x_role))


@app.get("/api/submissions")
def submissions():
    return store.list_submissions()


@app.post("/api/submissions")
def new_submission(body: NewSubmission, x_role: Optional[str] = Header(None)):
    sid = store.create_submission(body.title, body.channel, body.content, body.intent.model_dump(), role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/image")
async def new_image_submission(file: UploadFile = File(...), title: str = Form("", max_length=120),
                               channel: str = Form("Instagram post", max_length=60), caption: str = Form("", max_length=4000),
                               alt: str = Form("", max_length=300), purpose: str = Form("", max_length=200),
                               audience: str = Form("", max_length=200), cta: str = Form("", max_length=100),
                               x_role: Optional[str] = Header(None)):
    role = role_of(x_role)
    media = await run_in_threadpool(images.load_upload, await file.read(MAX_IMAGE_READ), alt)
    intent = {"purpose": purpose, "audience": audience, "cta": cta}
    sid = await run_in_threadpool(store.create_submission, title or file.filename or "Image", channel, caption, intent, role,
                                  True, media)
    return await run_in_threadpool(store.get_submission, sid)


@app.post("/api/submissions/{sid}/image")
async def replace_image(sid: int, file: UploadFile = File(...), x_role: Optional[str] = Header(None)):
    role = role_of(x_role)
    cur = await run_in_threadpool(store.get_submission, sid)
    if cur["kind"] != "image":
        raise HTTPException(400, "This content has no image.")
    media = await run_in_threadpool(images.load_upload, await file.read(MAX_IMAGE_READ), (cur["current"]["media"] or {}).get("alt", ""))
    await run_in_threadpool(store.edit_content, sid, cur["current"]["content"], role, media)
    return await run_in_threadpool(store.get_submission, sid)


@app.get("/api/submissions/{sid}")
def submission(sid: int):
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/accept")
def accept(sid: int, body: FindingBody, x_role: Optional[str] = Header(None)):
    store.accept_fix(sid, body.finding_id, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/dismiss")
def dismiss(sid: int, body: FindingBody, x_role: Optional[str] = Header(None)):
    store.dismiss(sid, body.finding_id, body.reason, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/apply-rewrite")
def apply_rewrite(sid: int, x_role: Optional[str] = Header(None)):
    store.apply_rewrite(sid, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/edit")
def edit(sid: int, body: TextBody, x_role: Optional[str] = Header(None)):
    store.edit_content(sid, body.text, role_of(x_role), alt=body.alt)
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/recheck")
def recheck(sid: int, x_role: Optional[str] = Header(None)):
    store.recheck(sid, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/submit")
def submit(sid: int, x_role: Optional[str] = Header(None)):
    store.submit_for_review(sid, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/comment")
def add_comment(sid: int, body: TextBody, x_role: Optional[str] = Header(None)):
    store.comment(sid, body.text, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/request-changes")
def request_changes(sid: int, body: TextBody, x_role: Optional[str] = Header(None)):
    store.request_changes(sid, body.text, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/submissions/{sid}/approve")
def approve(sid: int, body: TextBody, x_role: Optional[str] = Header(None)):
    store.approve(sid, body.text, role_of(x_role))
    return store.get_submission(sid)


@app.post("/api/extract-docx")
async def extract_docx(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "Please upload a .docx file.")
    data = await file.read()
    if len(data) > 2_000_000:
        raise HTTPException(413, "File is larger than 2 MB.")
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "Could not read this .docx file.")
    text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
    return {"text": text[:8000], "truncated": len(text) > 8000}


@app.get("/api/audit")
def audit_log():
    return store.global_audit()


@app.get("/api/insights")
def insights():
    return store.rule_insights()


class RuleEdit(BaseModel):
    title: Optional[str] = Field(None, max_length=120)
    text: Optional[str] = Field(None, max_length=500)
    good: Optional[str] = Field(None, max_length=200)
    bad: Optional[str] = Field(None, max_length=200)
    patterns: Optional[List[str]] = None


@app.get("/api/clarity")
def clarity_scores():
    return {"scores": clarity.scores(), "proposals": clarity.list_proposals(),
            "min_decisions": clarity.MIN_DECISIONS, "unclear_rate": clarity.UNCLEAR_DISMISS_RATE}


@app.post("/api/clarity/{rule_id}/draft")
def clarity_draft(rule_id: str, x_role: Optional[str] = Header(None)):
    return clarity.draft_rule(rule_id, role_of(x_role))


@app.get("/api/clarity/proposals/{pid}")
def clarity_proposal(pid: int):
    return clarity.get_proposal(pid)


@app.post("/api/clarity/proposals/{pid}/apply")
def clarity_apply(pid: int, body: RuleEdit, x_role: Optional[str] = Header(None)):
    clarity.apply_proposal(pid, body.model_dump(), role_of(x_role))
    return clarity.get_proposal(pid)


@app.post("/api/clarity/proposals/{pid}/discard")
def clarity_discard(pid: int, x_role: Optional[str] = Header(None)):
    clarity.discard_proposal(pid, role_of(x_role))
    return clarity.get_proposal(pid)


@app.post("/api/reset")
def reset():
    store.seed(background=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="static")
