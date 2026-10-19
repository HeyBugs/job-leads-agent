"""Job Leads Agent v1.

Collects postings from Greenhouse/Lever/Ashby company boards (watchlist.json),
Remotive, and optionally Adzuna; dedupes into SQLite; hard-filters against
config.json; scores fit with Claude against profile.md; writes a ranked digest.

Usage:
  py -3 agent.py run            # collect + score + digest (what the schedule runs)
  py -3 agent.py collect
  py -3 agent.py score
  py -3 agent.py digest
  py -3 agent.py mark <job-id> <applied|skipped|interview|new>
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows redirects stdout to a cp1252 file, which throws on ✓, —, é, etc.
# Force UTF-8 so no log line (company names, emoji, dashes) can ever crash a run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "leads.db"
DIGEST_DIR = ROOT / "digests"
try:
    CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
except Exception as _e:
    print(f"config.json is broken ({_e}) — fix the JSON or restore it from the "
          f"OneDrive backup. Nothing will run until then.")
    sys.exit(2)
WATCHLIST = json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))
PROFILE = (ROOT / "profile.md").read_text(encoding="utf-8")


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Last-resort guard: never let a stray character crash the caller.
        enc = (getattr(sys.stdout, "encoding", None) or "ascii")
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "job-leads-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    con.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT,
            location TEXT, url TEXT, posted_at TEXT, salary TEXT, snippet TEXT,
            first_seen TEXT, passed_filter INTEGER, filter_reason TEXT,
            score INTEGER, why TEXT, flags TEXT, status TEXT DEFAULT 'new',
            fingerprint TEXT)"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_fp ON jobs(fingerprint)")
    for col in ("drafted INTEGER DEFAULT 0", "draft_path TEXT",
                "status_date TEXT", "prep_path TEXT", "skip_reason TEXT"):
        try:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return con


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def word_hit(needle: str, haystack: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


# ---------------------------------------------------------------- collectors

def collect_greenhouse(entry) -> list[dict]:
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{entry['slug']}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        posted = (j.get("first_published") or j.get("updated_at") or "")[:10]
        out.append({
            "id": f"gh:{entry['slug']}:{j.get('id')}",
            "source": "greenhouse", "company": entry["name"],
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""), "posted_at": posted,
            "salary": "", "snippet": strip_html(j.get("content", ""))[:1500],
        })
    return out


def collect_lever(entry) -> list[dict]:
    data = http_json(f"https://api.lever.co/v0/postings/{entry['slug']}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        posted = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        out.append({
            "id": f"lv:{entry['slug']}:{j.get('id')}",
            "source": "lever", "company": entry["name"],
            "title": j.get("text", ""), "location": cat.get("location", ""),
            "url": j.get("hostedUrl", ""), "posted_at": posted,
            "salary": "", "snippet": (j.get("descriptionPlain") or "")[:1500],
        })
    return out


def collect_ashby(entry) -> list[dict]:
    data = http_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{entry['slug']}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location") or ""
        secondary = "; ".join((s.get("location") or "") for s in j.get("secondaryLocations") or [])
        if secondary:
            loc = f"{loc}; {secondary}".strip("; ")
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} / Remote".strip(" /")
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append({
            "id": f"ab:{entry['slug']}:{j.get('id')}",
            "source": "ashby", "company": entry["name"],
            "title": j.get("title", ""), "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "posted_at": (j.get("publishedAt") or "")[:10],
            "salary": comp,
            "snippet": (j.get("descriptionPlain") or "")[:1500],
        })
    return out


def collect_smartrecruiters(entry) -> list[dict]:
    out = []
    for offset in (0, 100, 200):
        data = http_json(
            f"https://api.smartrecruiters.com/v1/companies/{entry['slug']}"
            f"/postings?limit=100&offset={offset}")
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(
                x for x in (loc.get("city"), loc.get("country")) if x)
            if loc.get("remote") and "remote" not in location.lower():
                location = f"{location} / Remote".strip(" /")
            out.append({
                "id": f"sr:{entry['slug']}:{j.get('id')}",
                "source": "smartrecruiters", "company": entry["name"],
                "title": j.get("name", ""), "location": location,
                "url": f"https://jobs.smartrecruiters.com/{entry['slug']}/{j.get('id')}",
                "posted_at": (j.get("releasedDate") or "")[:10],
                "salary": "", "snippet": "",
            })
        if len(items) < 100:
            break
    return out


def _parse_relative_date(s: str | None) -> str:
    m = re.search(r"(\d+)\s*(hour|day|week|month)", s or "")
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    days = 0 if unit == "hour" else n * {"day": 1, "week": 7, "month": 30}[unit]
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def collect_google_jobs() -> list[dict]:
    """Google for Jobs via SerpAPI — includes postings LinkedIn/Indeed list.
    Free key (100 searches/month): serpapi.com"""
    key = CONFIG.get("serpapi_key")
    if not key:
        log("google-jobs: no serpapi_key in config.json, skipping "
            "(free key at serpapi.com — this is the LinkedIn coverage)")
        return []
    out = []
    for q in CONFIG.get("serpapi_queries", []):
        params = {"engine": "google_jobs", "q": q.get("q", ""),
                  "location": q.get("location", "Toronto, Ontario, Canada"),
                  "api_key": key, "hl": "en"}
        try:
            data = http_json("https://serpapi.com/search.json?"
                             + urllib.parse.urlencode(params), timeout=30)
        except Exception as e:
            log(f"google-jobs '{q.get('q')}' failed: {e}")
            continue
        for j in data.get("jobs_results", []):
            ext = j.get("detected_extensions") or {}
            apply_opts = j.get("apply_options") or []
            url = (apply_opts[0].get("link") if apply_opts else "") or \
                j.get("share_link", "")
            via = j.get("via", "")
            jid = j.get("job_id") or f"{j.get('title')}|{j.get('company_name')}"
            out.append({
                "id": "sp:" + hashlib.md5(jid.encode("utf-8")).hexdigest()[:16],
                "source": "google-jobs", "company": j.get("company_name", ""),
                "title": j.get("title", ""), "location": j.get("location", ""),
                "url": url, "posted_at": _parse_relative_date(ext.get("posted_at")),
                "salary": ext.get("salary", ""),
                "snippet": (f"[{via}] " if via else "")
                           + (j.get("description") or "")[:1400],
            })
    return out


def parse_linkedin_alert_html(body: str) -> list[dict]:
    """Extract jobs from a LinkedIn job-alert email's HTML (pure, testable)."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
            r'href="(https://www\.linkedin\.com/(?:comm/)?jobs/view/'
            r'(\d+)[^"]*)"[^>]*>(.*?)</a>', body, re.S):
        jid = m.group(2)
        title = strip_html(m.group(3))[:120].strip()
        if jid in seen or len(title) < 4:
            continue
        seen.add(jid)
        out.append({
            "id": f"li:{jid}", "source": "linkedin-alert", "company": "",
            "title": title, "location": "",
            "url": f"https://www.linkedin.com/jobs/view/{jid}/",
            "posted_at": "", "salary": "", "snippet": "",
        })
    return out


