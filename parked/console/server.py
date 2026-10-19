"""Local console server for the Job Leads Agent.

Serves the interactive dashboard at http://127.0.0.1:8787 and handles its
buttons: marking leads, launching the apply assistant, opening application
folders, and running the pipeline on demand. Localhost only. Started
automatically at login by the JobLeadsConsole scheduled task.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import dashboard  # noqa: E402
import followup  # noqa: E402
from agent import claude_command, funnel_stats  # noqa: E402

PORT = 8787
VALID_STATUS = {"new", "applied", "followed-up", "interview", "closed",
                "rejected", "skipped", "check"}
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

SETUP_CONFIG_KEYS = ("adzuna_app_id", "adzuna_app_key", "serpapi_key",
                     "linkedin_imap_user", "linkedin_imap_app_password",
                     "gmail_user", "gmail_app_password", "followup_days",
                     "twilio_account_sid", "twilio_auth_token", "twilio_from", "my_phone")
SETUP_ANSWER_KEYS = ("first_name", "last_name", "email", "phone", "location",
                     "linkedin", "pronouns")


def _answers() -> dict:
    return json.loads((ROOT / "answers.json").read_text(encoding="utf-8"))


def _rule_answer(ans: dict, pat: str) -> str:
    for r in ans.get("yes_no_rules", []):
        if pat in r["pattern"]:
            return r.get("answer", "")
    return ""


def build_setup_page(saved: bool) -> str:
    import html as _h
    cfg, ans = config(), _answers()

    def txt(name, label, value="", placeholder="", note=""):
        return (f'<label>{label}'
                + (f' <span class="mut">{note}</span>' if note else "")
                + f'<input name="{name}" value="{_h.escape(value)}" '
                  f'placeholder="{_h.escape(placeholder)}"></label>')

    def secret(name, label, is_set, note=""):
        ph = "•••••• set — leave blank to keep" if is_set else "not set yet"
        return txt(name, label, "", ph, note)

    def yn(name, label, current):
        opts = "".join(f'<option{" selected" if o == current else ""}>{o}</option>'
                       for o in ("Yes", "No"))
        return f'<label>{label}<select name="{name}">{opts}</select></label>'

    pron = ans.get("pronouns", "")
    pron_opts = "".join(
        f'<option value="{o}"{" selected" if o == pron else ""}>{o or "Ask me each time"}'
        f'</option>' for o in ("", "He/Him", "She/Her", "They/Them"))
    banner = '<div class="ok">✓ Saved. New values apply from the next run/apply.</div>' \
        if saved else ""
    gmail_user, gmail_pw = followup.gmail_creds()
    if followup.calendar_connected():
        cal_html = ('<p class="mut">✓ Google Calendar connected. Applied and follow-up '
                    'events are created for every application.</p>')
    elif followup.CLIENT_FILE.exists():
        cal_html = ('<p class="mut">Client file found. Click to authorize (a browser tab '
                    'opens):</p><button type="button" onclick="fetch(\'/calendar/connect\','
                    '{method:\'POST\'}).then(()=>alert(\'Check your browser for the Google '
                    'sign-in tab. This page will show connected once you finish.\'))">'
                    'Connect Google Calendar</button>')
    else:
        cal_html = ('<p class="mut">Not connected (optional, ~5 min once). Steps: '
                    '<a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" '
                    'target="_blank">enable the Google Calendar API</a> in a Google Cloud '
                    'project → <a href="https://console.cloud.google.com/apis/credentials" '
                    'target="_blank">Credentials</a> → Create credentials → OAuth client ID '
                    '→ Desktop app → download the JSON → save it as '
                    f'<code>{_h.escape(followup.CLIENT_FILE.name)}</code> in the agent '
                    'folder → reload this page and click Connect. Follow-ups work without '
                    'it; events are added retroactively once connected.</p>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setup — Job Leads Console</title><style>
:root {{ color-scheme: light dark; }}
body {{ margin:0; padding:24px; background:#f9f9f7; color:#0b0b0b;
  font:14px/1.5 system-ui,sans-serif; }}
@media (prefers-color-scheme: dark) {{ body {{ background:#0d0d0d; color:#fff; }}
  .card {{ background:#1a1a19 !important; border-color:rgba(255,255,255,.1) !important; }}
  input,select {{ background:#0d0d0d !important; color:#fff !important;
    border-color:rgba(255,255,255,.15) !important; }} }}
.wrap {{ max-width:640px; margin:0 auto; }}
h1 {{ font-size:19px; }} h2 {{ font-size:14px; margin:22px 0 4px; }}
.card {{ background:#fcfcfb; border:1px solid rgba(11,11,11,.1); border-radius:10px;
  padding:16px 18px; margin-bottom:14px; }}
label {{ display:block; margin:10px 0 0; font-weight:600; font-size:13px; }}
input,select {{ display:block; width:100%; margin-top:4px; padding:8px 10px;
  font:13.5px system-ui; border:1px solid rgba(11,11,11,.15); border-radius:8px;
  box-sizing:border-box; }}
.mut {{ color:#898781; font-weight:400; font-size:12px; }}
.ok {{ background:#0ca30c22; border:1px solid #0ca30c; border-radius:8px;
  padding:9px 12px; margin-bottom:12px; font-weight:600; }}
button {{ margin-top:16px; font:600 14px system-ui; color:#fff; background:#2a78d6;
  border:none; border-radius:8px; padding:10px 22px; cursor:pointer; }}
a {{ color:#2a78d6; }}
</style></head><body><div class="wrap">
<h1>⚙ Setup</h1>
<p class="mut"><a href="/">← back to console</a> · everything saves to your local
 files, nothing leaves this machine</p>
{banner}
<form method="POST" action="/setup">
<div class="card"><h2>Your answers (typed onto real applications — make them true)</h2>
{yn("eligible", "Legally eligible to work in Canada?", _rule_answer(ans, "eligible"))}
{yn("sponsor", "Will you require visa sponsorship?", _rule_answer(ans, "sponsor"))}
<label>Pronouns <span class="mut">blank = forms leave it for you to pick</span>
<select name="pronouns">{pron_opts}</select></label>
{txt("phone", "Phone", ans.get("phone", ""))}
{txt("email", "Email", ans.get("email", ""))}
{txt("location", "Location", ans.get("location", ""))}
{txt("linkedin", "LinkedIn URL", ans.get("linkedin", ""))}
</div>
<div class="card"><h2>More job sources (free keys, ~5 min each)</h2>
{secret("serpapi_key", "SerpAPI key", bool(cfg.get("serpapi_key")),
        '— Google Jobs incl. LinkedIn postings · <a href="https://serpapi.com" target="_blank">get free key</a>')}
{secret("adzuna_app_id", "Adzuna App ID", bool(cfg.get("adzuna_app_id")),
        '— broad coverage incl. much of Indeed · <a href="https://developer.adzuna.com" target="_blank">get free key</a>')}
{secret("adzuna_app_key", "Adzuna App Key", bool(cfg.get("adzuna_app_key")))}
{txt("linkedin_imap_user", "Gmail address (for LinkedIn alert emails)",
     cfg.get("linkedin_imap_user", ""),
     "", '— set LinkedIn job alerts to email you, agent reads them')}
{secret("linkedin_imap_app_password", "Gmail app password",
        bool(cfg.get("linkedin_imap_app_password")),
        '— <a href="https://myaccount.google.com/apppasswords" target="_blank">create one</a>')}
</div>
<div class="card"><h2>Follow-ups (one-click send after you apply)</h2>
{txt("gmail_user", "Gmail address that sends your follow-ups", gmail_user,
     "you@gmail.com", "— replies land in this inbox; shares the LinkedIn-alerts login if left blank")}
{secret("gmail_app_password", "Gmail app password", bool(gmail_pw),
        '— <a href="https://myaccount.google.com/apppasswords" target="_blank">create one</a> (2-step verification must be on)')}
{txt("followup_days", "Days after applying to follow up", str(cfg.get("followup_days", 7)), "7")}
<label>Google Calendar</label>{cal_html}
</div>
<div class="card"><h2>Phone line (text a link, get texted the follow-up)</h2>
<p class="mut">Paste your Twilio keys, save, then run <code>py -3 sms.py buy-number</code>
 once to get a Canadian number. The JobLeadsSMS task answers texts at every login.</p>
{txt("my_phone", "Your mobile number", cfg.get("my_phone", ""), "+14165551234",
     "— the only number the line listens to")}
{secret("twilio_account_sid", "Twilio Account SID", bool(cfg.get("twilio_account_sid")),
        '— <a href="https://console.twilio.com" target="_blank">console.twilio.com</a>')}
{secret("twilio_auth_token", "Twilio Auth Token", bool(cfg.get("twilio_auth_token")))}
{txt("twilio_from", "Twilio number (filled by buy-number)", cfg.get("twilio_from", ""), "+1...")}
</div>
<button type="submit">Save</button>
</form></div></body></html>"""


