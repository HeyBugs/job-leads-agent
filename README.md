# Job Leads Agent

Finds hot job leads automatically so you never trawl LinkedIn/Indeed manually.
Collects from Greenhouse/Lever/Ashby company boards (`watchlist.json`) plus
Remotive (and Adzuna once you add a free key), dedupes into `leads.db`,
hard-filters by title/location (`config.json`), scores every survivor 0-100
with Claude against `profile.md`, and writes a ranked digest.

## Daily output

`digests\latest.md` — today's ranked leads (🔥 Hot = score 75+, apply fast), with
salary ranges and posting age. Fresh leads scoring 80+ get a **tailored resume
(.docx + PDF), a cover letter (as text AND as a matching .docx + PDF document),
and a LinkedIn outreach message auto-drafted** into
`applications\<date>-<company>-<role>\`, then reviewed by a second **critic pass**
posing as the hiring manager — still review before sending, never submit blind.
A Windows toast fires only when NEW hot leads appear (reruns stay silent). The
scheduled task (**JobLeadsAgent**) runs at 8:00 AM and 1:00 PM, catches up after
sleep, and also runs 2 minutes after every logon — all with no visible window —
so mornings where the laptop was closed at 8:00 still get covered.

## Applying (the apply agent)

Verify `answers.json` once before first use — the agent asserts those values
(work eligibility, sponsorship) on real applications.

- `py -3 apply.py <job-id>` — assisted: fills everything in a visible browser,
  YOU review and click Submit, then press Enter to mark applied.
- `py -3 apply.py <job-id> --auto` — fills AND submits, marks applied.
- `py -3 apply.py --auto-all` — auto-applies to every drafted, still-new lead.
- `py -3 apply.py <job-id> --test` — headless dry run + screenshot, never submits.

Auto mode mechanics: it will not submit if an interactive CAPTCHA is present
(cannot solve them; those jobs fall back to assisted mode), it screenshots the
form before and the confirmation after every submission into the application
folder, and if no confirmation is detected it marks the lead 'check' instead of
'applied'. To go fully hands-free, set `"auto_apply_in_run": true` in
config.json — the twice-daily scheduled run will then submit for you.

## Setup (no file editing needed)

**⚙ Setup** in the console header (http://127.0.0.1:8787/setup): confirm the
answers forms will carry (work eligibility, sponsorship, pronouns, contact),
and paste the free keys that unlock more sources — SerpAPI (Google Jobs incl.
LinkedIn), Adzuna, and your Gmail app password for LinkedIn alert emails —
with direct links to each signup. Saves straight to config.json/answers.json.

## The Console (the dashboard you actually use)

**Double-click "Job Console" on the Desktop** (or open http://127.0.0.1:8787).
A local server (server.py, started automatically at login by the
JobLeadsConsole task) serves the dashboard with working buttons:

- **▶ Apply** — opens the real application form in a browser, pre-filled;
  review, click Submit on the site, close the window, press **✓ Applied**.
- **⚡ Auto** — submits the application in the background, no review.
- **✓ Applied / ✕ Skip** — update tracking; **📄 package** opens the folder
  with the tailored resume, cover letter, and outreach message.
- In flight rows: **↻ Followed up / 🎤 Interview / ✕ Closed** — marking
  Interview auto-generates the prep pack.
- **↻ Run pipeline now** — fetch + score + draft on demand.
- **⚡ Apply to all ready** — auto-submits every drafted application in one go
  (confirm dialog; CAPTCHA forms are skipped for assisted mode).
- **✕ Skip asks why (optional)** — your reason feeds the scorer, so it learns
  what you don't want, not just that you didn't want it.
- `py -3 agent.py selftest` — invariant checks (also runs before every
  scheduled pipeline run); `py -3 agent.py status` — one-screen state.
- `py -3 test_apply_fill.py` — the applier's regression suite (16 checks:
  human-like filling rules, the review banner, and the stay-open wait loop).
  Run it after any change to apply.py.

Applications launched from the console run as fully detached processes, so
restarting or stopping the console can never close a form you're reviewing.
- **Next best move banner** — the single highest-value action right now.
- **🧭 Coach briefing** — once a day an agent reads the whole funnel and writes
  what to do today (regenerate anytime: `py -3 agent.py coach`).
- **Ask the agent** box at the bottom — ask anything ("which should I do
  first?", "why did X score low?"); it answers from your live pipeline data.
- Filter chips + search over the leads table.
- **📦 Build** on any lead without a package — drafts the tailored resume,
  cover letter, and outreach on demand (works even for old postings the
  automatic drafter skips).
- A warning banner appears if the pipeline hasn't run in over a day.
- The activity tile shows new leads per day (14d) and applications this week.

Leads whose posting disappears from a company's board are automatically marked
**gone** and drop off the console (and come back if the posting reappears) —
only boards that fetched successfully are pruned against, so an API hiccup
never mass-removes leads. Marking a lead applied or interview from the console
also refreshes the coach briefing immediately.

`leads.db` is backed up daily to OneDrive
(`OneDrive\Documents\job-leads-agent-backup`, last 7 kept), and logs
self-rotate at 2MB.

The scorer also **learns from your decisions**: roles similar to ones you skip
get scored down, roles similar to ones you pursue get scored up.

Hot leads (75+) stay on the board until acted on; a green dot marks leads
found today; the page refreshes itself every 15 minutes. Everything is
localhost-only. `dashboard.html` is a static button-less copy regenerated
each pipeline run (works without the server).

## The line (this is the product)

Everything happens in one Telegram chat with your bot. Nothing to open.

```
you   → https://jobs.acme.com/123
bot   → #4 Reading the posting and writing your resume + cover letter. About
        5 minutes.
