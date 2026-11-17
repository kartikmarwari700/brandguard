"""BrandGuard web app: JSON API under /api and the single-page UI under /."""
import io
import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, store

app = FastAPI(title="BrandGuard API", version="1.0.0",
              description="Explainable brand governance before publishing. Team Cite or Flag, CodeZen India 2026.")
store.connect(os.environ.get("BRANDGUARD_DB", "brandguard.db"))


@app.exception_handler(store.WorkflowError)
def workflow_error(_: Request, exc: store.WorkflowError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


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


class TextBody(BaseModel):
    text: str = Field("", max_length=8000)


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
    return {"ai": agent.ai_available(), "model": agent.MODEL if agent.ai_available() else None,
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
    store.edit_content(sid, body.text, role_of(x_role))
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


@app.post("/api/reset")
def reset():
    store.seed(background=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="static")