def config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def spawn(script_args: list[str]) -> None:
    """Launch a helper fully detached, so restarting/stopping the console server
    can never kill an application the user is in the middle of reviewing."""
    logf = open(ROOT / "logs" / "console-actions.log", "a")
    base = NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            subprocess.Popen([sys.executable] + script_args, cwd=ROOT,
                             creationflags=flags, stdout=logf,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            break
        except OSError:
            continue
    logf.close()


def answer_question(question: str) -> str:
    con = sqlite3.connect(ROOT / "leads.db", timeout=15)
    cfg = config()
    leads = con.execute(
        """SELECT id, title, company, score, salary, posted_at, drafted, status
           FROM jobs WHERE status='new' AND passed_filter=1 AND score>=?
           ORDER BY score DESC LIMIT 30""",
        (cfg.get("worth_a_look_threshold", 60),)).fetchall()
    pipe = con.execute(
        """SELECT title, company, status, status_date FROM jobs
           WHERE status NOT IN ('new','skipped','closed','rejected','gone')
           ORDER BY status_date DESC LIMIT 25""").fetchall()
    coach = ""
    if (ROOT / "coach.md").exists():
        coach = (ROOT / "coach.md").read_text(encoding="utf-8")[:1500]
    context = json.dumps({
        "funnel": funnel_stats(con),
        "open_leads": [dict(id=i, title=t, company=c, score=s, salary=sal or None,
                            posted=p, package_ready=bool(d)) for
                       i, t, c, s, sal, p, d, _ in leads],
        "in_flight": [dict(title=t, company=c, status=st, since=sd)
                      for t, c, st, sd in pipe],
        "todays_coach_briefing": coach,
    }, ensure_ascii=False)
    profile = (ROOT / "profile.md").read_text(encoding="utf-8")[:2500]
    instruction = (
        "You are the copilot embedded in Matthew's job-hunt console. The JSON on "
        "stdin is his live pipeline; his profile follows it. Answer his question "
        "practically and concretely in plain text (no markdown), max 180 words. "
        "Refer to leads by title and company. If the honest answer is 'apply to "
        "the ones already prepared', say so.\n\nQUESTION: " + question[:500]
    )
    proc = subprocess.run(
        claude_command() + ["-p", instruction,
                            "--model", config().get("console_ask_model", "sonnet")],
        input="PIPELINE:\n" + context + "\n\nPROFILE:\n" + profile,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, creationflags=NO_WINDOW)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError((proc.stderr or "no answer")[:300])
    return proc.stdout.strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console log quiet
        pass

    def _respond(self, code: int, body: str, ctype: str = "text/plain") -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                self._respond(200, dashboard.render(config(), interactive=True),
                              "text/html")
            except Exception as e:
                self._respond(500, f"dashboard error: {e}")
        elif path == "/setup":
            try:
                saved = "saved=1" in (urlparse(self.path).query or "")
                self._respond(200, build_setup_page(saved), "text/html")
            except Exception as e:
                self._respond(500, f"setup error: {e}")
        else:
            self._respond(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        route = parsed.path
        try:
            if route == "/mark":
                job_id, status = q.get("id", ""), q.get("status", "")
                reason = q.get("reason", "")[:200]
                if status not in VALID_STATUS:
                    return self._respond(400, f"bad status: {status}")
                con = sqlite3.connect(ROOT / "leads.db", timeout=15)
                if status == "skipped" and reason:
                    n = con.execute(
                        "UPDATE jobs SET status=?, status_date=?, skip_reason=? WHERE id=?",
                        (status, datetime.now().strftime("%Y-%m-%d"), reason,
                         job_id)).rowcount
                else:
                    n = con.execute(
                        "UPDATE jobs SET status=?, status_date=? WHERE id=?",
                        (status, datetime.now().strftime("%Y-%m-%d"), job_id)).rowcount
                con.commit()
                if not n:
                    return self._respond(404, f"no job {job_id}")
                if status == "interview":
                    spawn([str(ROOT / "agent.py"), "prep", job_id])
                if status == "applied":
                    # follow-up date + calendar events, right away
                    spawn([str(ROOT / "followup.py"), "schedule", job_id])
                if status in ("applied", "interview"):
                    # the funnel just changed meaningfully — refresh the coach briefing
                    spawn([str(ROOT / "agent.py"), "coach"])
                return self._respond(200, "ok")
            if route == "/apply":
                job_id, mode = q.get("id", ""), q.get("mode", "assisted")
                args = [str(ROOT / "apply.py"), job_id]
                args += ["--auto"] if mode == "auto" else ["--console"]
                spawn(args)
                return self._respond(200, "started")
            if route == "/apply-all":
                spawn([str(ROOT / "apply.py"), "--auto-all"])
                return self._respond(200, "started")
            if route == "/draft":
                job_id = q.get("id", "")
                if not job_id:
                    return self._respond(400, "missing id")
                spawn([str(ROOT / "agent.py"), "draft", job_id])
                return self._respond(200, "building")
            if route == "/run":
                spawn([str(ROOT / "agent.py"), "run"])
                return self._respond(200, "started")
            if route == "/setup":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(min(length, 20000)).decode("utf-8", "replace")
                data = {k: v[0].strip() for k, v in parse_qs(body).items()}
                ans_path = ROOT / "answers.json"
                ans = _answers()
                for k in SETUP_ANSWER_KEYS:
                    if k in data:
                        ans[k] = data[k][:200]
                if ans.get("first_name") and ans.get("last_name"):
                    ans["full_name"] = f"{ans['first_name']} {ans['last_name']}"
                for form_key, pat in (("eligible", "eligible"), ("sponsor", "sponsor")):
                    if data.get(form_key) in ("Yes", "No"):
                        for r in ans.get("yes_no_rules", []):
                            if pat in r["pattern"]:
                                r["answer"] = data[form_key]
                ans_path.write_text(json.dumps(ans, indent=2) + "\n", encoding="utf-8")
                cfg_path = ROOT / "config.json"
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                for k in SETUP_CONFIG_KEYS:
                    if data.get(k):  # blank keeps the existing value
                        cfg[k] = data[k][:300]
                if str(cfg.get("followup_days", "")).strip().isdigit():
                    cfg["followup_days"] = int(cfg["followup_days"])
                else:
                    cfg["followup_days"] = 7
                cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                self.send_response(303)
                self.send_header("Location", "/setup?saved=1")
                self.end_headers()
                return None
            if route == "/ask":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(min(length, 10000)).decode("utf-8", "replace")
                question = json.loads(body or "{}").get("q", "").strip()
                if not question:
                    return self._respond(400, "empty question")
                return self._respond(200, answer_question(question))
            if route == "/applied":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(min(length, 20000)).decode("utf-8", "replace")
                text = json.loads(body or "{}").get("text", "").strip()
                try:
                    ids = followup.cmd_applied(text)
                except ValueError as e:
                    return self._respond(400, str(e))
                spawn([str(ROOT / "agent.py"), "coach"])
                return self._respond(200, json.dumps({"ids": ids}), "application/json")
            if route == "/followup/draft":
                spawn([str(ROOT / "followup.py"), "draft", q.get("id", "")])
                return self._respond(200, "drafting")
            if route == "/followup/send":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(min(length, 40000)).decode("utf-8", "replace")
                d = json.loads(body or "{}")
                try:
                    res = followup.cmd_send(d.get("id", ""), d.get("to"), d.get("subject"),
                                            d.get("body"))
                except Exception as e:
                    return self._respond(400, str(e))
                return self._respond(200, f"Sent to {res['to']}")
            if route == "/followup/sent":
                followup.cmd_mark_sent_manually(q.get("id", ""), q.get("how", "by hand"))
                return self._respond(200, "ok")
            if route == "/followup/snooze":
                days = max(1, min(30, int(q.get("days", "3") or 3)))
                con = followup.fdb()
                con.execute(
                    "UPDATE jobs SET followup_due=?, followup_state='scheduled', "
                    "followup_notified=NULL WHERE id=?",
                    ((datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"),
                     q.get("id", "")))
                con.commit()
                return self._respond(200, "ok")
            if route == "/followup/skip":
                con = followup.fdb()
                con.execute("UPDATE jobs SET followup_state='skipped' WHERE id=?",
                            (q.get("id", ""),))
                con.commit()
                return self._respond(200, "ok")
            if route == "/followup/contact":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(min(length, 10000)).decode("utf-8", "replace")
                d = json.loads(body or "{}")
                con = followup.fdb()
                con.execute(
                    "UPDATE jobs SET contact_name=?, contact_email=?, contact_linkedin=? "
                    "WHERE id=?",
                    (d.get("name", "")[:120], d.get("email", "")[:200],
                     d.get("linkedin", "")[:300], d.get("id", "")))
                con.commit()
                if d.get("redraft"):
                    spawn([str(ROOT / "followup.py"), "draft", d.get("id", "")])
                return self._respond(200, "ok")
            if route == "/followup/edit":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(min(length, 10000)).decode("utf-8", "replace")
                d = json.loads(body or "{}")
                if not d.get("id") or not d.get("title"):
                    return self._respond(400, "need a title")
                con = followup.fdb()
                con.execute(
                    "UPDATE jobs SET title=?, company=COALESCE(NULLIF(?,''),company), "
                    "snippet=COALESCE(NULLIF(?,''),snippet) WHERE id=?",
                    (d["title"][:160], d.get("company", "")[:120],
                     d.get("notes", "")[:2000], d["id"]))
                con.commit()
                return self._respond(200, "ok")
            if route == "/calendar/connect":
                spawn([str(ROOT / "followup.py"), "calendar-connect"])
                return self._respond(200, "started")
            if route == "/open":
                target = Path(q.get("path", "")).resolve()
                if not str(target).lower().startswith(str(ROOT).lower()):
                    return self._respond(403, "path outside agent folder")
                if not target.exists():
                    return self._respond(404, "path not found")
                os.startfile(str(target))  # noqa: S606 — local console action
                return self._respond(200, "opened")
            return self._respond(404, "not found")
        except Exception as e:
            return self._respond(500, str(e))


def main() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"port {PORT} already in use — console probably already running")
        return
    print(f"Job Leads Console: http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
