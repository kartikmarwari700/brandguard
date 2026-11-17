# 🛡️ BrandGuard

**Explainable brand governance before you hit publish.** Team **Cite or Flag** · CodeZen India 2026 · problem statement by Inkloom AI.

**Live demo:** _link added after deployment_ · no sign-in needed · the demo brand **CodeCraft Academy** and all its facts are fictional.

Content goes in before it is published (a post, an ad, an email or a Word document). BrandGuard checks it against the
organisation's Brand Kit (rules, approved facts, terminology, brand colours). Every issue comes back with the **rule or fact
it breaks** and a **suggested correction that keeps the creator's message**, and **a person makes the final approval**.

## Try it in 2 minutes (for judges)

1. Open the live link. You start as **Creator** (role switch at the top right; no sign-in).
2. Click **Start with a sample →**. The post is highlighted: red = critical, orange = major, yellow = minor, green = a claim verified against an approved fact.
3. Each card on the right names the rule or fact (hover the ID), explains the problem and offers **Accept fix** or **Dismiss…** (a reason is required).
4. Click **Accept suggested version** for a full corrected text, then **Submit for review**.
5. Switch to **Reviewer**: you can approve only if no critical issue is open. Otherwise switch to **Manager** to sign off.
6. See **Version history** (with a word-level compare), **Activity** (audit log), **Brand Kit** (as Manager you can add a fact and it is used immediately) and **Insights** (which rules people keep dismissing).
7. **+ New content** lets you paste your own text or upload a `.docx`. **Reset demo data** in the footer restores the samples.

## What works (Round 2)

| Area | Completed |
|---|---|
| Brand Kit | Rules with IDs and good/bad examples, glossary, approved facts with source, owner and **expiry date**, brand colours; managers can add or delete items (versioned) |
| Intake | Paste text or upload `.docx`; one-line intent: purpose, audience, call to action |
| Rule checks (code) | Glossary variants and casing, hype, slang, pressure tactics, superlatives, guarantees, "click here", ALL-CAPS, sentence length, missing call to action |
| Claim checks (code) | Every number is compared with approved facts: **supported** (shown as verified), **contradicted** (fix offered), **outdated** (expired fact, current figure offered) or **no source** |
| Review Agent (AI) | With an API key, Claude plans further checks, calls Brand Kit tools (`search_rules`, `lookup_fact`, `add_finding`, `submit_rewrite`) and proposes a full rewrite. Without a key the app runs in rules-only mode and stays fully usable |
| Policy guard | AI findings must cite a rule returned by a tool and quote the text exactly; a rewrite must keep the call to action and brand name and add no unapproved numbers, otherwise it is not shown |
| Workflow | Creator / Reviewer / Manager roles; accept fix, accept suggested version, edit, dismiss with reason (critical: Manager only), submit, comment, request changes, approve (critical open: Manager sign-off only); approved content is locked |
| Traceability | Every change is a new version with a word-level diff; every action is in the audit log; dismissals per rule are counted on the Insights page |

## How it works

```
Content + intent ──► Rule checks in code ──► Review Agent (Claude + Brand Kit tools) ──► Policy guard ──► Findings + suggested version
                      glossary, phrases,        search_rules · lookup_fact ·             cited IDs only,        │
                      numbers vs facts          add_finding · submit_rewrite             intent check            ▼
                                                                                          Creator: accept / edit / dismiss
                                                                                          Reviewer / Manager: approve
```

- `app/engine.py`: deterministic tools (all checks, claim extraction and fact lookup, fixes, intent check).
- `app/agent.py`: the Review Agent (manual tool-use loop on the Anthropic Messages API, `claude-opus-5-5`, low effort, server-side refusal fallback) and the policy guard.
- `app/store.py`: SQLite persistence, versions, decisions, role checks, approvals, audit log.
- `app/main.py`: FastAPI JSON API (`/docs` has interactive API docs) and static hosting of the UI.
- `app/static/`: the single-page UI (plain HTML, CSS and JavaScript, no build step).
- `app/seed/codecraft.json`: the fictional demo Brand Kit and four sample posts.

## Technology

Python 3.11, FastAPI, Uvicorn, SQLite (standard library), python-docx, Anthropic Python SDK; plain HTML/CSS/JS frontend;
pytest; GitHub Actions CI; deployed on Render.

## Changes from the Round 1 plan

| Plan | Built | Why |
|---|---|---|
| React (Vite) + Tailwind frontend | One FastAPI app serving a plain HTML/CSS/JS page | One service to deploy and nothing to build: faster to ship and more reliable for judges in the 72-hour window |
| Login with three roles | Role switch at the top right, every action role-checked and audited | Judges must use the product without signing in |
| Agent tools `check_glossary`, `extract_claims`, `propose_fix`, `verify_intent` called by the LLM | These run in code before the agent; the agent calls `search_rules`, `lookup_fact`, `add_finding`, `submit_rewrite` | Exact checks are cheaper and fully reliable in code; the AI is used only for judgment |
| Claim provenance as a Round 2 improvement | Claim checks (supported / contradicted / outdated / no source) are part of the core | The organisers' brief puts approved facts at the centre of the workflow |
| Seeded evaluation set in CI | 16 automated tests on fixed sample content in CI | Same purpose; precision/recall reporting moves to Round 3 |

## Three improvements for Round 3

1. **Claim provenance ledger:** a page per approved fact showing every draft that uses it; when a fact expires or changes, affected drafts and approved content are re-flagged automatically, and fact owners get a review queue.
2. **Guideline Clarity Score:** turn the dismissal data already collected into a score per rule, with the dismissal reasons grouped and an AI-suggested clearer rewrite of the rule for the brand manager to approve.
3. **Visual identity and accessibility checks for images:** upload a poster or social image; check colours against the brand palette, text contrast against WCAG 4.5:1, and missing or weak alt text (with a suggested alt text), shown with the same cited, explained findings.

## Run locally

```sh
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://127.0.0.1:8000
python -m pytest -q                # 16 tests, rules-only mode
```

AI review is optional: set `BRANDGUARD_ANTHROPIC_API_KEY` to enable the Claude Review Agent. The key is read only from this
variable and is never sent anywhere except the Anthropic API.

## Privacy and responsible design

- Only the content under review, its intent and the relevant Brand Kit items are sent to the AI service; nothing is used for training by BrandGuard.
- The AI cannot approve or publish: those actions exist only for people. Critical issues need a Manager.
- Every AI suggestion is labelled and every action is logged.
- The demo uses a fictional brand; no personal data is required.

## Team and contributions

- **Kartik Marwari** (team lead): Review Agent, rule and claim engine, backend, workflow, deployment.
- **Priyani Rajvanshi**: demo Brand Kit and sample content, user testing, documentation and demo video.

## AI assistance and third-party disclosure

Built with help from an AI coding assistant (Claude by Anthropic) for code generation, tests and documentation; the team reviewed,
ran and tested the code. At runtime the optional Review Agent calls the Anthropic Messages API (model `claude-opus-5-5`).
Open-source libraries: FastAPI, Uvicorn, Pydantic, python-docx, Anthropic Python SDK, pytest. No external datasets or templates.