def collect_linkedin_alerts() -> list[dict]:
    """Read LinkedIn's own job-alert emails from Gmail via IMAP.
    Needs a Gmail app password in config.json (see README) and LinkedIn job
    alerts turned on. This is LinkedIn content LinkedIn chose to send — no
    scraping, no account risk."""
    user = CONFIG.get("linkedin_imap_user")
    pw = CONFIG.get("linkedin_imap_app_password")
    if not user or not pw:
        log("linkedin-alerts: no Gmail IMAP creds in config.json, skipping (see README)")
        return []
    import email as email_mod
    import imaplib
    out: list[dict] = []
    seen: set[str] = set()
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com")
        box.login(user, pw)
        box.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(
            days=CONFIG.get("linkedin_alert_days", 7))).strftime("%d-%b-%Y")
        typ, ids = box.search(None, f'(FROM "linkedin.com" SINCE {since})')
        for num in (ids[0].split() if typ == "OK" else [])[-40:]:
            typ, msg_data = box.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email_mod.message_from_bytes(msg_data[0][1])
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    body += part.get_payload(decode=True).decode("utf-8", "replace")
            for job in parse_linkedin_alert_html(body):
                if job["id"] not in seen:
                    seen.add(job["id"])
                    out.append(job)
        box.logout()
        log(f"linkedin-alerts: {len(out)} jobs parsed from alert emails")
    except Exception as e:
        log(f"linkedin-alerts failed: {e}")
    return out


def collect_remotive() -> list[dict]:
    out = []
    allow = [norm(x) for x in CONFIG.get("remotive_locations_allow", [])]
    for term in CONFIG.get("remotive_searches", []):
        try:
            data = http_json(f"https://remotive.com/api/remote-jobs?search={urllib.parse.quote(term)}&limit=50")
        except Exception as e:
            log(f"remotive '{term}' failed: {e}")
            continue
        for j in data.get("jobs", []):
            required = norm(j.get("candidate_required_location"))
            if allow and required and not any(a in required for a in allow):
                continue
            out.append({
                "id": f"rm:{j.get('id')}",
                "source": "remotive", "company": j.get("company_name", ""),
                "title": j.get("title", ""),
                "location": f"Remote ({j.get('candidate_required_location', '')})",
                "url": j.get("url", ""),
                "posted_at": (j.get("publication_date") or "")[:10],
                "salary": j.get("salary", ""),
                "snippet": strip_html(j.get("description", ""))[:1500],
            })
    return out


def collect_adzuna() -> list[dict]:
    app_id, app_key = CONFIG.get("adzuna_app_id"), CONFIG.get("adzuna_app_key")
    if not app_id or not app_key:
        log("adzuna: no API keys in config.json, skipping (free key: developer.adzuna.com)")
        return []
    out = []
    for q in CONFIG.get("adzuna_queries", []):
        params = {
            "app_id": app_id, "app_key": app_key, "results_per_page": 50,
            "sort_by": "date", "max_days_old": CONFIG.get("adzuna_max_days_old", 7),
            "what": q.get("what", ""),
        }
        if q.get("where"):
            params["where"] = q["where"]
        try:
            data = http_json("https://api.adzuna.com/v1/api/jobs/ca/search/1?" + urllib.parse.urlencode(params))
        except Exception as e:
            log(f"adzuna '{q.get('what')}' failed: {e}")
            continue
        for j in data.get("results", []):
            smin, smax = j.get("salary_min"), j.get("salary_max")
            salary = f"${int(smin):,}-${int(smax):,}" if smin and smax else ""
            out.append({
                "id": f"az:{j.get('id')}",
                "source": "adzuna",
                "company": (j.get("company") or {}).get("display_name", ""),
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("display_name", ""),
                "url": j.get("redirect_url", ""),
                "posted_at": (j.get("created") or "")[:10],
                "salary": salary,
                "snippet": strip_html(j.get("description", ""))[:1500],
            })
    return out


# ------------------------------------------------------------------- filters

def passes_filter(job: dict) -> tuple[bool, str]:
    title = norm(job["title"])
    for bad in CONFIG.get("title_exclude", []):
        if word_hit(norm(bad), title):
            return False, f"title excludes '{bad}'"
    include = [norm(x) for x in CONFIG.get("title_include_any", [])]
    if include and not any(word_hit(k, title) for k in include):
        return False, "title outside target cluster"
    loc = norm(job["location"])
    if loc:
        for bad in CONFIG.get("locations_block", []):
            if norm(bad) in loc:
                return False, "blocked location"
        allow = [norm(x) for x in CONFIG.get("locations_allow", [])]
        if allow and not any(a in loc for a in allow):
            return False, f"location not allowed: {job['location']}"
    return True, ""


# ------------------------------------------------------------------ commands

ATS_PREFIX = {"greenhouse": "gh", "lever": "lv", "ashby": "ab",
              "smartrecruiters": "sr"}


