# 🛡️ BrandGuard

[![CI](https://github.com/kartikmarwari700/brandguard/actions/workflows/ci.yml/badge.svg)](https://github.com/kartikmarwari700/brandguard/actions/workflows/ci.yml)

**Explainable brand governance before you hit publish.** Team **Cite or Flag** · CodeZen India 2026 · problem statement by Inkloom AI.

**Live demo: https://brandguard-fa7o.onrender.com** · no sign-in · each browser gets its own private demo workspace ·
final report: [docs/CiteOrFlag-BrandGuard-Final-Report.pdf](docs/CiteOrFlag-BrandGuard-Final-Report.pdf)

Content goes in before it is published: a post, an ad, an email, a Word document or an image. BrandGuard checks it against the
organisation's Brand Kit (rules, approved facts, terminology, brand colours and accessibility). Every issue comes back with the
**rule or fact it breaks** and a **suggested fix that keeps the creator's message**, and **a person makes the final approval**.
The guidelines themselves improve over time: rules people keep overriding are flagged and rewritten with evidence, and an
existing guideline document can be imported instead of typed in.

The demo brand **CodeCraft Academy**, its facts, sample posts, poster, guideline PDF and example review history are fictional.

> **Live demo:** the AI runs on Abacus.AI RouteLLM (the code also supports Anthropic Claude). If the AI is unavailable, everything keeps working in rules-only mode and the header says so.

---

## Try it in 3 minutes

You need nothing but the link. The free server may take up to a minute to wake on the first visit.

1. **Review a post.** You start as **Creator** (role switch, top right). Click **Start with a sample →**. Highlights show each issue:
   red = critical, orange = major, yellow = minor, green = a claim verified against an approved fact. Each card names the rule
   or fact, explains it and offers **Accept fix** or **Dismiss…** (with a reason).
2. **Fix and approve.** Click **Accept suggested version**, then **Submit for review**. Switch to **Reviewer** and approve. If a
   critical issue is still open, only a **Manager** can sign off.
3. **Check an image.** Open **Poster: Winter Bootcamp**. The off-brand colour and weak alt text are reported. Click **Check text contrast** and
   drag a box around a line of text: code measures it against WCAG (the yellow line fails at about 1.5:1). With AI credit, the text in the
   image is also read automatically, checked for contrast and claims, and a suggested alt text is offered.
4. **Improve a guideline.** Switch to **Manager**, open **Guideline Clarity**. The vague rule *Friendly but professional* is flagged.
   Click **Draft a clearer rule**: compare before/after, see how past decisions would change, then **Apply to Brand Kit**.
5. **Import guidelines.** As Manager, open **Brand Kit → Import from guidelines** and click **Try with our sample guideline PDF**.
   Review the candidates (each with page and quote), select some and add them as a new Brand Kit version.
6. **Change a fact.** In **Brand Kit → Approved facts**, edit a fact (for example the 4.6 rating). Drafts are re-checked
   automatically; approved content that used the old figure gets a **provenance alert**.
7. **Reset demo data** in the footer restores the samples in your workspace at any time.

## Features

| Area | What works |
|---|---|
| Brand Kit | Rules with IDs, severity and good/bad examples; glossary; approved facts with source, owner and **expiry date**; brand palette. Managers add, edit and delete items; every change is a new, audited Kit version |
| **Brand Kit import** (Round 3) | Upload a guideline **PDF or Word** file; the Review Agent proposes rules, terms and facts with the **page and exact quote**; code drops any candidate whose quote is not in the document, flags duplicates and missing end dates; a Manager edits and selects what to add |
| Intake | Paste text, upload `.docx`, or upload an **image** (PNG/JPG/WebP) with caption and alt text; one-line intent (purpose, audience, call to action) |
| Exact checks (code) | Glossary and brand-name spelling, hype, slang, pressure tactics, guarantees, superlatives, “click here”, ALL-CAPS, sentence length, missing call to action |
| Claim checks (code) | Every number is compared with approved facts: **supported**, **contradicted**, **outdated** (expired fact) or **no source**; dates, times and phone numbers are ignored |
| **Image checks** (Round 3) | Main colours vs the brand palette (CIELAB distance), alt text missing or generic, and **WCAG contrast** (4.5:1, 3:1 large) of any line a person marks with a box (all code); with AI, every text block is read with its position, measured and run through the rule and fact checks, and alt text is suggested as a one-click fix |
| Review Agent (AI) | Claude Haiku 5.5 with Brand Kit tools (`search_rules`, `lookup_fact`, `add_finding`, `submit_rewrite`) checks tone, messaging and claims in context and writes a suggested version |
| Policy guard | AI findings must cite a rule a tool returned and quote the text exactly; they are advisory (only code checks can raise a critical, approval-blocking issue); fixes must be publishable text; a rewrite must keep the call to action and brand name and add no unapproved number |
| Workflow | Creator / Reviewer / Manager; accept fix, accept suggested version, edit, dismiss with reason (critical: Manager only), comment, request changes, approve (critical open: Manager sign-off only); approved content is locked |
| Claim provenance (I1) | Drafts are re-checked automatically when a fact changes; approved content gets a provenance alert; the Facts tab shows where each fact is used |
| **Guideline Clarity Score** (Round 3) | Score per rule from real accept/dismiss decisions; unclear rules flagged with most-dismissed phrases and reasons; AI drafts a clearer rule; code **replays past decisions** against the draft; Manager applies it as a new Kit version and the rule starts a fresh score |
| Traceability | Versions with word-level compare (caption and alt text for images), full audit log, per-review AI cost |
| Reliability | Private workspace per browser; clear messages for bad input, repeated actions and stale screens; buttons blocked while saving; AI timeouts and a watchdog so nothing hangs; rules-only mode when AI is unavailable |
| AI cost control | Spend meter from real token usage, hard demo budget, hourly cap, prompt caching, reuse of identical reviews |

## How it works

```
                     ┌──────────── Brand Kit (versioned) ◄── Import from guideline PDF/DOCX (agent proposes, code checks quotes, manager accepts)
                     ▼                    ▲
Content + intent ─► Exact checks ─► Review Agent ─► Policy guard ─► Findings + suggested fix ─► People decide ─► Audit log
(text/.docx/image)  (code: rules,   (Claude Haiku    (cited IDs,      (rule or fact on every      (accept · edit ·    │
                     facts, colours, 5.5 + tools;     exact quotes,     issue)                       dismiss · approve) ▼
                     alt text)       vision for       AI ≤ major,                                             Guideline Clarity Score
                                     image text)      intent kept)                                            (score → AI draft → replay → apply)
```

| Module | Responsibility |
|---|---|
| `app/engine.py` | Deterministic checks: patterns, glossary, claim extraction and fact lookup, readability, call to action, fixes, intent check |
| `app/agent.py` | Review Agent tool loop, policy guard, spend meter, review cache, API error handling |
| `app/images.py` | Image loading, palette and alt-text checks, AI text reading, WCAG contrast measurement |
| `app/clarity.py` | Guideline Clarity Score, AI/code rule drafts, decision replay, applying drafts |
| `app/importer.py` | PDF/DOCX text extraction, AI/code candidate extraction, quote grounding, duplicates, applying imports |
| `app/store.py` | SQLite persistence, per-browser workspaces, versions, decisions, roles, approvals, audit, demo seed |
| `app/main.py` | FastAPI JSON API (`/docs`), validation messages, workspace middleware, static UI |
| `app/static/` | Single-page UI in plain HTML, CSS and JavaScript (no build step) |
| `app/seed/` | Fictional Brand Kit, sample posts, demo poster and example review history |
| `eval/` | Hand-labelled evaluation set and precision/recall report |

### AI and cost

Claude is used only for judgment: tone and messaging in context, reading text in images, drafting clearer rules and reading
guideline documents. Exact checks, contrast maths, quote grounding and decision replay are code. The model is
`claude-haiku-5-5` (fast and inexpensive), with strict tool schemas, prompt caching and short loops. A typical review costs about
$0.001–0.002. A spend meter prices the real token usage of every response and switches to rules-only mode at the budget
(`BRANDGUARD_AI_BUDGET_USD`, default $1.00) or the hourly cap (`BRANDGUARD_AI_MAX_PER_HOUR`, default 30); the screen says so and
the rest of the product keeps working.

## Testing

- **39 automated tests** (`pytest`) run in GitHub Actions on every push: the engine, the full workflow through the HTTP API,
  workspace isolation, repeated actions and bad input, the Review Agent loop against a simulated model (policy guard, invented
  numbers, API failure, an account without credit, budget and hourly caps, caching), image checks (real image, simulated vision response, contrast
  measurement, alt-text fix), the Guideline Clarity Score (scoring, code and simulated-AI drafts, replay, apply, fresh score), and
  Brand Kit import (PDF parsing, code extraction of rules, terms and facts, quote grounding against invented quotes, duplicates, apply), and contrast checks drawn by a person.
- **Labelled evaluation set:** 50 hand-labelled cases in [`eval/cases.json`](eval/cases.json); `python -m eval.run_eval` prints
  precision and recall per check ([`eval/results.md`](eval/results.md)). The first run found 3 false alarms (a clock time and a phone
  number read as claims) and 1 labelling error; after the fix: 100% precision, 100% recall, 8/8 claims verified. A test keeps both
  at 95% or more in CI. The set was written by the team and is small, so it guards against regressions rather than proving
  real-world accuracy.
- **Live end-to-end checks** of every journey on the deployed link, as Creator, Reviewer and Manager, including refresh, repeated
  clicks, wrong file types, a missing page and the AI being unavailable.

## Run it yourself

Requirements: Python 3.11 and Git.

```sh
git clone https://github.com/kartikmarwari700/brandguard.git
cd brandguard
python -m venv .venv
.venv\Scripts\activate             # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn app.main:app --reload       # open http://127.0.0.1:8000
```

```sh
python -m pytest -q                 # 39 tests, no API key needed
python -m eval.run_eval             # precision/recall on the labelled set
```

Everything works without an API key (rules-only mode). To enable the AI Review Agent, set the key before starting the server:

| Variable | Default | Meaning |
|---|---|---|
| `BRANDGUARD_ANTHROPIC_API_KEY` | (none) | Anthropic API key; read only from this variable and sent only to the Anthropic API |
| `BRANDGUARD_ABACUS_API_KEY` | (none) | Optional: use Abacus.AI RouteLLM (`route-llm`) instead of Anthropic when no Anthropic key is set |
| `BRANDGUARD_MODEL` | `claude-haiku-5-5` | Model for all AI features |
| `BRANDGUARD_AI_BUDGET_USD` | `1.00` | AI spend per server process before switching to rules-only mode |
| `BRANDGUARD_AI_MAX_PER_HOUR` | `30` | New AI reviews per hour |
| `BRANDGUARD_DB` | `brandguard.db` | SQLite file (demo data; rebuilt from the seed if the schema changes) |

**Deploy:** the repository includes a Render Blueprint (`render.yaml`): New → Blueprint → this repository, then paste the API key
into `BRANDGUARD_ANTHROPIC_API_KEY`. A GitHub Action (`keep-awake.yml`) pings the app every 10 minutes when the repository
variable `APP_URL` is set.

## Known limitations

- Roles are a demo switch, not accounts; the role rules and audit log are enforced, but there is no authentication.
- Free hosting: the first visit after idling can be slow, and data resets to the samples when the server restarts.
- Automatic text reading in images needs the AI; without it, colours, alt text and contrast of marked text are still checked in code.
- Scanned (image-only) PDFs cannot be imported.
- English content only. The evaluation set is small and written by the team.

## Roadmap

1. **Pilot with real teams (next 3 months):** accounts and SSO with the same three roles, a persistent PostgreSQL database,
   per-organisation Brand Kits, and user studies to measure review time and repeat issues.
2. **Where content is written:** Google Docs and Figma plugins, a browser extension for social media composers, and a
   publishing gate API for CMS tools.
3. **Indian languages:** Hindi and Hinglish tone checks and glossaries.
4. **More formats:** PDF and slide decks, video through transcripts and key frames, logo misuse detection.
5. **Private deployment:** a locally hosted model option for organisations whose content must never leave their servers.
6. **Learning from approvals:** the Clarity Score suggests new rules from patterns in approved and rejected content.

## Privacy and responsible design

- Only the content under review, its intent and the Brand Kit items returned by tools are sent to the AI service.
- The AI cannot approve or publish; critical issues need a Manager; AI findings are labelled and cannot block approval alone.
- Every AI suggestion is checked by code (citations, quotes, numbers, intent) and every action is logged.
- The demo needs no personal data.

## Team and contributions

- **Kartik Marwari** (team lead): Review Agent, rule and claim engine, image checks, Clarity Score, import, backend, deployment.
- **Priyani Rajvanshi**: demo Brand Kit and sample content, documentation, user testing.

## AI assistance and third-party disclosure

Built with help from an AI coding assistant (Claude by Anthropic) for code generation, tests and documentation; the team reviewed,
ran and tested the code. At runtime the optional Review Agent calls the Anthropic Messages API (model `claude-haiku-5-5`).
Open-source libraries: FastAPI, Uvicorn, Pydantic, python-docx, pypdf, Pillow, Anthropic Python SDK, pytest. No external datasets;
the demo brand, documents and images were created for this project.
