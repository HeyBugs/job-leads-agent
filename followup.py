"""Follow-up engine for the Job Leads Agent.

The problem it solves: applications go out, follow-ups never do. So every
application (from the scout OR a link someone sent you) gets a follow-up date,
two calendar events, a targeted draft when the date arrives, a toast that opens
the console, and a one-click Send.

Intake is deliberately frictionless. Paste anything into the console's
"I just applied" box: a job link, several links, a recruiter email, a name.
    https://boards.greenhouse.io/acme/jobs/123  Jane Doe jane@acme.com
The engine pulls the URLs and emails out, reads each posting (plain HTTP, the
LinkedIn guest endpoint, or a headless browser when the page needs JavaScript),
asks Claude for the title/company/contact facts, and files the row as applied.
If the scout already knew the posting, that row is reused.

Commands (run from this folder):
  py -3 followup.py applied <text with links>   # file an application
  py -3 followup.py intake <job-id>             # (internal) read posting, schedule
  py -3 followup.py schedule <job-id>           # (re)compute due date + calendar events
  py -3 followup.py due                         # draft everything due, toast once
  py -3 followup.py draft <job-id>              # draft now, ignoring the date
  py -3 followup.py send <job-id>               # send the stored draft (console does this)
  py -3 followup.py calendar-connect            # one-time Google Calendar OAuth
  py -3 followup.py selftest                    # offline checks of the parsers

Config keys (config.json): followup_days, followup_model, intake_model,
  gmail_user, gmail_app_password (fall back to the linkedin_imap_* pair),
  google_calendar_id (default "primary").
Google Calendar needs google_oauth_client.json (an OAuth "Desktop app" client
from console.cloud.google.com with the Calendar API enabled) in this folder;
the token lands in google_token.json. Until then everything else still works
and events are created retroactively once connected.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from agent import CONFIG, claude_command, db, log, norm, strip_html  # noqa: E402

NO_WINDOW = 0x08000000
CONSOLE_URL = "http://127.0.0.1:8787"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

FOLLOWUP_COLUMNS = (
    "contact_name TEXT", "contact_email TEXT", "contact_linkedin TEXT",
    "contact_role TEXT", "contact_notes TEXT", "contact_phone TEXT", "applied_at TEXT",
    "followup_due TEXT", "followup_state TEXT", "followup_target TEXT",
    "followup_draft TEXT", "followup_sent_at TEXT", "followup_notified TEXT",
    "cal_applied_id TEXT", "cal_followup_id TEXT",
)


def fdb() -> sqlite3.Connection:
    con = db()
    for col in FOLLOWUP_COLUMNS:
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return con


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ------------------------------------------------------------------ parsing
URL_RE = re.compile(r"https?://[^\s<>\"']+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
# Words people type around a contact ("Phone Number:", "email", "recruiter")
# and chatter ("I just applied to the job, here are some contact details").
LABEL_RE = re.compile(
    r"\b(phone(?: number)?|tel|cell|mobile|e-?mail|contact(?: details| info)?|name|"
    r"recruiter|hiring manager|linkedin|applied|i just|to the job|here (?:are|is)|some|"
    r"the|and|for|this|that|job|role|position|posting)\b[:\s]*", re.I)


def parse_intake(text: str) -> dict:
    """Split a pasted blob into job URLs, a contact LinkedIn URL, an email, a
    phone number and, if what is left looks like a person's name, the name."""
    text = (text or "").strip()
    urls = [u.rstrip(".,;)") for u in URL_RE.findall(text)]
    job_urls, linkedin_profile = [], ""
    for u in urls:
        if re.search(r"linkedin\.com/in/", u, re.I):
            linkedin_profile = linkedin_profile or u
        else:
            job_urls.append(u)
    emails = EMAIL_RE.findall(text)
    rest = URL_RE.sub(" ", text)
    rest = EMAIL_RE.sub(" ", rest)
    phones = PHONE_RE.findall(rest)
    rest = PHONE_RE.sub(" ", rest)
    rest = LABEL_RE.sub(" ", rest)
    rest = re.sub(r"#\d+", " ", rest)
    rest = re.sub(r"[^\w\s'’.\-]", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" -–,;:|.")
    words = rest.split()
    # A name is one to four Capitalized words ("Jane Doe", "Priya Shah-Lee");
    # "I to" or "that job" are leftovers of a sentence, not a person.
    name = rest[:120] if (1 <= len(words) <= 4 and
                          all(re.match(r"[A-Z][\w'’.-]*$", w) for w in words) and
                          not all(w.lower() in ("i", "me", "my", "it", "ok", "yes") for w in words)
                          ) else ""
    return {"job_urls": job_urls, "contact_email": emails[0] if emails else "",
            "contact_linkedin": linkedin_profile, "contact_name": name,
            "contact_phone": phones[0].strip() if phones else ""}


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same posting pasted twice matches once."""
    p = urllib.parse.urlsplit(url.strip())
    keep = []
    for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in ("ref", "refid", "trk", "trackingid",
                                            "src", "source", "gh_src", "lever-source",
                                            "originalsubdomain", "position", "pagenum"):
            continue
        keep.append((k, v))
    host = p.netloc.lower().removeprefix("www.").removeprefix("ca.")
    path = p.path.rstrip("/")
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", host, path,
                                    urllib.parse.urlencode(keep), ""))


def linkedin_job_id(url: str) -> str | None:
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d{6,})", url, re.I) \
        or re.search(r"[?&]currentJobId=(\d{6,})", url)
    return m.group(1) if m else None


def job_id_for(url: str) -> str:
    return "ap-" + hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ fetching
BLOCK_WORDS = ("enable javascript", "just a moment", "access denied",
               "verify you are human", "sign in to view", "authwall",
               "checking your browser", "request blocked", "you have been blocked",
               "additional verification required")


def _http_get(url: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _meta(html: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)',
                  html, re.I) or \
        re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
                  html, re.I)
    return _html.unescape(m.group(1)).strip() if m else ""


def _title_tag(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return strip_html(m.group(1)) if m else ""


def _body_text(html: str) -> str:
    html = re.sub(r"<(script|style|noscript|svg|nav|footer)[^>]*>.*?</\1>", " ",
                  html, flags=re.I | re.S)
    return strip_html(html)


def _looks_blocked(status: int, text: str) -> bool:
    if status in (0, 401, 403, 429, 999) or len(text) < 400:
        return True
    head = text[:600].lower()
    return any(w in head for w in BLOCK_WORDS)


def _browser_text(url: str) -> tuple[str, str]:
    """Headless Chromium fallback for JavaScript-rendered postings."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return "", ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=UA, viewport={"width": 1280, "height": 1600})
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            html = page.content()
            text = page.inner_text("body")
            browser.close()
            return html, re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        log(f"intake: browser fallback failed ({e})")
        return "", ""