bot   → 📄 Resume - Customer Success Manager - Acme.docx
        ✉️ Cover letter - Customer Success Manager - Acme.docx
        #4 Resume + cover letter for Customer Success Manager at Acme are
        above. Apply when you're ready, then reply APPLIED #4.
you   → APPLIED #4  Jane Doe jane@acme.com
line  → #4 Filed: Customer Success Manager at Acme. I'll message you the
        follow-up on Sep 29. To: Jane Doe, jane@acme.com.
        (a week later, 8:00 AM)
line  → #4 Follow up: Customer Success Manager at Acme (applied Sep 22)
        TO: Jane Doe, Talent Partner  jane@acme.com
        SUBJECT: … / the drafted email …
        Reply SEND to email this from your Gmail, SNOOZE #4, or SKIP #4.
you   → SEND
line  → Sent to jane@acme.com. #4 marked followed up.
```

Already applied when you send the link? Put the word **applied** in the same
message (plus a name/email if you have one) and it skips straight to the
follow-up. Other messages: `SEND <email>`, `SENT` (you messaged them on
LinkedIn yourself), `SNOOZE 5`, `SKIP`, `DRAFT` (write the follow-up now),
`LIST`, `NAME <role> at <company>` (when a posting would not load), or just an
email address / LinkedIn profile to attach a contact. `#n` picks one when
several are pending. Anything else is answered as a question about your
pipeline.