def cmd_collect() -> None:
    con = db()
    fetched: list[dict] = []
    ok_prefixes: list[str] = []  # boards whose fetch succeeded — safe to prune against
    collectors = {"greenhouse": collect_greenhouse, "lever": collect_lever,
                  "ashby": collect_ashby, "smartrecruiters": collect_smartrecruiters}

    def fetch_board(entry):
        fn = collectors.get(entry.get("ats"))
        if not fn:
            return entry, None, None
        try:
            return entry, fn(entry), None
        except Exception as e:
            return entry, None, str(e)

    started = datetime.now()
    with ThreadPoolExecutor(max_workers=10) as ex:
        for entry, jobs, err in ex.map(fetch_board, WATCHLIST):
            if err:
                log(f"{entry['ats']}:{entry['slug']} failed: {err}")
            elif jobs is not None:
                fetched.extend(jobs)
                ok_prefixes.append(f"{ATS_PREFIX[entry['ats']]}:{entry['slug']}:")
    boards_secs = (datetime.now() - started).total_seconds()
    fetched.extend(collect_remotive())
    fetched.extend(collect_adzuna())
    fetched.extend(collect_google_jobs())
    fetched.extend(collect_linkedin_alerts())
    fetched_ids = {j["id"] for j in fetched}

    today = datetime.now().strftime("%Y-%m-%d")
    inserted = passed = enriched = revived = 0
    for j in fetched:
        existing = con.execute(
            "SELECT snippet, salary, passed_filter, status FROM jobs WHERE id=?",
            (j["id"],)).fetchone()
        if existing:
            if existing[3] == "gone":  # posting is back on the board
                con.execute("UPDATE jobs SET status='new' WHERE id=?", (j["id"],))
                revived += 1
            # Backfill description/salary if a source starts returning richer data
            if (not existing[0] and j["snippet"]) or (not existing[1] and j["salary"]):
                con.execute(
                    "UPDATE jobs SET snippet=COALESCE(NULLIF(snippet,''),?), "
                    "salary=COALESCE(NULLIF(salary,''),?), "
                    "score=CASE WHEN passed_filter=1 THEN NULL ELSE score END WHERE id=?",
                    (j["snippet"], j["salary"], j["id"]),
                )
                enriched += 1
            continue
        fp = f"{norm(j['company'])}|{norm(j['title'])}|{norm(j['location'])}"
        if con.execute("SELECT 1 FROM jobs WHERE fingerprint=?", (fp,)).fetchone():
            continue  # same role already seen via another source
        ok, reason = passes_filter(j)
        con.execute(
            """INSERT INTO jobs(id, source, company, title, location, url, posted_at,
               salary, snippet, first_seen, passed_filter, filter_reason, fingerprint)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (j["id"], j["source"], j["company"], j["title"], j["location"], j["url"],
             j["posted_at"], j["salary"], j["snippet"], today, 1 if ok else 0, reason, fp),
        )
        inserted += 1
        passed += 1 if ok else 0
    # Prune open leads whose posting vanished from a board we successfully fetched
    gone = 0
    for prefix in ok_prefixes:
        for (jid,) in con.execute(
                "SELECT id FROM jobs WHERE status='new' AND id LIKE ?",
                (prefix + "%",)).fetchall():
            if jid not in fetched_ids:
                con.execute("UPDATE jobs SET status='gone', status_date=? WHERE id=?",
                            (today, jid))
                gone += 1
    con.commit()
    log(f"collect: {len(fetched)} fetched (boards in {boards_secs:.0f}s), {inserted} new, "
        f"{passed} passed hard filters, {enriched} enriched, {gone} gone"
        + (f", {revived} revived" if revived else ""))


def claude_command() -> list[str]:
    cmd = CONFIG.get("claude_cmd") or shutil.which("claude")
    if not cmd:
        # Fall back to the binary bundled with the VS Code extension (any version)
        bundled = list(Path.home().glob(
            ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe"))
        if bundled:
            cmd = str(max(bundled, key=lambda p: p.stat().st_mtime))
    if not cmd:
        raise RuntimeError("claude CLI not found; set claude_cmd in config.json")
    if str(cmd).lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", str(cmd)]
    return [str(cmd)]


def cmd_score() -> None:
    con = db()
    rows = con.execute(
        """SELECT id, company, title, location, salary, snippet, posted_at FROM jobs
           WHERE passed_filter=1 AND score IS NULL
           ORDER BY first_seen DESC, posted_at DESC LIMIT ?""",
        (CONFIG.get("score_batch_limit", 40),),
    ).fetchall()
    if not rows:
        log("score: nothing new to score")
        return
    payload = [
        {"n": i, "company": r[1], "title": r[2], "location": r[3],
         "salary": r[4], "description": (r[5] or "")[:700],
         "posted_days_ago": _job_age_days(r[6])}
        for i, r in enumerate(rows)
    ]
    n_to_id = {i: r[0] for i, r in enumerate(rows)}
    pursued = [r[0] for r in con.execute(
        """SELECT title FROM jobs WHERE status IN ('applied','followed-up','interview')
           ORDER BY status_date DESC LIMIT 20""")]
    skipped = [f"{r[0]} (reason: {r[1]})" if r[1] else r[0] for r in con.execute(
        """SELECT title, skip_reason FROM jobs WHERE status='skipped'
           ORDER BY status_date DESC LIMIT 20""")]
    preference = ""
    if pursued or skipped:
        preference = (
            "\n\nPREFERENCE SIGNAL from the candidate's actual decisions — weight "
            "similar roles up or down accordingly:\n"
            + (f"PURSUED: {json.dumps(pursued)}\n" if pursued else "")
            + (f"SKIPPED: {json.dumps(skipped)}\n" if skipped else ""))
    stdin_doc = (
        "CANDIDATE PROFILE (includes scoring guidance):\n" + PROFILE + preference +
        "\n\nJOB POSTINGS (JSON):\n" + json.dumps(payload, ensure_ascii=False)
    )
    instruction = (
        "Score each job posting on stdin for fit against the candidate profile, "
        "following the profile's scoring guidance. Respond with ONLY a JSON array, "
        "no prose, no code fences: "
        '[{"n": <job number>, "score": <0-100 int>, "why": "<one specific sentence>", '
        '"flags": "<comma-separated concerns, or empty string>"}] '
        "— one object per job, every n from the input exactly once. "
        f"Freshness matters: postings under {CONFIG.get('fresh_days', 14)} days old "
        "are far more winnable — score them up; treat postings older than a month "
        "skeptically (likely filled) unless evergreen, e.g. new-grad programs."
    )
    cmd = claude_command() + ["-p", instruction, "--model", CONFIG.get("score_model", "haiku")]
    log(f"score: scoring {len(rows)} jobs with Claude ({CONFIG.get('score_model', 'haiku')})...")
    proc = subprocess.run(
        cmd, input=stdin_doc, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude scoring failed (exit {proc.returncode}): {proc.stderr[:500]}")
    m = re.search(r"\[.*\]", proc.stdout, re.S)
    if not m:
        raise RuntimeError(f"no JSON array in claude output: {proc.stdout[:500]}")
    results = json.loads(m.group(0))
    updated = 0
    for item in results:
        job_id = n_to_id.get(item.get("n"))
        if job_id is None:
            continue
        con.execute(
            "UPDATE jobs SET score=?, why=?, flags=? WHERE id=?",
            (int(item.get("score", 0)), item.get("why", ""), item.get("flags", ""), job_id),
        )
        updated += 1
    con.commit()
    log(f"score: updated {updated}/{len(rows)} jobs")


def _job_age_days(posted_at: str) -> int | None:
    try:
        return (datetime.now() - datetime.strptime(posted_at, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def _docx_to_pdf(docx_path: Path) -> Path | None:
    pdf_path = docx_path.with_suffix(".pdf")
    script = (
        f"$w = New-Object -ComObject Word.Application; $w.Visible = $false; "
        f"try {{ $d = $w.Documents.Open('{docx_path}'); "
        f"$d.SaveAs([ref]'{pdf_path}', [ref]17); $d.Close($false) }} finally {{ $w.Quit() }}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=120)
        return pdf_path if pdf_path.exists() else None
    except Exception:
        return None


def _notify(title: str, body: str) -> None:
    if not CONFIG.get("notify", True):
        return
    # The text lands inside a PowerShell double-quoted string: neutralise quotes,
    # $ (variable expansion) and backticks (escape char) from job titles.
    title, body = (s.replace('"', "'").replace("`", "").replace("$", "`$")[:200]
                   for s in (title, body))
    script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName("text")
$texts.Item(0).AppendChild($xml.CreateTextNode("{title}")) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode("{body}")) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = "{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe"
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
'''
    # Run from a .ps1 file: passing the script via -Command mangles the quotes
    # inside it (PowerShell strips them), which made the toast fail silently.
    try:
        ps1 = ROOT / "logs" / "notify.ps1"
        ps1.parent.mkdir(exist_ok=True)
        ps1.write_text(script, encoding="utf-8-sig")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        if proc.returncode != 0 or "Exception" in (proc.stderr or ""):
            log(f"notify failed (non-fatal): {(proc.stderr or proc.stdout)[:200]}")
            return False
        return True
    except Exception as e:
        log(f"notify failed (non-fatal): {e}")
        return False


DRAFT_SCHEMA = """{
  "title_line": "<Job Title> | <2-4 word positioning angle>",
  "location_line": "Toronto, ON | <availability phrasing that fits this job's location/remote setup>",
  "profile": "<3-4 sentence tailored summary>",
  "role_match": [{"label": "<2-4 words>", "text": "<one sentence>"} /* exactly 4 */],
  "experience": [{"title": "...", "company": "...", "dates": "...", "bullets": ["..."]} /* all 4 roles from FACTS, most relevant first, 2-5 tailored bullets each */],
  "education_note": "<one line starting 'Foundation includes...' listing the coursework areas most relevant to this job — NEVER repeat the degree name or university, the template already prints them>",
  "keywords": "<one comma-separated ATS keyword line drawn from FACTS skills + this job's language>",
  "cover_letter": "<3 short paragraphs, plain text, addressed to the hiring team>",
  "outreach": "<a 60-100 word LinkedIn message Matthew can send to a recruiter or hiring manager at this company: friendly, specific to this role, mentions ONE concrete FACTS achievement, asks for a conversation — plain text>"
}"""


def _critique_draft(data: dict, facts: str, job_info: dict) -> dict | None:
    """Second pass: review the draft as the hiring manager and return an improved version."""
    stdin_doc = (
        "FACTS (the only permitted source of resume content):\n" + facts +
        "\n\nJOB POSTING:\n" + json.dumps(job_info, ensure_ascii=False) +
        "\n\nDRAFT RESUME CONTENT (JSON):\n" + json.dumps(data, ensure_ascii=False)
    )
    instruction = (
        "You are the hiring manager for this exact job. Critique the draft resume content on "
        "stdin, then return an improved version. Respond with ONLY a JSON object in exactly the "
        "same schema as the draft (same keys, same structure, cover_letter included). Improve: "
        "emphasis and ordering for THIS role, terminology mirrored from the posting, weak or "
        "generic wording, ATS keyword coverage. Constraints: every claim must still come from "
        "FACTS — never add facts; obey the Hard rules section of FACTS; do not grow total "
        "length (one page)."
    )
    cmd = claude_command() + ["-p", instruction, "--model", CONFIG.get("draft_model", "sonnet")]
    try:
        proc = subprocess.run(cmd, input=stdin_doc, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        m = re.search(r"\{.*\}", proc.stdout, re.S)
        if proc.returncode != 0 or not m:
            return None
        revised = json.loads(m.group(0))
        required = ("title_line", "profile", "role_match", "experience", "keywords")
        return revised if all(revised.get(k) for k in required) else None
    except Exception:
        return None


def cmd_draft(job_id: str | None = None) -> None:
    con = db()
    threshold = CONFIG.get("draft_threshold", 80)
    stale_days = CONFIG.get("stale_days", 60)
    if job_id:
        # Explicit request (console 📦 Build button): no threshold, cap, or stale rule
        rows = con.execute(
            """SELECT id, company, title, location, salary, snippet, url, posted_at,
                      score, why FROM jobs WHERE id=?""", (job_id,)).fetchall()
        if not rows:
            log(f"draft: no job with id {job_id}")
            return
    else:
        rows = con.execute(
            """SELECT id, company, title, location, salary, snippet, url, posted_at, score, why
               FROM jobs WHERE passed_filter=1 AND status='new' AND drafted=0
               AND score>=? ORDER BY score DESC, posted_at DESC""",
            (threshold,),
        ).fetchall()
        rows = [r for r in rows
                if (_job_age_days(r[7]) is None or _job_age_days(r[7]) <= stale_days)]
        rows = rows[: CONFIG.get("draft_max_per_run", 3)]
    if not rows:
        log("draft: no fresh leads at or above threshold")
        return
    facts = (ROOT / "facts.md").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    from resume_template import build_resume

    for r in rows:
        _id, company, title, location, salary, snippet, url, posted, score, why = r
        job_info = {
            "company": company, "title": title, "location": location,
            "salary": salary, "url": url, "description": (snippet or "")[:1500],
        }
        stdin_doc = (
            "FACTS (the only permitted source of resume content):\n" + facts +
            "\n\nJOB POSTING:\n" + json.dumps(job_info, ensure_ascii=False)
        )
        instruction = (
            "Write tailored resume content and a cover letter for the job posting on stdin, "
            "for the candidate described in FACTS. Respond with ONLY a JSON object, no prose, "
            "no code fences, matching this schema exactly:\n" + DRAFT_SCHEMA +
            "\nRules: every claim must come from FACTS (reorder, reword, emphasize — never "
            "invent employers, dates, tools, certifications, languages, or metrics). Obey the "
            "Hard rules section of FACTS. Plain professional tone. Cover letter: specific to "
            "this company and role, references 1-2 concrete FACTS achievements, under 250 words."
        )
        cmd = claude_command() + ["-p", instruction, "--model", CONFIG.get("draft_model", "sonnet")]
        log(f"draft: {company} — {title} (score {score})...")
        try:
            data = None
            for attempt in range(3):  # the model occasionally returns broken JSON
                proc = subprocess.run(cmd, input=stdin_doc, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=600)
                m = re.search(r"\{.*\}", proc.stdout, re.S)
                if proc.returncode != 0 or not m:
                    log(f"draft: attempt {attempt + 1} failed for {_id}: "
                        f"{(proc.stderr or proc.stdout)[:300]}")
                    continue
                try:
                    data = json.loads(m.group(0), strict=False)
                    break
                except json.JSONDecodeError as je:
                    log(f"draft: attempt {attempt + 1} returned broken JSON for {_id}: {je}")
            if data is None:
                log(f"draft: FAILED for {_id} after 3 attempts")
                continue
            if CONFIG.get("draft_critic", True):
                improved = _critique_draft(data, facts, job_info)
                if improved:
                    data = improved
                    log("draft: critic pass applied")
                else:
                    log("draft: critic pass failed, keeping first draft")
            slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:60]
            out_dir = ROOT / "applications" / f"{datetime.now().strftime('%Y-%m-%d')}-{slug}"
            out_dir.mkdir(parents=True, exist_ok=True)
            # Named the way a recruiter expects to see it in their downloads.
            co = re.sub(r'[\\/:*?"<>|]+', " ", company or "").strip().rstrip(".")[:40]
            docx_path = out_dir / f"Matthew McCusker - Resume - {co}.docx"
            build_resume(data, docx_path)
            if CONFIG.get("draft_pdf", True):
                _docx_to_pdf(docx_path)
            (out_dir / "cover_letter.md").write_text(
                f"# Cover letter — {title} at {company}\n\n{data.get('cover_letter', '')}\n",
                encoding="utf-8")
            try:
                from resume_template import build_cover_letter
                cl_docx = out_dir / f"Matthew McCusker - Cover Letter - {co}.docx"
                build_cover_letter(data.get("cover_letter", ""), cl_docx)
                if CONFIG.get("draft_pdf", True):
                    _docx_to_pdf(cl_docx)
            except Exception as e:
                log(f"cover letter document failed (non-fatal): {e}")
            if data.get("outreach"):
                (out_dir / "outreach.md").write_text(
                    f"# LinkedIn outreach — {title} at {company}\n\n"
                    "Send to a recruiter or the hiring manager at "
                    f"{company} (find them via the company's LinkedIn page):\n\n"
                    f"{data['outreach']}\n", encoding="utf-8")
            (out_dir / "job.md").write_text(
                f"# {title} — {company}\n\n{location} | {salary or 'salary unlisted'} | "
                f"posted {posted}\nScore {score}: {why}\n\n<{url}>\n\n---\n\n{snippet}\n",
                encoding="utf-8")
            con.execute("UPDATE jobs SET drafted=1, draft_path=? WHERE id=?",
                        (str(out_dir), _id))
            con.commit()
            log(f"draft: -> {out_dir}")
        except Exception as e:
            log(f"draft: FAILED for {_id}: {e}")


def cmd_digest() -> None:
    con = db()
    cutoff = (datetime.now() - timedelta(days=CONFIG.get("digest_window_days", 3))).strftime("%Y-%m-%d")
    rows = con.execute(
        """SELECT id, company, title, location, url, posted_at, salary, score, why, flags,
                  draft_path
           FROM jobs WHERE passed_filter=1 AND score IS NOT NULL AND status='new'
           AND first_seen>=? ORDER BY score DESC, posted_at DESC LIMIT ?""",
        (cutoff, CONFIG.get("digest_max_jobs", 25)),
    ).fetchall()
    hot_min = CONFIG.get("hot_threshold", 75)
    look_min = CONFIG.get("worth_a_look_threshold", 60)
    stale_days = CONFIG.get("stale_days", 60)
    today = datetime.now().strftime("%Y-%m-%d")

    def is_stale(r) -> bool:
        age = _job_age_days(r[5])
        return age is not None and age > stale_days

    def fmt(r) -> str:
        _id, company, title, location, url, posted, salary, score, why, flags, draft = r
        age = _job_age_days(posted)
        posted_note = f" | posted {posted}" + (f" ({age}d ago)" if age is not None else "")
        lines = [f"### {title} — {company}  `{score}`",
                 f"{location or 'location unlisted'}" +
                 (posted_note if posted else "") +
                 (f" | {salary}" if salary else ""),
                 f"{why}"]
        if flags:
            lines.append(f"⚠ {flags}")
        if is_stale(r):
            lines.append(f"⏳ posting has been open {age} days — may be evergreen or filled")
        if draft:
            lines.append(f"📄 tailored resume + cover letter ready: `{draft}`")
        lines.append(f"<{url}>")
        lines.append(f"Apply (auto-fills the form): `py -3 apply.py {_id}` · "
                     f"applied another way? `py -3 agent.py mark {_id} applied`")
        return "\n".join(lines) + "\n"

    hot = [r for r in rows if r[7] >= hot_min and not is_stale(r)]
    look = [r for r in rows
            if (look_min <= r[7] < hot_min) or (r[7] >= hot_min and is_stale(r))]
    n_rest = len(rows) - len(hot) - len(look)
    out = [f"# Job leads — {today}", ""]
    out.append(f"## 🔥 Hot ({len(hot)})\n" if hot else "## 🔥 Hot\n\nNothing above "
               f"{hot_min} today.\n")
    out.extend(fmt(r) for r in hot)
    if look:
        out.append(f"## Worth a look ({len(look)})\n")
        out.extend(fmt(r) for r in look)
    if n_rest:
        out.append(f"\n_{n_rest} more scored below {look_min}; see leads.db._")

    try:
        pipe = con.execute(
            """SELECT id, company, title, status, status_date, prep_path, followup_due,
                      followup_state FROM jobs
               WHERE status NOT IN ('new', 'skipped', 'closed', 'rejected', 'gone')
               ORDER BY status_date DESC""").fetchall()
    except sqlite3.OperationalError:  # followup.py has not created its columns yet
        pipe = [tuple(r) + (None, None) for r in con.execute(
            """SELECT id, company, title, status, status_date, prep_path FROM jobs
               WHERE status NOT IN ('new', 'skipped', 'closed', 'rejected', 'gone')
               ORDER BY status_date DESC""").fetchall()]
    if pipe:
        followup_days = CONFIG.get("followup_days", 7)
        moveon_days = CONFIG.get("moveon_days", 10)
        out.append(f"\n## Pipeline ({len(pipe)})\n")
        for _id, company, title, status, sdate, prep, fu_due, fu_state in pipe:
            age = _job_age_days(sdate) if sdate else None
            out.append(f"- **{title} — {company}**: {status}" +
                       (f" since {sdate} ({age}d)" if age is not None else ""))
            if status == "applied" and fu_state == "ready":
                out.append("  - ✉ **follow-up drafted** — it was texted to you; reply SEND")
            elif status == "applied" and fu_state == "scheduled" and fu_due:
                out.append(f"  - follow-up scheduled for {fu_due} (texted to you that morning)")
            elif status == "applied" and age is not None and age >= followup_days:
                out.append(f"  - ⏰ **follow up now**: text DRAFT to the line, or `py -3 followup.py draft {_id}`")
            if status == "followed-up" and age is not None and age >= moveon_days:
                out.append(f"  - ⏳ no reply {age}d after follow-up — keep momentum elsewhere; "
                           f"`py -3 agent.py mark {_id} closed` when ready")
            if status == "interview":
                out.append(f"  - 🎤 prep pack: `{prep}`" if prep else
                           f"  - 🎤 generate prep: `py -3 agent.py prep {_id}`")
        out.append("")
    text = "\n".join(out)

    DIGEST_DIR.mkdir(exist_ok=True)
    (DIGEST_DIR / f"digest-{today}.md").write_text(text, encoding="utf-8")
    (DIGEST_DIR / "latest.md").write_text(text, encoding="utf-8")
    log(f"digest: {len(hot)} hot, {len(look)} worth a look -> digests/digest-{today}.md")
    if hot:
        # Only toast when the hot set gains NEW leads — catch-up reruns stay silent
        state_file = ROOT / "digest_state.json"
        hot_ids = sorted(r[0] for r in hot)
        prev: list = []
        try:
            prev = json.loads(state_file.read_text(encoding="utf-8")).get("notified", [])
        except Exception:
            pass
        fresh = [r for r in hot if r[0] not in set(prev)]
        if fresh:
            top = "; ".join(f"{r[2]} @ {r[1]}" for r in fresh[:3])
            _notify(f"{len(fresh)} new hot job lead{'s' if len(fresh) != 1 else ''} "
                    f"({len(hot)} total)", top)
        state_file.write_text(json.dumps({"notified": hot_ids}), encoding="utf-8")


def funnel_stats(con) -> dict:
    g = lambda q, *a: con.execute(q, a).fetchone()[0]  # noqa: E731
    return {
        "jobs_seen": g("SELECT count(*) FROM jobs"),
        "passed_filters": g("SELECT count(*) FROM jobs WHERE passed_filter=1"),
        "hot_open": g("SELECT count(*) FROM jobs WHERE status='new' AND score>=?",
                      CONFIG.get("hot_threshold", 75)),
        "packages_ready": g("SELECT count(*) FROM jobs WHERE drafted=1 AND status='new'"),
        "applied": g("SELECT count(*) FROM jobs WHERE status IN "
                     "('applied','followed-up','check')"),
        "interviews": g("SELECT count(*) FROM jobs WHERE status='interview'"),
        "skipped": g("SELECT count(*) FROM jobs WHERE status='skipped'"),
    }


def cmd_coach(force: bool = False) -> None:
    """One short daily briefing from an agent that reads the whole funnel."""
    coach_file = ROOT / "coach.md"
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and coach_file.exists() and \
            datetime.fromtimestamp(coach_file.stat().st_mtime).strftime("%Y-%m-%d") == today:
        log("coach: today's briefing already written")
        return
    con = db()
    stats = funnel_stats(con)
    hot = con.execute(
        """SELECT title, company, score, salary, posted_at, drafted, status_date
           FROM jobs WHERE status='new' AND score>=? ORDER BY score DESC LIMIT 15""",
        (CONFIG.get("hot_threshold", 75),)).fetchall()
    pipe = con.execute(
        """SELECT title, company, status, status_date FROM jobs
           WHERE status NOT IN ('new','skipped','closed','rejected','gone')
           ORDER BY status_date DESC LIMIT 20""").fetchall()
    skipped = [r[0] for r in con.execute(
        "SELECT title FROM jobs WHERE status='skipped' ORDER BY status_date DESC LIMIT 15")]
    oldest_ready = con.execute(
        "SELECT min(first_seen) FROM jobs WHERE drafted=1 AND status='new'"
    ).fetchone()[0]
    waiting_days = _job_age_days(oldest_ready) if oldest_ready else None
    stdin_doc = json.dumps({
        "today": today, "funnel": stats,
        "oldest_ready_package_has_waited_days": waiting_days,
        "hot_open_leads": [dict(title=t, company=c, score=s, salary=sal or None,
                                posted=p, package_ready=bool(d)) for
                           t, c, s, sal, p, d, _ in hot],
        "in_flight": [dict(title=t, company=c, status=st, since=sd)
                      for t, c, st, sd in pipe],
        "recently_skipped": skipped,
    }, ensure_ascii=False)
    instruction = (
        "You are the coach agent for Matthew's job hunt (junior solutions/support/"
        "developer/data roles, Toronto or remote Canada; he runs this automated "
        "lead pipeline). The JSON on stdin is the live state. Write today's briefing "
        "as PLAIN TEXT, max 130 words, brutally practical, no flattery, no markdown "
        "headers. Format exactly:\n"
        "One to three sentences reading the state (call out the real bottleneck).\n"
        "Then 2-4 lines, each starting with '- ', the highest-leverage concrete "
        "actions for TODAY (name specific leads).\n"
        "If his decisions suggest a pattern (e.g. skipping all sales roles), say it "
        "and suggest what to change in one line. If ready packages have waited more "
        "than 2 days with zero applications sent, that stagnation IS the story — "
        "name the number of wasted days plainly and make sending the only priority."
    )
    try:
        proc = subprocess.run(
            claude_command() + ["-p", instruction,
                                "--model", CONFIG.get("coach_model", "sonnet")],
            input=stdin_doc, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        text = proc.stdout.strip()
        if proc.returncode != 0 or not (40 < len(text) < 2000):
            log(f"coach: FAILED: {(proc.stderr or text)[:200]}")
            return
        coach_file.write_text(f"{today}\n{text}\n", encoding="utf-8")
        log("coach: briefing written")
    except Exception as e:
        log(f"coach: FAILED: {e}")


def cmd_selftest(quiet: bool = False) -> bool:
    """Fast invariant checks — no network, no LLM. Returns True when all pass."""
    cases = [
        # (job fields, expected pass) — the regression suite for passes_filter
        ({"title": "Junior Developer", "company": "X", "location": "Toronto, ON"}, True),
        ({"title": "Senior Developer", "company": "X", "location": "Toronto, ON"}, False),
        ({"title": "Software Engineer II", "company": "X", "location": "Toronto"}, False),
        ({"title": "Software Developer Internships (Winter 2027)", "company": "X",
          "location": "Toronto"}, False),
        ({"title": "Marketing Manager", "company": "X", "location": "Toronto"}, False),
        ({"title": "Support Analyst", "company": "X", "location": "Remote Poland"}, False),
        ({"title": "Support Analyst", "company": "X", "location": "Remote (Canada)"}, True),
        ({"title": "Data Analyst", "company": "X", "location": ""}, True),
    ]
    failures: list[str] = []
    for job, expected in cases:
        got, _ = passes_filter(job)
        if got != expected:
            failures.append(f"passes_filter({job['title']!r} @ {job['location']!r}) "
                            f"= {got}, expected {expected}")
    if _parse_relative_date("3 days ago") != \
            (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"):
        failures.append("_parse_relative_date('3 days ago') wrong")
    if word_hit("intern", "internship"):
        failures.append("word_hit boundary broken: 'intern' matched 'internship'")
    sample_alert = (
        '<a href="https://www.linkedin.com/comm/jobs/view/412345?tracking=x" '
        'style="x">Junior <b>Developer</b> — Acme</a> '
        '<a href="https://www.linkedin.com/jobs/view/412345?dup=1">dupe</a> '
        '<a href="https://www.linkedin.com/jobs/view/98765?t=y">Support Analyst</a>')
    parsed = parse_linkedin_alert_html(sample_alert)
    if len(parsed) != 2 or parsed[0]["title"] != "Junior Developer — Acme" \
            or parsed[1]["id"] != "li:98765":
        failures.append(f"parse_linkedin_alert_html wrong: {parsed}")
    for key in ("title_line", "role_match", "experience", "keywords", "cover_letter",
                "outreach"):
        if f'"{key}"' not in DRAFT_SCHEMA:
            failures.append(f"DRAFT_SCHEMA missing {key}")
    try:
        db().close()
        db().close()  # migrations must be idempotent
    except Exception as e:
        failures.append(f"db() failed: {e}")
    # log() must survive Unicode even when stdout is a cp1252 file (the crash that
    # killed every apply session: ✓ and — could not be encoded and closed the browser).
    import io
    real = sys.stdout
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        log("encoding guard ✓ — é ✕ 🎯")
    except Exception as e:
        failures.append(f"log() crashes on Unicode under cp1252: {e}")
    finally:
        sys.stdout = real
    for f in failures:
        log(f"selftest FAIL: {f}")
    if not failures and not quiet:
        log(f"selftest: all {len(cases) + 6} checks passed")
    return not failures


def cmd_status() -> None:
    """Plain-language one-screen status of the whole system."""
    con = db()
    s = funnel_stats(con)
    try:
        last = datetime.fromtimestamp(
            (ROOT / "digests" / "latest.md").stat().st_mtime).strftime("%b %d %H:%M")
    except OSError:
        last = "never"
    by_ats: dict[str, int] = {}
    for w in WATCHLIST:
        by_ats[w["ats"]] = by_ats.get(w["ats"], 0) + 1
    keys = {
        "Adzuna (Indeed-ish coverage)": bool(CONFIG.get("adzuna_app_id")),
        "SerpAPI (Google Jobs / LinkedIn)": bool(CONFIG.get("serpapi_key")),
        "LinkedIn alert emails (Gmail IMAP)": bool(CONFIG.get("linkedin_imap_user")),
    }
    print(f"""
JOB LEADS AGENT — status
  Last pipeline run : {last}   (scheduled 8:00, 13:00, and at every login)
  Funnel            : {s['jobs_seen']} seen -> {s['passed_filters']} passed filters """
          f"""-> {s['hot_open']} hot open -> {s['applied']} applied -> {s['interviews']} interview
  Packages ready    : {s['packages_ready']} (resume + cover letter + outreach, waiting to be sent)
  Skipped by you    : {s['skipped']}
  Companies watched : {len(WATCHLIST)} ({', '.join(f'{k}: {v}' for k, v in sorted(by_ats.items()))})
  Optional sources  : """ + "; ".join(
              f"{name}: {'ON' if on else 'off — key not set'}" for name, on in keys.items())
          + """
  Phone line        : """ + (CONFIG.get("twilio_from") or "not set up — see README, Follow-ups by text") + """
  Auto-apply in runs: """ + ("ON" if CONFIG.get("auto_apply_in_run") else "off")
          + "\n  Docs              : README.md (how to use) · SYSTEM.md (how it works)\n")


def cmd_refilter() -> None:
    """Re-apply hard filters to every stored job after a config change."""
    con = db()
    rows = con.execute("SELECT id, company, title, location FROM jobs").fetchall()
    changed = 0
    for _id, company, title, location in rows:
        ok, reason = passes_filter({"company": company, "title": title, "location": location})
        cur = con.execute("SELECT passed_filter FROM jobs WHERE id=?", (_id,)).fetchone()[0]
        if cur != (1 if ok else 0):
            con.execute("UPDATE jobs SET passed_filter=?, filter_reason=? WHERE id=?",
                        (1 if ok else 0, reason, _id))
            changed += 1
    con.commit()
    total = con.execute("SELECT count(*) FROM jobs WHERE passed_filter=1").fetchone()[0]
    log(f"refilter: {changed} rows changed, {total} now pass")


def cmd_prep(job_id: str) -> None:
    """Build an interview prep pack (uses web search for company research)."""
    con = db()
    r = con.execute(
        "SELECT company, title, location, salary, snippet, url, draft_path "
        "FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        log(f"prep: no job with id {job_id}")
        return
    company, title, location, salary, snippet, url, draft_path = r
    facts = (ROOT / "facts.md").read_text(encoding="utf-8")
    stdin_doc = (
        "CANDIDATE FACTS:\n" + facts + "\n\nJOB:\n" + json.dumps({
            "company": company, "title": title, "location": location,
            "salary": salary, "url": url, "description": (snippet or "")[:1500],
        }, ensure_ascii=False)
    )
    instruction = (
        "Build an interview prep pack for the job on stdin, as plain Markdown (no JSON, no "
        "code fences). Use web search to research the company — product, size, recent news, "
        "competitors; when something cannot be verified, say so instead of guessing. Sections: "
        "1) Company snapshot; 2) What this role really is and what they will probe; 3) Likely "
        "interview questions WITH suggested answers as STAR stories drawn ONLY from CANDIDATE "
        "FACTS; 4) A strong 'why this company' answer; 5) Smart questions to ask them; "
        "6) Watch-outs: gaps they may probe and honest ways to address each."
    )
    cmd = claude_command() + ["-p", instruction, "--model", CONFIG.get("prep_model", "sonnet"),
                              "--allowedTools", "WebSearch"]
    log(f"prep: researching {company} — {title}...")
    proc = subprocess.run(cmd, input=stdin_doc, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    if proc.returncode != 0 or len(proc.stdout.strip()) < 300:
        log(f"prep: FAILED: {(proc.stderr or proc.stdout)[:300]}")
        return
    if draft_path and Path(draft_path).exists():
        out_dir = Path(draft_path)
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:60]
        out_dir = ROOT / "applications" / f"prep-{slug}"
        out_dir.mkdir(parents=True, exist_ok=True)
    prep_file = out_dir / "interview_prep.md"
    prep_file.write_text(proc.stdout, encoding="utf-8")
    con.execute("UPDATE jobs SET prep_path=? WHERE id=?", (str(prep_file), job_id))
    con.commit()
    log(f"prep: -> {prep_file}")


def cmd_mark(job_id: str, status: str) -> None:
    con = db()
    n = con.execute("UPDATE jobs SET status=?, status_date=? WHERE id=?",
                    (status, datetime.now().strftime("%Y-%m-%d"), job_id)).rowcount
    con.commit()
    log(f"mark: {job_id} -> {status}" if n else f"mark: no job with id {job_id}")
    if n and status == "interview":
        try:
            cmd_prep(job_id)
        except Exception as e:
            log(f"prep failed (run manually: py -3 agent.py prep {job_id}): {e}")


def _rotate_logs() -> None:
    for name in ("agent.log", "console.log", "console-actions.log", "scout.log"):
        f = ROOT / "logs" / name
        try:
            if f.exists() and f.stat().st_size > 2_000_000:
                f.write_bytes(f.read_bytes()[-500_000:])
        except Exception:
            pass


def _backup_db() -> None:
    dest_dir = Path(r"C:\Users\matth\OneDrive\Documents\job-leads-agent-backup")
    try:
        if not dest_dir.parent.exists():
            return
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"leads-{datetime.now().strftime('%Y%m%d')}.db"
        if not dest.exists():
            shutil.copy2(DB_PATH, dest)
            for old in sorted(dest_dir.glob("leads-*.db"))[:-7]:
                old.unlink()
            apps = ROOT / "applications"
            if apps.exists():
                shutil.copytree(apps, dest_dir / "applications", dirs_exist_ok=True)
            log(f"backup: {dest.name} + applications -> OneDrive (keeping last 7 DBs)")
    except Exception as e:
        log(f"backup failed (non-fatal): {e}")


def main() -> None:
    _rotate_logs()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        if not cmd_selftest(quiet=True):
            log("selftest failures above — continuing, but review before trusting output")
        try:  # follow-ups first: they are due regardless of what the scout finds
            subprocess.run([sys.executable, str(ROOT / "followup.py"), "due"], timeout=1800)
        except Exception as e:
            log(f"follow-up check failed (non-fatal): {e}")
        cmd_collect()
        cmd_score()
        cmd_draft()
        if CONFIG.get("auto_apply_in_run"):
            try:
                subprocess.run([sys.executable, str(ROOT / "apply.py"), "--auto-all"],
                               timeout=3600)
            except Exception as e:
                log(f"auto-apply failed (non-fatal): {e}")
        cmd_coach()
        cmd_digest()
        _backup_db()
    elif cmd == "collect":
        cmd_collect()
    elif cmd == "score":
        cmd_score()
    elif cmd == "draft":
        cmd_draft(sys.argv[2] if len(sys.argv) >= 3 else None)
    elif cmd == "digest":
        cmd_digest()
    elif cmd == "refilter":
        cmd_refilter()
    elif cmd == "prep" and len(sys.argv) >= 3:
        cmd_prep(sys.argv[2])
    elif cmd == "coach":
        cmd_coach(force=True)
    elif cmd == "status":
        cmd_status()
    elif cmd == "selftest":
        sys.exit(0 if cmd_selftest() else 1)
    elif cmd == "mark" and len(sys.argv) >= 4:
        cmd_mark(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