def fetch_posting(url: str) -> dict:
    """Returns {text, og_title, site_name, title_tag, fetched_via}."""
    out = {"text": "", "og_title": "", "site_name": "", "title_tag": "", "fetched_via": ""}
    jid = linkedin_job_id(url)
    if jid:
        status, html = _http_get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}")
        if status == 200 and len(html) > 400:
            out.update(text=_body_text(html), title_tag=_title_tag(html),
                       fetched_via="linkedin-guest")
            m = re.search(r'class="topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', html, re.S)
            if m:
                out["site_name"] = strip_html(m.group(1))
            m = re.search(r'class="top-card-layout__title[^"]*"[^>]*>(.*?)</h', html, re.S)
            if m:
                out["og_title"] = strip_html(m.group(1))
            return out
    status, html = _http_get(url)
    text = _body_text(html) if html else ""
    via = "http"
    if _looks_blocked(status, text):
        # Indeed and friends block the first headless visit now and then and
        # let the next one through, so try a few times before giving up.
        import time
        for attempt in range(3):
            b_html, b_text = _browser_text(url)
            if len(b_text) > len(text) and not _looks_blocked(200, b_text):
                html, text, via = b_html, b_text, "browser"
                break
            if len(b_text) > len(text):
                html, text, via = b_html, b_text, "browser"
            time.sleep(5 * (attempt + 1))
    out.update(text=text, og_title=_meta(html, "og:title"),
               site_name=_meta(html, "og:site_name"), title_tag=_title_tag(html),
               fetched_via=via)
    return out


# ------------------------------------------------------------------ claude
def _claude_json(instruction: str, stdin_doc: str, model: str, timeout: int = 300) -> dict | None:
    cmd = claude_command() + ["-p", instruction, "--model", model]
    try:
        proc = subprocess.run(cmd, input=stdin_doc, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=NO_WINDOW)
        m = re.search(r"\{.*\}", proc.stdout, re.S)
        if proc.returncode != 0 or not m:
            log(f"claude: no JSON ({(proc.stderr or proc.stdout)[:200]})")
            return None
        return json.loads(m.group(0))
    except Exception as e:
        log(f"claude failed: {e}")
        return None


EXTRACT_SCHEMA = """{
  "company": "<employer name, not the job board>",
  "title": "<job title as posted>",
  "location": "<city/region and remote/hybrid/onsite if stated, else ''>",
  "salary": "<salary/range text if stated, else ''>",
  "summary": "<3-4 sentences: what the role does, team, top 3 requirements>",
  "recruiter_name": "<a named recruiter/hiring contact in the posting, else ''>",
  "recruiter_email": "<an email address in the posting meant for applicants, else ''>",
  "hiring_manager_hint": "<the title this role likely reports to, e.g. 'Head of Customer Success', else ''>",
  "is_job_posting": true
}"""


def extract_posting(url: str, fetched: dict) -> dict:
    hints = {k: fetched[k] for k in ("og_title", "site_name", "title_tag") if fetched.get(k)}
    doc = ("URL: " + url + "\nPAGE HINTS: " + json.dumps(hints, ensure_ascii=False) +
           "\n\nPAGE TEXT:\n" + (fetched.get("text") or "")[:9000])
    instruction = (
        "Extract the job posting facts from the page on stdin. Respond with ONLY a JSON "
        "object, no prose, no code fences, matching exactly:\n" + EXTRACT_SCHEMA +
        "\nNever invent a person or an email; leave them '' if the page does not name "
        "one. If the page is not a job posting (login wall, error, expired) set "
        "is_job_posting=false and fill what you can from the URL and hints."
    )
    data = _claude_json(instruction, doc, CONFIG.get("intake_model", "haiku")) or {}
    host = urllib.parse.urlsplit(url).netloc.removeprefix("www.")
    data.setdefault("company", fetched.get("site_name") or host)
    data.setdefault("title", fetched.get("og_title") or fetched.get("title_tag") or "(untitled role)")
    for k in ("location", "salary", "summary", "recruiter_name", "recruiter_email",
              "hiring_manager_hint"):
        data[k] = (data.get(k) or "").strip() if isinstance(data.get(k), str) else ""
    if not EMAIL_RE.fullmatch(data["recruiter_email"] or "x"):
        data["recruiter_email"] = ""
    return data