The documents are the same tailored resume (.docx + PDF) and cover letter
(.docx + PDF) the pipeline builds from `facts.md`, saved in
`applications\<date>-<company>-<role>\` and emailed to you when Gmail is set up.

### One-time setup

1. In Telegram open **@BotFather**, send `/newbot`, give it a name and a
   username ending in `bot`, and copy the token.
2. Put the token in `config.json` as `telegram_bot_token`. The bot only answers
   `telegram_username` (already set to your account).
3. Start it: `schtasks /run /tn JobLeadsLine` (it also starts at every login).
4. Open your bot in Telegram and send it anything. It replies "Connected".

Also worth setting: `gmail_user` + `gmail_app_password`
(myaccount.google.com/apppasswords) so SEND can email follow-ups from your
Gmail, and optionally Google Calendar (`google_oauth_client.json` from a
Desktop OAuth client with the Calendar API enabled, then
`py -3 followup.py calendar-connect`; applied and follow-up events are then
created for every application, earlier ones backfilled).

The poller long-polls Telegram, so no public URL or tunnel is needed.
`py -3 sms.py handle "<message>"` dry-runs any message without sending.
Twilio SMS/WhatsApp remains available in `sms.py` as an alternative transport.

The old console (server.py, dashboard.py) is parked in `parked\console\`.

## The application pipeline

Track every lead with `mark`; the digest grows a **Pipeline** section showing
where everything stands and nagging at the right moments:

- `mark <id> applied` — schedules the follow-up (above); the digest shows the
  date and says when the draft is ready in the console.
- `mark <id> interview` — auto-generates an **interview prep pack**
  (web-researched company intel + STAR answers from facts.md) into the
  application folder. Or run it anytime: `py -3 agent.py prep <id>`.
- `mark <id> closed` / `rejected` / `skipped` — retires the lead.

## The scout (supporting agent)

`scout.py` runs weekly (**JobLeadsScout** task, Sundays 9:00 AM): it harvests
company names from the latest Hacker News "Who is hiring" thread and from
Remotive/Adzuna rows in the database, probes ATS endpoints (full-name slugs
only, Greenhouse boards name-verified), auto-appends validated boards to
`watchlist.json`, and removes boards unreachable three runs straight.

## Commands (run from this folder)

```
py -3 agent.py run        # full pipeline: collect + score + draft + digest
py -3 agent.py collect    # just fetch new jobs
py -3 agent.py score      # just score unscored jobs (claude CLI, haiku)
py -3 agent.py draft      # just draft resumes for undrafted 80+ leads (sonnet + critic)
py -3 agent.py prep <id>  # interview prep pack for one lead (sonnet + web search)
py -3 agent.py digest     # just rebuild the digest
py -3 agent.py refilter   # re-apply filters after editing config.json
py -3 agent.py mark <job-id> <status>  # applied | followed-up | interview |
                                       # closed | rejected | skipped
py -3 scout.py            # grow/prune the watchlist now
```

## Tuning

- **facts.md** — the ONLY source drafted resumes may use. Keep it truthful and
  current; every draft is constrained to reorder/reword what's here. Start by
  copying `facts.example.md` (facts.md stays untracked, like config.json).
- **profile.md** — what "good fit" means. Edit freely; scoring reads it
  verbatim. Start by copying `profile.example.md` (also untracked).
- **config.json** — hard filters (title include/exclude, locations), thresholds,
  score model. After editing filters, run `refilter`.
- **watchlist.json** — companies to watch. To add more, put candidate slugs in
  `validate_watchlist.py` and run it; paste valid entries in. (Or ask Claude:
  "find 20 more Toronto companies on Greenhouse/Lever/Ashby and validate them.")
- **Adzuna** (broad coverage incl. much of what Indeed lists): free key at
  developer.adzuna.com, paste `adzuna_app_id`/`adzuna_app_key` into config.json.

## LinkedIn — the honest answer

Bots on a logged-in LinkedIn account get the ACCOUNT BANNED (their detection is
the best in the industry), which would hurt your job hunt far more than any
scraper helps. But most LinkedIn postings are mirrors of sources this agent CAN
read, and LinkedIn will voluntarily email you the rest:

1. **SmartRecruiters ATS** (already live, no key): mainstream employers like
   ServiceNow, Ubisoft, Gameloft, Keywords Studios — the kind of postings you
   see on LinkedIn.
2. **Google Jobs via SerpAPI** — Google indexes LinkedIn's postings. Sign up
   free at serpapi.com (100 searches/month, no card), paste the key into
   `serpapi_key` in config.json. Results arrive tagged "via LinkedIn".
3. **LinkedIn job-alert emails** — LinkedIn emails you its matches; the agent
   reads them from your own Gmail. Setup (one time, ~5 min):
   a. On LinkedIn: search your target job, toggle "Set alert" (do a few:
      junior developer Toronto, solutions specialist, support analyst — set
      frequency Daily).
   b. Google Account → Security → 2-Step Verification (enable if needed) →
      App passwords → create one for "Mail".
   c. In config.json set `linkedin_imap_user` to your Gmail address and
      `linkedin_imap_app_password` to that 16-character password.
   The pipeline then ingests every job LinkedIn emails you — no scraping, no
   login automation, no account risk.

## Schedule

```
schtasks /run /tn JobLeadsAgent        # run now
schtasks /delete /tn JobLeadsAgent /f  # remove
schtasks /change /tn JobLeadsAgent /st 07:00   # change time
```

Runs log to `logs\agent.log`. Day 1 was a backfill; from now on only
never-before-seen postings appear, so freshness is real.
