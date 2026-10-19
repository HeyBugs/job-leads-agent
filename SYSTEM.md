# How this system works

One sentence: **robots watch the job market, an AI decides what fits you,
writes the applications, and nags you until they're sent — you review and
click.**

```
        SOURCES                    BRAIN                     OUTPUT
  ┌──────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
  │ 64 company boards│   │ hard filters         │   │ digest (markdown)    │
  │  Greenhouse      │   │  title/location/level│   │ digest + phone line  │
  │  Lever           ├──►│ dedupe + lifecycle   ├──►│  (WhatsApp / SMS)    │
  │  Ashby           │   │  (SQLite leads.db)   │   │ desktop notification │
  │  SmartRecruiters │   │ Claude scoring 0-100 │   │ coach briefing       │
  │ Remotive (remote)│   │  + learns from your  │   └──────────┬───────────┘
  │ Adzuna*          │   │    skips & applies   │              │
  │ Google Jobs*     │   │ Claude drafting      │   ┌──────────▼───────────┐
  │  (incl LinkedIn) │   │  resume+letter+      │   │ THE HANDS            │
  │ LinkedIn alert   │   │  outreach + critic   │   │ apply.py fills real  │
  │  emails*         │   │  review pass         │   │ forms (Playwright);  │
  └──────────────────┘   └──────────────────────┘   │ you click Submit (or │
   * = needs a free key/                            │ --auto does)         │
       setup, see README                            └──────────────────────┘
```

## The cast

| Piece | File | What it does |
|---|---|---|
| Pipeline | `agent.py` | follow-ups due → collect → filter → score → draft → coach → digest → backup. One `run` does it all. |
| Watchlist | `watchlist.json` | The 64 companies whose boards are polled. |
| Scout | `scout.py` | Weekly (Sun 9:00): finds new companies (HN "Who is hiring", DB company names), validates their boards, adds them; prunes dead boards. |
| Filters | `config.json` | Title include/exclude, locations, thresholds, models, optional API keys. After editing: `py -3 agent.py refilter`. |
| Scoring truth | `profile.md` | What "a good job for Matthew" means. The scorer reads it verbatim, plus your recent skips/applies as a preference signal. |
| Resume truth | `facts.md` | The ONLY facts drafts may use. Drafting = rearranging this file, never inventing. |
| Applier | `apply.py` | Opens the real application form, uploads the tailored resume, fills your details (`answers.json`), pastes the cover letter, sets eligibility dropdowns, AI-answers custom questions. Assisted = you click Submit; `--auto` submits itself (skips CAPTCHA forms, keeps screenshot audit trail). |
| Phone line | `sms.py` | WhatsApp/SMS on a Twilio number, polled every 30 s: a job link → resume + cover letter emailed; APPLIED → follow-up scheduled; the drafted follow-up arrives on the day, SEND emails it. Questions are answered from live pipeline data. |
| Follow-ups | `followup.py` | Reads any posting (HTTP / LinkedIn guest endpoint / headless browser), schedules the follow-up + Google Calendar events, drafts the targeted email, sends via Gmail. |
| (parked) Console | `parked/console/` | The old localhost UI. Not started, not needed. |
| Coach | `agent.py coach` → `coach.md` | Daily: an agent reads the entire funnel and writes what to do today. Refreshes immediately when you mark applied/interview. |
| Prep | `agent.py prep <id>` | On `interview`: web-researched company brief + STAR answers from facts.md. |
| Memory | `leads.db` | Every job ever seen, its score, status, and paper trail. Backed up daily to OneDrive. |

## What runs when

- **8:00, 13:00, and 2 min after every login** — full pipeline (task
  `JobLeadsAgent`, invisible, catches up after sleep).
- **Every login** — phone-line poller starts (task `JobLeadsSMS`).
- **Sunday 9:00** — scout grows/prunes the watchlist (`JobLeadsScout`).
- **Daily** — coach briefing, DB backup, log rotation.

## Lifecycle of one job

collected → deduped (never shown twice) → hard-filtered → scored by Claude
(with your preference history) → if 80+ and fresh: package auto-drafted
(resume + cover letter + outreach, then a critic pass) → appears in the digest.
Or, the usual way: you message a job link → the same package is built and
emailed to you → you apply → APPLIED → 7 days later the drafted, targeted
follow-up arrives on your phone → SEND → interview: prep pack generated →
closed/rejected → done.
If the company deletes the posting meanwhile: automatically marked gone.

## What it costs / uses

- All board APIs are public and free. Claude calls run on your Claude Code
  subscription: haiku for scoring (cheap, batched), sonnet for drafts/critic/
  coach/answers.
- Optional free keys unlock more coverage: Adzuna, SerpAPI (Google Jobs —
  the LinkedIn channel), Gmail app password (LinkedIn alert emails).

## What it deliberately does NOT do

- Scrape LinkedIn with your logged-in account (gets accounts banned).
- Solve CAPTCHAs (auto mode skips those forms; assisted mode you solve them).
- Invent resume facts (facts.md is a hard boundary with written rules).
- Submit anything you haven't authorized (auto is opt-in per click, or via
  `auto_apply_in_run` for fully hands-free).

## Quick commands

`py -3 agent.py status` — one-screen state of everything.
Everything else: README.md.