# ------------------------------------------------------------------ intake
def find_existing(con: sqlite3.Connection, url: str) -> str | None:
    canon = canonical_url(url)
    jid = linkedin_job_id(url)
    for row in con.execute("SELECT id, url FROM jobs WHERE url IS NOT NULL").fetchall():
        u = row[1] or ""
        if u == url or canonical_url(u) == canon or (jid and jid in u):
            return row[0]
    return None


def cmd_file(text: str, applied: bool = True, sync: bool = False) -> list[str]:
    """File one or more job links from a pasted blob. Returns the job ids.
    applied=True: you already applied, schedule the follow-up (intake helper).
    applied=False: you want the documents first (prep helper builds resume +
    cover letter, then messages you). Helpers run detached unless sync=True."""
    parsed = parse_intake(text)
    if not parsed["job_urls"]:
        raise ValueError("no job link found in what you pasted")
    con = fdb()
    ids = []
    contact_sql = ("contact_name=COALESCE(NULLIF(?,''),contact_name), "
                   "contact_email=COALESCE(NULLIF(?,''),contact_email), "
                   "contact_linkedin=COALESCE(NULLIF(?,''),contact_linkedin), "
                   "contact_phone=COALESCE(NULLIF(?,''),contact_phone)")
    contact_vals = (parsed["contact_name"], parsed["contact_email"],
                    parsed["contact_linkedin"], parsed["contact_phone"])
    for url in parsed["job_urls"]:
        jid = find_existing(con, url)
        if not jid:
            jid = job_id_for(url)
            host = urllib.parse.urlsplit(url).netloc.removeprefix("www.")
            con.execute(
                """INSERT OR IGNORE INTO jobs(id, source, company, title, location, url,
                   posted_at, salary, snippet, first_seen, passed_filter, filter_reason,
                   score, why, status, status_date, fingerprint)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, "manual", host, "(reading posting…)", "", url, today(), "", "",
                 today(), 1, "sent by text", None, "sent by text",
                 "new", today(), f"manual|{jid}"))
            log(f"file: {url} -> new row {jid}")
        else:
            log(f"file: {url} matched existing lead {jid}")
        if applied:
            con.execute(
                f"UPDATE jobs SET status='applied', status_date=?, "
                f"applied_at=COALESCE(applied_at,?), "
                f"followup_state=COALESCE(followup_state,'scheduled'), {contact_sql} WHERE id=?",
                (today(), today(), *contact_vals, jid))
        else:
            con.execute(f"UPDATE jobs SET {contact_sql} WHERE id=?", (*contact_vals, jid))
        con.commit()
        ids.append(jid)
    for jid in ids:
        helper = "intake" if applied else "prep"
        if sync:
            (cmd_intake if applied else cmd_prep)(jid)
        else:
            _spawn([helper, jid])
    return ids


def cmd_applied(text: str, sync: bool = False) -> list[str]:
    return cmd_file(text, applied=True, sync=sync)


def mark_applied(job_id: str) -> None:
    """You applied (to a prepped lead or any lead): schedule the follow-up."""
    con = fdb()
    con.execute(
        "UPDATE jobs SET status='applied', status_date=?, applied_at=COALESCE(applied_at,?), "
        "followup_state=COALESCE(followup_state,'scheduled') WHERE id=?",
        (today(), today(), job_id))
    con.commit()
    cmd_schedule(job_id)


def _docs_in(draft_path: str | None) -> list[Path]:
    """The Word documents of a package, resume first. (PDFs are not sent;
    draft_pdf=false in config.json stops making them at all.)"""
    if not draft_path or not Path(draft_path).exists():
        return []
    docs = list(Path(draft_path).glob("*.docx"))
    resumes = [d for d in docs if "resume" in d.name.lower()]
    covers = [d for d in docs if "cover" in d.name.lower()]
    return resumes[:1] + covers[:1]


def _doc_items(docs: list[Path], company: str, title: str) -> list[tuple[Path, str, str]]:
    """(path, caption, display filename). The file is named the way a recruiter
    expects to see it: 'Matthew McCusker - Resume - Acme.docx'."""
    items = []
    for d in docs:
        kind = "Cover letter" if "cover" in d.name.lower() else "Resume"
        items.append((d, f"{'✉️' if kind == 'Cover letter' else '📄'} {kind} — {title} at {company}",
                      d.name))
    return items


def cmd_prep(job_id: str) -> None:
    """Read the posting, build the tailored resume + cover letter (agent.cmd_draft:
    Word docs + PDFs into applications\\...), email them to you, and message you."""
    con = fdb()
    r = con.execute("SELECT source, title, drafted FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        log(f"prep: no job {job_id}")
        return
    source, title, drafted = r
    if (title or "").startswith("(reading"):
        cmd_intake(job_id, schedule=False)
        title = con.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
    if (title or "").startswith("(couldn't read"):
        # Never build documents against a posting we could not read.
        _message(job_id, "{h} I couldn't read that posting (the site blocked me). Reply "
                         "NAME <job title> at <company>, and paste the job description "
                         "text in a message too if you can, and I'll build the resume "
                         "and cover letter from that.")
        return
    if not drafted:
        import agent
        agent.cmd_draft(job_id)
    r = con.execute("SELECT company, title, draft_path, url FROM jobs WHERE id=?",
                    (job_id,)).fetchone()
    company, title, draft_path, url = r
    docs = _docs_in(draft_path)
    try:
        answers = json.loads((ROOT / "answers.json").read_text(encoding="utf-8"))
    except Exception:
        answers = {}
    where = ""
    if docs and answers.get("email") and all(gmail_creds()):
        try:
            send_email(answers["email"], f"Resume + cover letter: {title} at {company}",
                       f"Tailored for {title} at {company}.\n{url}\n\nFiles attached. "
                       f"They are also in {draft_path}.", answers.get("full_name", ""),
                       attachments=docs)
            where = "in your email"
        except Exception as e:
            log(f"prep: email failed (non-fatal): {e}")
    if not where:
        where = f"in {draft_path}" if docs else "not built (drafting failed, see logs)"
    _message(job_id, f"{{h}} Resume + cover letter for {title} at {company} are {where}. "
                     f"Apply when you're ready, then reply APPLIED {{h}} and I'll take it "
                     f"from there.", docs=_doc_items(docs, company, title))
    log(f"prep: {job_id} docs {where}")


def _message(job_id: str, text: str, docs: list | None = None) -> None:
    """Message you on the line (Telegram first, then Twilio), else a Windows
    toast. {h} in the text becomes the job's #n handle. On Telegram the
    documents are sent as files first."""
    import sms
    h = sms.handle_for(job_id)
    text = text.replace("{h}", h)
    try:
        import tg
        if tg.configured() and tg.chat_id():
            if docs and tg.send_documents(docs):
                text = re.sub(r" are in .*?\. Apply", " are above. Apply", text, count=1)
            tg.send(text)
            return
    except Exception as e:
        log(f"message via telegram failed (non-fatal): {e}")
    try:
        if sms.configured():
            sms.send_sms(text)
            return
    except Exception as e:
        log(f"message via phone line failed (non-fatal): {e}")
    toast("Job Leads", text.replace(f"{h} ", "")[:280], (ROOT / "applications").as_uri())


def cmd_intake(job_id: str, schedule: bool = True) -> None:
    """Read the posting behind a hand-filed link and fill in the row; then
    schedule the follow-up when it was filed as applied."""
    con = fdb()
    r = con.execute("SELECT url, title, source FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        log(f"intake: no job {job_id}")
        return
    url, title, source = r
    if source != "manual" and title and not title.startswith("(reading"):
        if schedule:
            cmd_schedule(job_id)
        return
    fetched = fetch_posting(url)
    log(f"intake: {url} via {fetched['fetched_via'] or 'nothing'} "
        f"({len(fetched['text'])} chars)")
    data = extract_posting(url, fetched)
    if not data.get("is_job_posting", True):
        # A login wall or search page came back: keep the row honest and let the
        # console's ✎ button name it, rather than filing a stranger's job.
        host = urllib.parse.urlsplit(url).netloc.removeprefix("www.")
        data.update(company=fetched.get("site_name") or host,
                    title="(couldn't read the posting)",
                    location="", salary="", summary="", recruiter_name="",
                    recruiter_email="", hiring_manager_hint="")
        fetched["text"] = ""
    con.execute(
        """UPDATE jobs SET company=?, title=?, location=?, salary=?, snippet=?,
           fingerprint=?, contact_name=COALESCE(NULLIF(contact_name,''),NULLIF(?,'')),
           contact_email=COALESCE(NULLIF(contact_email,''),NULLIF(?,'')),
           contact_role=CASE WHEN contact_role IS NULL AND ?<>'' THEN 'recruiter (named in posting)'
                             ELSE contact_role END,
           contact_notes=? WHERE id=?""",
        (data["company"][:120], data["title"][:160], data["location"][:120],
         data["salary"][:120],
         (data["summary"] + "\n\n" + (fetched["text"] or "")[:3500]).strip(),
         f"{norm(data['company'])}|{norm(data['title'])}|{norm(data['location'])}",
         data["recruiter_name"], data["recruiter_email"], data["recruiter_name"],
         json.dumps({"hiring_manager_hint": data["hiring_manager_hint"],
                     "fetched_via": fetched["fetched_via"],
                     "is_job_posting": bool(data.get("is_job_posting", True))}),
         job_id))
    con.commit()
    log(f"intake: {job_id} = {data['title']} at {data['company']}")
    if schedule:
        cmd_schedule(job_id)


# ------------------------------------------------------------------ schedule
def cmd_schedule(job_id: str) -> None:
    """Set the follow-up date from the applied date and create the two calendar
    events (when Google Calendar is connected; otherwise they wait)."""
    con = fdb()
    r = con.execute(
        """SELECT company, title, url, status_date, applied_at, followup_due,
                  followup_state, cal_applied_id, cal_followup_id FROM jobs WHERE id=?""",
        (job_id,)).fetchone()
    if not r:
        log(f"schedule: no job {job_id}")
        return
    company, title, url, sdate, applied_at, due, state, cal_a, cal_f = r
    applied_at = applied_at or sdate or today()
    if state in ("sent", "skipped"):
        return
    if not due:
        due = (datetime.strptime(applied_at, "%Y-%m-%d")
               + timedelta(days=int(CONFIG.get("followup_days", 7)))).strftime("%Y-%m-%d")
    con.execute("UPDATE jobs SET applied_at=?, followup_due=?, "
                "followup_state=COALESCE(followup_state,'scheduled') WHERE id=?",
                (applied_at, due, job_id))
    con.commit()
    if not calendar_connected():
        log(f"schedule: {job_id} due {due} (calendar not connected yet)")
        return
    try:
        if not cal_a:
            cal_a = calendar_create(
                f"Applied: {title} at {company}", applied_at,
                f"Application sent {applied_at}.\n{url}\nFollow-up due {due}.\n"
                f"Job Leads Console: {CONSOLE_URL}")
        if not cal_f:
            cal_f = calendar_create(
                f"Follow up: {title} at {company}", due,
                f"Applied {applied_at}. The console has a drafted follow-up ready "
                f"to send.\n{url}\n{CONSOLE_URL}/#followups", reminder=True)
        con.execute("UPDATE jobs SET cal_applied_id=?, cal_followup_id=? WHERE id=?",
                    (cal_a, cal_f, job_id))
        con.commit()
        log(f"schedule: {job_id} due {due}, calendar events created")
    except Exception as e:
        log(f"schedule: calendar failed for {job_id} (non-fatal): {e}")


# ------------------------------------------------------------------ drafting
DRAFT_SCHEMA = """{
  "target": {
    "channel": "email" | "linkedin",
    "name": "<person to write to, or '' if unknown>",
    "email": "<their email, ONLY if given in CONTACT or POSTING, else ''>",
    "linkedin_url": "<their LinkedIn profile URL if given, else ''>",
    "role": "<their role, e.g. 'Talent Acquisition Partner' or 'Head of Support (likely hiring manager)'>",
    "why": "<one sentence: why this person, and how to find them if not named>",
    "search_url": "<a LinkedIn people-search URL to find the right person when no name is known, else ''>"
  },
  "subject": "<email subject, under 70 chars, includes the role title>",
  "body": "<the follow-up message, 90-140 words, plain text with blank lines between paragraphs>"
}"""


def _package_context(draft_path: str | None) -> str:
    if not draft_path:
        return ""
    p = Path(draft_path)
    bits = []
    for name in ("cover_letter.md", "outreach.md"):
        f = p / name
        if f.exists():
            bits.append(f.read_text(encoding="utf-8")[:1800])
    return "\n\n".join(bits)


def cmd_draft(job_id: str, quiet: bool = False) -> dict | None:
    con = fdb()
    r = con.execute(
        """SELECT company, title, location, url, snippet, applied_at, status_date,
                  contact_name, contact_email, contact_linkedin, contact_role,
                  contact_notes, draft_path FROM jobs WHERE id=?""", (job_id,)).fetchone()
    if not r:
        log(f"draft: no job {job_id}")
        return None
    (company, title, location, url, snippet, applied_at, sdate, c_name, c_email,
     c_linkedin, c_role, c_notes, draft_path) = r
    applied_at = applied_at or sdate or today()
    facts = (ROOT / "facts.md").read_text(encoding="utf-8")
    try:
        answers = json.loads((ROOT / "answers.json").read_text(encoding="utf-8"))
    except Exception:
        answers = {}
    me = {k: answers.get(k, "") for k in ("full_name", "email", "phone", "linkedin")}
    contact = {"name": c_name or "", "email": c_email or "", "linkedin": c_linkedin or "",
               "role": c_role or "", "notes": c_notes or ""}
    days = (datetime.now() - datetime.strptime(applied_at, "%Y-%m-%d")).days
    doc = (
        "CANDIDATE (sender):\n" + json.dumps(me, ensure_ascii=False) +
        "\n\nFACTS (the only permitted source of claims about the candidate):\n" + facts +
        "\n\nJOB: " + json.dumps({"company": company, "title": title, "location": location,
                                  "url": url, "applied_on": applied_at,
                                  "days_since_applied": days}, ensure_ascii=False) +
        "\n\nCONTACT (what the candidate knows about who to write to):\n" +
        json.dumps(contact, ensure_ascii=False) +
        "\n\nPOSTING:\n" + (snippet or "")[:3500] +
        ("\n\nWHAT WAS SENT WITH THE APPLICATION:\n" + _package_context(draft_path)
         if draft_path else "")
    )
    instruction = (
        "Matthew applied for this job and it is time to follow up. Decide WHO to write "
        "to and WRITE the follow-up. Respond with ONLY a JSON object, no prose, no code "
        "fences, matching exactly:\n" + DRAFT_SCHEMA +
        "\nTarget ladder, in order: (1) the person in CONTACT if any; (2) a recruiter or "
        "email named in POSTING; (3) otherwise the likely hiring manager for this role "
        "by title, reached on LinkedIn, with a search_url like "
        "https://www.linkedin.com/search/results/people/?keywords=<company>%20<role words>. "
        "channel is 'email' only when an email address is actually known; never invent "
        "or guess an address (no careers@, no firstname.lastname@). Body rules: warm, "
        "specific, not needy; name the role and the applied date; one concrete FACTS "
        "achievement that maps to the posting; a light ask (a short call, or where they "
        "are in the process); close with the candidate's name, then LinkedIn URL on its "
        "own line. Never repeat the cover letter verbatim. No markdown."
    )
    data = _claude_json(instruction, doc, CONFIG.get("followup_model", "sonnet"), timeout=400)
    if not data or not data.get("body"):
        log(f"draft: failed for {job_id}")
        return None
    tgt = data.get("target") or {}
    if not EMAIL_RE.fullmatch(tgt.get("email") or "x"):
        tgt["email"] = ""
    # An address is only trusted if the human or the posting supplied it.
    known = {c_email or "", *EMAIL_RE.findall(snippet or "")}
    if tgt["email"] and tgt["email"] not in known:
        tgt["email"] = ""
    tgt["channel"] = "email" if tgt["email"] else "linkedin"
    if not tgt.get("linkedin_url") and c_linkedin:
        tgt["linkedin_url"] = c_linkedin
    data["target"] = tgt
    draft = {"subject": (data.get("subject") or f"Following up: {title}")[:120],
             "body": data["body"].strip(), "drafted_at": today()}
    con.execute("UPDATE jobs SET followup_target=?, followup_draft=?, followup_state='ready' "
                "WHERE id=?", (json.dumps(tgt, ensure_ascii=False),
                               json.dumps(draft, ensure_ascii=False), job_id))
    con.commit()
    if not quiet:
        log(f"draft: {title} at {company} -> {tgt['channel']} "
            f"{tgt.get('name') or tgt.get('role') or ''}")
    return {"target": tgt, **draft}


def cmd_due() -> int:
    """Called by the scheduled run. Drafts everything that is due and shows one
    toast that opens the console. Returns how many are ready."""
    con = fdb()
    # Applications marked by apply.py or the ✓ button never went through
    # cmd_applied; give them a follow-up date now.
    for (jid,) in con.execute(
            "SELECT id FROM jobs WHERE status='applied' AND followup_due IS NULL").fetchall():
        cmd_schedule(jid)
    # Rows scheduled before the calendar was connected get their events now.
    if calendar_connected():
        for (jid,) in con.execute(
                "SELECT id FROM jobs WHERE status='applied' AND followup_due IS NOT NULL "
                "AND (cal_applied_id IS NULL OR cal_followup_id IS NULL) "
                "AND COALESCE(followup_state,'scheduled')='scheduled'").fetchall():
            cmd_schedule(jid)
    due = con.execute(
        """SELECT id, company, title FROM jobs WHERE status='applied' AND followup_due<=?
           AND COALESCE(followup_state,'scheduled')='scheduled'""", (today(),)).fetchall()
    for jid, company, title in due:
        log(f"due: drafting follow-up for {title} at {company}")
        cmd_draft(jid, quiet=True)
    ready = con.execute(
        """SELECT id, company, title FROM jobs WHERE status='applied'
           AND followup_state='ready' AND COALESCE(followup_notified,'')<>?""",
        (today(),)).fetchall()
    if ready:
        texted = False
        try:
            import tg
            texted = tg.notify_ready(con, ready)
        except Exception as e:
            log(f"telegram notify failed: {e}")
        if not texted:
            try:
                import sms
                texted = sms.notify_ready(con, ready)
            except Exception as e:
                log(f"sms notify failed, falling back to toast: {e}")
        if not texted:
            if len(ready) == 1:
                body = f"{ready[0][2]} at {ready[0][1]} — a draft is ready (phone line not set up)."
            else:
                body = (f"{len(ready)} follow-ups drafted: " +
                        ", ".join(r[1] for r in ready[:4]) + " (phone line not set up).")
            toast("Time to follow up", body, (ROOT / "digests" / "latest.md").as_uri())
        con.execute("UPDATE jobs SET followup_notified=? WHERE id IN (%s)" %
                    ",".join("?" * len(ready)), (today(), *[r[0] for r in ready]))
        con.commit()
    n = con.execute("SELECT count(*) FROM jobs WHERE status='applied' "
                    "AND followup_state='ready'").fetchone()[0]
    log(f"due: {len(due)} drafted now, {n} ready to send")
    return n


# ------------------------------------------------------------------ sending
def gmail_creds() -> tuple[str, str]:
    user = CONFIG.get("gmail_user") or CONFIG.get("linkedin_imap_user") or ""
    pw = CONFIG.get("gmail_app_password") or CONFIG.get("linkedin_imap_app_password") or ""
    return user.strip(), pw.replace(" ", "").strip()


def send_email(to_addr: str, subject: str, body: str, from_name: str = "",
               attachments: list[Path] | None = None) -> None:
    user, pw = gmail_creds()
    if not user or not pw:
        raise RuntimeError("Gmail is not set up: put gmail_user and gmail_app_password "
                           "in config.json")
    if not EMAIL_RE.fullmatch(to_addr or ""):
        raise RuntimeError(f"not a valid email address: {to_addr!r}")
    msg = EmailMessage()
    msg["From"] = f"{from_name} <{user}>" if from_name else user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    import mimetypes
    for f in attachments or []:
        f = Path(f)
        if not f.exists():
            continue
        ctype, _ = mimetypes.guess_type(f.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(f.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=f.name)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=40) as s:
        s.login(user, pw)
        s.send_message(msg)


def cmd_send(job_id: str, to_addr: str | None = None, subject: str | None = None,
             body: str | None = None) -> dict:
    """Send the follow-up (the console passes the reviewed text). Marks the row
    followed-up and files a copy next to the application package."""
    con = fdb()
    r = con.execute(
        """SELECT company, title, followup_target, followup_draft, draft_path, url
           FROM jobs WHERE id=?""", (job_id,)).fetchone()
    if not r:
        raise RuntimeError(f"no job {job_id}")
    company, title, tgt_s, draft_s, draft_path, url = r
    tgt = json.loads(tgt_s or "{}")
    draft = json.loads(draft_s or "{}")
    to_addr = (to_addr or tgt.get("email") or "").strip()
    subject = (subject or draft.get("subject") or f"Following up: {title}").strip()
    body = (body or draft.get("body") or "").strip()
    if not body:
        raise RuntimeError("nothing drafted yet")
    try:
        answers = json.loads((ROOT / "answers.json").read_text(encoding="utf-8"))
        from_name = answers.get("full_name", "")
    except Exception:
        from_name = ""
    send_email(to_addr, subject, body, from_name)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    con.execute(
        """UPDATE jobs SET status='followed-up', status_date=?, followup_state='sent',
           followup_sent_at=?, contact_email=COALESCE(NULLIF(contact_email,''),?),
           followup_draft=? WHERE id=?""",
        (today(), now, to_addr,
         json.dumps({**draft, "subject": subject, "body": body, "sent_to": to_addr},
                    ensure_ascii=False), job_id))
    con.commit()
    _file_copy(draft_path, company, title, url,
               f"# Follow-up sent {now}\n\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n")
    log(f"send: {title} at {company} -> {to_addr}")
    return {"to": to_addr, "subject": subject}


def cmd_mark_sent_manually(job_id: str, note: str = "") -> None:
    """The LinkedIn path: the user sent the message themselves."""
    con = fdb()
    r = con.execute("SELECT company, title, followup_draft, draft_path, url FROM jobs "
                    "WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise RuntimeError(f"no job {job_id}")
    company, title, draft_s, draft_path, url = r
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    con.execute("UPDATE jobs SET status='followed-up', status_date=?, followup_state='sent', "
                "followup_sent_at=? WHERE id=?", (today(), now, job_id))
    con.commit()
    body = json.loads(draft_s or "{}").get("body", "")
    _file_copy(draft_path, company, title, url,
               f"# Follow-up sent {now} ({note or 'by hand'})\n\n{body}\n")


def _file_copy(draft_path: str | None, company: str, title: str, url: str, text: str) -> None:
    try:
        if draft_path and Path(draft_path).exists():
            out_dir = Path(draft_path)
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:60]
            out_dir = ROOT / "applications" / f"{today()}-{slug}"
            out_dir.mkdir(parents=True, exist_ok=True)
            if not (out_dir / "job.md").exists():
                (out_dir / "job.md").write_text(f"# {title} — {company}\n\n<{url}>\n",
                                                encoding="utf-8")
        (out_dir / "followup_sent.md").write_text(text, encoding="utf-8")
    except Exception as e:
        log(f"could not file the sent copy (non-fatal): {e}")


# ------------------------------------------------------------------ calendar
CLIENT_FILE = ROOT / "google_oauth_client.json"
TOKEN_FILE = ROOT / "google_token.json"
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def calendar_connected() -> bool:
    return TOKEN_FILE.exists() and CLIENT_FILE.exists()


def _client() -> dict:
    raw = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
    return raw.get("installed") or raw.get("web") or raw


def _token_post(data: dict) -> dict:
    req = urllib.request.Request("https://oauth2.googleapis.com/token",
                                 data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _access_token() -> str:
    tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    exp = tok.get("expires_at", 0)
    if exp - 60 > datetime.now().timestamp() and tok.get("access_token"):
        return tok["access_token"]
    c = _client()
    fresh = _token_post({"client_id": c["client_id"], "client_secret": c["client_secret"],
                         "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = datetime.now().timestamp() + int(fresh.get("expires_in", 3600))
    TOKEN_FILE.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok["access_token"]


def calendar_create(summary: str, date: str, description: str,
                    reminder: bool = False) -> str:
    cal = CONFIG.get("google_calendar_id", "primary")
    body = {"summary": summary, "description": description,
            "start": {"date": date}, "end": {"date": date},
            "transparency": "transparent"}
    if reminder:
        body["reminders"] = {"useDefault": False,
                             "overrides": [{"method": "popup", "minutes": 9 * 60}]}
    req = urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal)}/events",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_access_token()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["id"]


def cmd_calendar_connect() -> None:
    """One-time OAuth: opens the browser, catches the code on localhost, stores
    the refresh token. Needs google_oauth_client.json (Desktop app client)."""
    if not CLIENT_FILE.exists():
        print(f"missing {CLIENT_FILE.name}: create an OAuth 'Desktop app' client at "
              "https://console.cloud.google.com/apis/credentials (enable the Google "
              "Calendar API first), download the JSON, save it under that name here.")
        sys.exit(1)
    c = _client()
    code_holder: dict = {}

    class CB(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            code_holder["code"] = (q.get("code") or [""])[0]
            code_holder["error"] = (q.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h2>Google Calendar connected. You can close this tab.</h2>"
                             if code_holder["code"] else b"<h2>Authorization failed.</h2>")

    srv = HTTPServer(("127.0.0.1", 0), CB)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": CAL_SCOPE, "access_type": "offline", "prompt": "consent"})
    print("Opening your browser to authorize Google Calendar…")
    webbrowser.open(auth_url)
    srv.timeout = 300
    while "code" not in code_holder:
        srv.handle_request()
    srv.server_close()
    if not code_holder.get("code"):
        print(f"authorization failed: {code_holder.get('error')}")
        sys.exit(1)
    tok = _token_post({"code": code_holder["code"], "client_id": c["client_id"],
                       "client_secret": c["client_secret"], "redirect_uri": redirect,
                       "grant_type": "authorization_code"})
    if "refresh_token" not in tok:
        print("Google did not return a refresh token; remove the app under "
              "myaccount.google.com/permissions and run this again.")
        sys.exit(1)
    tok["expires_at"] = datetime.now().timestamp() + int(tok.get("expires_in", 3600))
    TOKEN_FILE.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    print("Connected. Creating events for anything already scheduled…")
    cmd_due()


# ------------------------------------------------------------------ toast
def toast(title: str, body: str, url: str) -> bool:
    """Windows toast that opens a URL when clicked (protocol activation)."""
    if not CONFIG.get("notify", True):
        return False
    esc = lambda s: _html.escape(s, quote=True)[:300]  # noqa: E731
    xml = (f'<toast activationType="protocol" launch="{esc(url)}" scenario="reminder">'
           f'<visual><binding template="ToastGeneric"><text>{esc(title)}</text>'
           f'<text>{esc(body)}</text></binding></visual>'
           f'<actions><action content="Review &amp; send" activationType="protocol" '
           f'arguments="{esc(url)}"/></actions></toast>')
    script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
{xml}
'@)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = "{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe"
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
'''
    try:
        ps1 = ROOT / "logs" / "notify-followup.ps1"
        ps1.parent.mkdir(exist_ok=True)
        ps1.write_text(script, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=NO_WINDOW)
        if proc.returncode != 0 or "Exception" in (proc.stderr or ""):
            log(f"toast failed (non-fatal): {(proc.stderr or proc.stdout)[:200]}")
            return False
        return True
    except Exception as e:
        log(f"toast failed (non-fatal): {e}")
        return False


# ------------------------------------------------------------------ misc
def _spawn(args: list[str]) -> None:
    logf = open(ROOT / "logs" / "followup.log", "a")
    subprocess.Popen([sys.executable, str(ROOT / "followup.py"), *args], cwd=ROOT,
                     creationflags=NO_WINDOW | 0x00000008 | 0x00000200,
                     stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    logf.close()


def cmd_selftest() -> bool:
    checks = []
    p = parse_intake("https://boards.greenhouse.io/acme/jobs/123?gh_src=abc Jane Doe "
                     "jane@acme.com https://www.linkedin.com/in/janedoe/")
    checks.append(("one job url", p["job_urls"] == ["https://boards.greenhouse.io/acme/jobs/123?gh_src=abc"]))
    checks.append(("email parsed", p["contact_email"] == "jane@acme.com"))
    checks.append(("profile url is contact, not job", p["contact_linkedin"].endswith("/in/janedoe/")))
    checks.append(("leftover words are the name", p["contact_name"] == "Jane Doe"))
    q = parse_intake("I just applied to the job, here are some contact details\n"
                     "Phone Number: 416-263-3000\nEmail: info@explace.on.ca")
    checks.append(("sentence is not a name", q["contact_name"] == ""))
    checks.append(("phone parsed", q["contact_phone"] == "416-263-3000"))
    checks.append(("email parsed from labelled text", q["contact_email"] == "info@explace.on.ca"))
    checks.append(("labelled name", parse_intake("Recruiter: Priya Shah, email priya@x.com")["contact_name"] == "Priya Shah"))
    checks.append(("'I applied to that job' has no name", parse_intake("I applied to that job")["contact_name"] == ""))
    checks.append(("lowercase chatter is not a name", parse_intake("ok done thanks")["contact_name"] == ""))
    checks.append(("multiple links", len(parse_intake("a https://x.com/1 b https://y.com/2")["job_urls"]) == 2))
    checks.append(("no link raises", parse_intake("just words")["job_urls"] == []))
    checks.append(("canonical strips tracking",
                   canonical_url("https://www.example.com/j/1/?utm_source=x&ref=y&id=7")
                   == "https://example.com/j/1?id=7"))
    checks.append(("linkedin id from view url",
                   linkedin_job_id("https://www.linkedin.com/jobs/view/senior-dev-at-acme-4012345678/?trk=x") == "4012345678"))
    checks.append(("linkedin id from search url",
                   linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=4012345678") == "4012345678"))
    checks.append(("stable id", job_id_for("https://a.com/x?utm_source=1") == job_id_for("https://a.com/x")))
    checks.append(("blocked detection", _looks_blocked(200, "Just a moment... " * 40)))
    checks.append(("not blocked", not _looks_blocked(200, "Senior Engineer role at Acme " * 40)))
    checks.append(("meta parse", _meta('<meta property="og:title" content="Dev &amp; Ops">', "og:title") == "Dev & Ops"))
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok = ok and passed
    return ok


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if cmd in ("applied", "prep-link") and len(sys.argv) > 2:
        ids = cmd_file(" ".join(a for a in sys.argv[2:] if a != "--wait"),
                       applied=(cmd == "applied"), sync="--wait" in sys.argv)
        print("filed:", ", ".join(ids))
    elif cmd == "prep" and arg:
        cmd_prep(arg)
    elif cmd == "mark-applied" and arg:
        mark_applied(arg)
    elif cmd == "intake" and arg:
        cmd_intake(arg)
    elif cmd == "schedule" and arg:
        cmd_schedule(arg)
    elif cmd == "due":
        cmd_due()
    elif cmd == "draft" and arg:
        d = cmd_draft(arg)
        if d:
            print(json.dumps(d, indent=2, ensure_ascii=False))
    elif cmd == "send" and arg:
        print(cmd_send(arg))
    elif cmd == "calendar-connect":
        cmd_calendar_connect()
    elif cmd == "selftest":
        sys.exit(0 if cmd_selftest() else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
