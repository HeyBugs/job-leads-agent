"""The phone line: WhatsApp or SMS on a Twilio number. The whole product.

  you  -> https://jobs.acme.com/123
  line -> #4 Reading the posting and writing your resume + cover letter. About
          5 minutes. I'll message you when they're in your email.
  line -> #4 Resume + cover letter for Customer Success Manager at Acme are in
          your email. Apply when you're ready, then reply APPLIED #4.
  you  -> APPLIED #4  Jane Doe jane@acme.com
  line -> #4 Filed: Customer Success Manager at Acme. I'll text you the
          follow-up on Sep 29. To: Jane Doe, jane@acme.com.
  ... a week later, the morning run ...
  line -> #4 Follow up: Customer Success Manager at Acme (applied Sep 22)
          TO: Jane Doe  jane@acme.com  SUBJECT: ...  <the drafted email>
          Reply SEND to email it from your Gmail, SNOOZE, SKIP, or SEND <email>.
  you  -> SEND
  line -> Sent to jane@acme.com. #4 marked followed up.

Things you can message (case doesn't matter):
  <job link>                    build resume + cover letter, email them to you
  <job link> applied [name/email/profile]   you already applied: schedule the follow-up
  APPLIED [#n|company] [name/email/profile] you applied to a prepped one
  SEND [#n|company] [email]     send the drafted follow-up (optionally to that address)
  SENT [#n|company]             you sent it yourself (LinkedIn etc.)
  SNOOZE [#n|company] [days]    push the follow-up out (default 3 days)
  SKIP [#n|company]             no follow-up (or drop a prepped lead)
  DRAFT [#n|company]            write the follow-up now, before the date
  NAME <role> at <company>      fix a posting the agent could not read
  <email / LinkedIn profile>    attach a contact to the pending / latest application
  LIST                          what is in flight and when each follow-up is due
  anything else                 a question for the agent about your pipeline

Runs as a poller (no public URL, no tunnel): `py -3 sms.py serve` checks the
Twilio number for new inbound messages every sms_poll_seconds (30) and answers.
The JobLeadsSMS scheduled task starts it at every login.

Setup (config.json): twilio_account_sid, twilio_auth_token, my_phone (+1...),
twilio_from. For WhatsApp set twilio_from to "whatsapp:+14155238886" (Twilio's
sandbox number: send its "join <word>" from your phone first, and again if it
lapses) or your approved WhatsApp sender. For SMS: `py -3 sms.py buy-number`.
Optional twilio_sms_from: an SMS number used as a fallback when a WhatsApp
message cannot be delivered (WhatsApp blocks business-initiated messages more
than 24 h after your last message, which is exactly what a week-later
follow-up is). Gmail (gmail_user + gmail_app_password) is what SEND and the
document emails use.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import followup as fu  # noqa: E402
from agent import CONFIG, claude_command, funnel_stats, log  # noqa: E402

STATE_FILE = ROOT / "sms_state.json"
API = "https://api.twilio.com/2010-04-01/Accounts/{sid}"
MAX_SMS = 1500  # long codes take up to 1600 chars; keep a margin
NO_WINDOW = 0x08000000


# ------------------------------------------------------------------ config/state
def creds() -> tuple[str, str]:
    return (CONFIG.get("twilio_account_sid", "").strip(),
            CONFIG.get("twilio_auth_token", "").strip())


def configured() -> bool:
    sid, tok = creds()
    return bool(sid and tok and CONFIG.get("twilio_from") and CONFIG.get("my_phone"))


def is_whatsapp() -> bool:
    return str(CONFIG.get("twilio_from", "")).lower().startswith("whatsapp:")


def my_address() -> str:
    """my_phone with the channel prefix the line uses."""
    me = str(CONFIG.get("my_phone", "")).strip()
    me = re.sub(r"^whatsapp:", "", me, flags=re.I)
    return f"whatsapp:{me}" if is_whatsapp() else me


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": [], "handles": {}, "watermark": ""}


def _save_state(s: dict) -> None:
    s["seen"] = s.get("seen", [])[-200:]
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def handle_for(job_id: str, state: dict | None = None) -> str:
    """Short tag like #4 that stays stable for a job, so SEND #4 is unambiguous."""
    s = state if state is not None else _state()
    h = s.setdefault("handles", {})
    if job_id not in h:
        h[job_id] = max([0, *h.values()]) + 1
        if state is None:
            _save_state(s)
    return f"#{h[job_id]}"


# ------------------------------------------------------------------ twilio
def _twilio(method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
    sid, tok = creds()
    if not sid or not tok:
        raise RuntimeError("Twilio is not set up (twilio_account_sid / twilio_auth_token)")
    url = API.format(sid=sid) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{sid}:{tok}".encode()).decode())
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twilio {e.code}: {e.read().decode()[:300]}") from None


def _chunks(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= MAX_SMS:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > MAX_SMS - 8 and cur:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n" + para) if cur else para
    if cur:
        parts.append(cur)
    n = len(parts)
    return [f"({i + 1}/{n}) {p}" for i, p in enumerate(parts)]


def send_sms(text: str, to: str | None = None) -> list[str]:
    """Send on the line (WhatsApp or SMS). If WhatsApp refuses and an SMS
    fallback number is configured, send the same text as SMS."""
    to = to or my_address()
    sids = []
    try:
        for part in _chunks(text):
            r = _twilio("POST", "/Messages.json",
                        {"From": CONFIG["twilio_from"], "To": to, "Body": part})
            sids.append(r.get("sid", ""))
    except RuntimeError as e:
        fallback = CONFIG.get("twilio_sms_from", "")
        if is_whatsapp() and fallback and to == my_address():
            log(f"sms: whatsapp send failed ({str(e)[:120]}), falling back to SMS")
            plain = re.sub(r"^whatsapp:", "", to, flags=re.I)
            for part in _chunks(text):
                r = _twilio("POST", "/Messages.json",
                            {"From": fallback, "To": plain, "Body": part})
                sids.append(r.get("sid", ""))
        else:
            raise
    log(f"sms: -> {to} ({len(sids)} part{'s' if len(sids) != 1 else ''})")
    return sids


def fetch_inbound(state: dict) -> list[dict]:
    r = _twilio("GET", "/Messages.json", params={"To": CONFIG["twilio_from"], "PageSize": 30})
    seen = set(state.get("seen", []))
    wm = state.get("watermark") or ""
    out = []
    for m in r.get("messages", []):
        if m.get("direction") != "inbound" or m["sid"] in seen:
            continue
        try:
            created = datetime.strptime(m["date_created"], "%a, %d %b %Y %H:%M:%S %z")
            if wm and created.isoformat() < wm:
                continue
        except (ValueError, KeyError):
            pass
        out.append({"sid": m["sid"], "from": m.get("from", ""), "body": m.get("body", "")})
    return list(reversed(out))  # oldest first


# ------------------------------------------------------------------ formatting
def _row(con, job_id: str):
    return con.execute(
        """SELECT id, company, title, applied_at, status_date, followup_due, followup_state,
                  contact_name, contact_email, contact_linkedin, followup_target,
                  followup_draft, url, status, draft_path FROM jobs WHERE id=?""",
        (job_id,)).fetchone()


def _nice_date(d: str | None) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%b %d").replace(" 0", " ")
    except (ValueError, TypeError):
        return d or "?"


def followup_text(con, job_id: str) -> str:
    r = _row(con, job_id)
    (_id, company, title, applied_at, sdate, due, state, c_name, c_email, c_li,
     tgt_s, draft_s, url, *_x) = r
    tgt = json.loads(tgt_s or "{}")
    draft = json.loads(draft_s or "{}")
    h = handle_for(job_id)
    who = " ".join(x for x in (tgt.get("name") or c_name or "", tgt.get("role") or "") if x)
    email = tgt.get("email") or c_email or ""
    li = tgt.get("linkedin_url") or c_li or tgt.get("search_url") or ""
    lines = [f"{h} Follow up: {title} at {company} (applied {_nice_date(applied_at or sdate)})",
             f"TO: {who or 'no named contact'}" + (f"  {email}" if email else "")]
    if not email:
        lines.append("No email known. " + (f"LinkedIn: {li}" if li else "") +
                     " Reply SEND <email> if you have one, or SENT after messaging them.")
    if tgt.get("why"):
        lines.append(f"Why: {tgt['why']}")
    lines += ["", f"SUBJECT: {draft.get('subject', '')}", "", draft.get("body", ""), "",
              ("Reply SEND to email this from your Gmail" if email else
               "Reply SEND <email> to email this from your Gmail")
              + f", SNOOZE {h} for 3 more days, or SKIP {h}."]
    return "\n".join(lines)


def filed_text(con, job_id: str) -> str:
    r = _row(con, job_id)
    (_id, company, title, applied_at, sdate, due, state, c_name, c_email, c_li, *_r) = r
    h = handle_for(job_id)
    if title.startswith("(couldn't read"):
        return (f"{h} Filed the link, but the posting would not load for me. Reply "
                f"NAME <job title> at <company> and I'll use that. Follow-up on {_nice_date(due)}.")
    phone = con.execute("SELECT contact_phone FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
    contact = ", ".join(x for x in (c_name, c_email, c_li, phone) if x)
    return (f"{h} Filed: {title} at {company}. I'll message you the follow-up on "
            f"{_nice_date(due)}. " +
            (f"To: {contact}." if contact else
             "No contact yet; send me a name, email or LinkedIn profile anytime."))


def list_text(con) -> str:
    rows = con.execute(
        """SELECT id, company, title, followup_due, followup_state, status, drafted FROM jobs
           WHERE status IN ('applied','followed-up','interview')
              OR (status='new' AND source='manual')
           ORDER BY status_date DESC LIMIT 15""").fetchall()
    if not rows:
        return "Nothing in flight. Send me a job link to get started."
    out = []
    for jid, company, title, due, st, status, drafted in rows:
        h = handle_for(jid)
        if status == "new":
            what = ("docs ready, reply APPLIED " + h) if drafted else "writing your docs"
        elif status == "interview":
            what = "interview"
        elif status == "followed-up" or st == "sent":
            what = "followed up"
        elif st == "ready":
            what = "draft ready, reply SEND " + h
        elif st == "skipped":
            what = "no follow-up"
        else:
            what = f"follow-up {_nice_date(due)}"
        out.append(f"{h} {title} at {company}: {what}")
    return "\n".join(out)


HELP = ("Send me a job link and I'll write your resume + cover letter and email them. "
        "Add the word 'applied' (plus a name/email if you have one) when you've already "
        "applied. Then: APPLIED, SEND, SEND <email>, SENT, SNOOZE [days], SKIP, DRAFT, "
        "LIST, NAME <role> at <company>. Add #n when more than one is pending. "
        "Anything else, just ask.")


# ------------------------------------------------------------------ commands
def _resolve(con, arg: str, states: tuple[str, ...],
             statuses: tuple[str, ...] = ("applied",)) -> tuple[str | None, str]:
    """Pick the job a command refers to: '#n', a company word, or the only
    candidate. Returns (job_id, error_text)."""
    marks = ",".join("?" * len(statuses))
    # 'new' means a lead YOU sent (prepped), never the scout's thousands.
    rows = con.execute(
        f"SELECT id, company, title, followup_state FROM jobs WHERE status IN ({marks}) "
        f"AND (status<>'new' OR source='manual') ORDER BY status_date DESC, rowid DESC",
        statuses).fetchall()
    cands = [r for r in rows if (r[3] or "scheduled") in states]
    arg = (arg or "").strip().lstrip("#").lower()
    if arg.isdigit():
        st = _state()
        for r in rows:
            if st.get("handles", {}).get(r[0]) == int(arg):
                return r[0], ""
        return None, f"I don't have a #{arg} in that state. Send LIST to see the numbers."
    if arg:
        hits = [r for r in rows if arg in (r[1] or "").lower() or arg in (r[2] or "").lower()]
        if len(hits) == 1:
            return hits[0][0], ""
        if len(hits) > 1:
            return None, ("Which one? " + ", ".join(f"{handle_for(r[0])} {r[1]}" for r in hits))
        return None, f"Nothing matching '{arg}'. Send LIST."
    if len(cands) == 1:
        return cands[0][0], ""
    if not cands:
        return None, "Nothing is waiting right now. Send LIST to see what's in flight."
    return None, ("Which one? " + ", ".join(f"{handle_for(r[0])} {r[1]}" for r in cands))


def _attach_contact(con, jid: str, parsed: dict) -> None:
    con.execute(
        "UPDATE jobs SET contact_email=COALESCE(NULLIF(?,''),contact_email), "
        "contact_linkedin=COALESCE(NULLIF(?,''),contact_linkedin), "
        "contact_phone=COALESCE(NULLIF(?,''),contact_phone), "
        "contact_name=COALESCE(NULLIF(?,''),contact_name) WHERE id=?",
        (parsed["contact_email"], parsed["contact_linkedin"], parsed.get("contact_phone", ""),
         parsed["contact_name"], jid))
    con.commit()


def _company_in_text(con, text: str, statuses: tuple[str, ...]) -> str:
    """'#n' if present, else the first company name mentioned in the text among
    the rows in those statuses, else ''. Lets 'I applied to the Exhibition
    Place one' work without a handle."""
    tag = re.search(r"#(\d+)", text)
    if tag:
        return tag.group(0)
    marks = ",".join("?" * len(statuses))
    low = text.lower()
    for (company,) in con.execute(
            f"SELECT company FROM jobs WHERE status IN ({marks}) "
            f"AND (status<>'new' OR source='manual') ORDER BY status_date DESC",
            statuses).fetchall():
        first = (company or "").split()[0].lower() if company else ""
        if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", low):
            return first
    return ""


def ask_agent(question: str) -> str:
    """Anything that is not a command: answer from the live pipeline."""
    con = fu.fdb()
    pipe = con.execute(
        """SELECT title, company, status, status_date, followup_due, followup_state FROM jobs
           WHERE status NOT IN ('new','skipped','closed','rejected','gone')
              OR (status='new' AND source='manual')
           ORDER BY status_date DESC LIMIT 25""").fetchall()
    context = json.dumps({
        "funnel": funnel_stats(con),
        "in_flight": [dict(title=t, company=c, status=s, since=d, followup_due=fd,
                           followup_state=fs) for t, c, s, d, fd, fs in pipe],
    }, ensure_ascii=False)
    profile = (ROOT / "profile.md").read_text(encoding="utf-8")[:2500]
    instruction = (
        "You are Matthew's job-hunt agent, answering over text message. The JSON on stdin "
        "is his live pipeline; his profile follows. Answer his message practically in plain "
        "text, no markdown, max 90 words. Only state facts that are in the JSON; never claim "
        "what is or is not on file beyond it. If he seems to be telling you he applied, or "
        "giving you a contact, and it did not parse, reply exactly: \"I didn't catch which job "
        "that was. Reply APPLIED #n (and the contact) and I'll file it.\" If he is asking for "
        "something you do (build docs for a link, schedule or send a follow-up), tell him the "
        "exact thing to send: a job link, 'APPLIED #n', 'SEND', 'LIST'.\n\nMESSAGE: "
        + question[:500])
    try:
        proc = subprocess.run(
            claude_command() + ["-p", instruction, "--model", CONFIG.get("console_ask_model", "sonnet")],
            input="PIPELINE:\n" + context + "\n\nPROFILE:\n" + profile,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180, creationflags=NO_WINDOW)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception as e:
        log(f"ask failed: {e}")
    return HELP


APPLIED_RE = re.compile(r"\bap+l(?:ied|eid|yed|id|ie)\b", re.I)  # applied, appleid, aplied…


def handle_message(text: str) -> str:
    """Turn one inbound message into one reply."""
    reply = _handle(text)
    st = _state()
    if reply.startswith("Which one?"):
        st["pending"] = (text or "").strip()  # remember the question we just asked
    else:
        st.pop("pending", None)
    _save_state(st)
    return reply


def _handle(text: str) -> str:
    con = fu.fdb()
    raw = (text or "").strip()
    low = raw.lower()
    parsed = fu.parse_intake(raw)

    # An answer to our own "Which one?": a handle or a company word completes
    # the message we could not place.
    pending = _state().get("pending")
    if pending and not parsed["job_urls"] and len(raw) <= 40:
        pick = _company_in_text(con, raw, ("new", "applied"))
        if pick:
            st = _state(); st.pop("pending", None); _save_state(st)
            return _handle(f"{pending} {pick}")

    if parsed["job_urls"]:
        if APPLIED_RE.search(low):
            ids = fu.cmd_applied(raw, sync=True)
            return "\n".join(filed_text(con, j) for j in ids)
        ids = fu.cmd_file(raw, applied=False, sync=False)
        hs = ", ".join(handle_for(j) for j in ids)
        return (f"{hs} Reading the posting and writing your resume + cover letter. About 5 "
                f"minutes. I'll message you when they're in your email. Already applied? "
                f"Reply APPLIED {hs.split(',')[0]}.")

    m = re.match(r"(applied|send|sent|snooze|skip|draft|docs|resend|list|status|help|name|undo)\b(.*)",
                 low, re.S)
    cmd, rest = (m.group(1), m.group(2).strip()) if m else ("", "")
    if not cmd and APPLIED_RE.search(low):
        # "I just applied to the job, here are some contact details ..."
        cmd, rest = "applied", raw

    if cmd == "undo":
        st = _state()
        last = st.pop("last_applied", None)
        if not last:
            return "Nothing to undo."
        con.execute("UPDATE jobs SET status='new', status_date=?, applied_at=NULL, followup_due=NULL, "
                    "followup_state=NULL, contact_name=NULL, contact_email=NULL, contact_phone=NULL, "
                    "contact_linkedin=NULL WHERE id=? AND source='manual'", (fu.today(), last))
        con.commit()
        _save_state(st)
        r = _row(con, last)
        return f"Undone: {handle_for(last)} ({r[1]}) is back to 'documents ready, not applied'."
    if cmd in ("list", "status"):
        return list_text(con)
    if cmd == "help" or not raw:
        return HELP

    if cmd in ("docs", "resend"):
        jid, err = _resolve(con, rest, ("scheduled", "ready", "sent", "skipped"),
                            statuses=("new", "applied"))
        if not jid:
            return err
        r = _row(con, jid)
        if not r[14] or not Path(r[14]).exists():
            fu._spawn(["prep", jid])
            return f"{handle_for(jid)} has no documents yet; building them now, about 5 minutes."
        fu.cmd_prep(jid)  # already drafted: just re-sends the files
        return ""  # cmd_prep messaged the files itself

    if cmd == "applied":
        arg = _company_in_text(con, rest, ("new", "applied"))
        jid, err = _resolve(con, arg, ("scheduled", "ready", "sent", "skipped"),
                            statuses=("new",))
        assumed = False
        if not jid and not arg:
            # Several prepped leads and no name given: he is replying to the
            # documents he was sent most recently. Say so, and offer UNDO.
            last = con.execute(
                "SELECT id FROM jobs WHERE status='new' AND source='manual' AND drafted=1 "
                "ORDER BY rowid DESC LIMIT 1").fetchone()
            if last:
                jid, assumed = last[0], True
            else:  # nothing prepped: the newest thing in flight
                jid, err = _resolve(con, "", ("scheduled", "ready", "sent", "skipped"),
                                    statuses=("new", "applied"))
        if not jid:
            return err
        if arg and not arg.startswith("#"):  # the company word is not part of the name
            parsed["contact_name"] = re.sub(re.escape(arg), "", parsed["contact_name"],
                                            flags=re.I).strip()
        _attach_contact(con, jid, parsed)
        fu.mark_applied(jid)
        st = _state(); st["last_applied"] = jid; _save_state(st)
        out = filed_text(con, jid)
        if assumed:
            out += " (I took that to mean the latest one. Not right? Reply UNDO, then APPLIED #n.)"
        return out

    if cmd == "name":
        mm = re.match(r"(?:#\d+\s*)?(.+?)\s+at\s+(.+)", raw[4:].strip(), re.I | re.S)
        if not mm:
            return "Say it like: NAME Field Service Technician at BFG Enterprise Services"
        tag = re.search(r"#(\d+)", raw)
        if tag:
            jid, err = _resolve(con, tag.group(1), ("scheduled", "ready", "sent", "skipped"),
                                statuses=("new", "applied"))
            if not jid:
                return err
        else:
            last = con.execute(
                "SELECT id FROM jobs WHERE source='manual' AND status IN ('new','applied') "
                "ORDER BY (title LIKE '(couldn''t read%') DESC, status_date DESC, rowid DESC LIMIT 1"
            ).fetchone()
            jid = last[0] if last else None
        if not jid:
            return "No hand-filed application to rename."
        title, company = mm.group(1).strip()[:160], mm.group(2).strip().rstrip(".")[:120]
        con.execute("UPDATE jobs SET title=?, company=? WHERE id=?", (title, company, jid))
        con.commit()
        r = _row(con, jid)
        if r[13] == "new" and not r[14]:
            fu._spawn(["prep", jid])
            return (f"{handle_for(jid)} is now {title} at {company}. Building the resume and "
                    f"cover letter, about 5 minutes. (Paste the job description text if you "
                    f"have it and I'll use it.)")
        return f"{handle_for(jid)} is now {title} at {company}."

    if cmd == "send":
        email = parsed["contact_email"]
        arg = re.sub(fu.EMAIL_RE, "", rest).strip()
        jid, err = _resolve(con, arg, ("ready",))
        if not jid:
            return err
        r = _row(con, jid)
        known = email or json.loads(r[10] or "{}").get("email") or r[8] or ""
        if not known:
            return (f"No email address for {handle_for(jid)} ({r[1]}). Reply SEND <email>, "
                    f"or SENT once you've messaged them on LinkedIn.")
        try:
            res = fu.cmd_send(jid, to_addr=known)
        except Exception as e:
            return f"Couldn't send: {str(e)[:200]}"
        return f"Sent to {res['to']}. {handle_for(jid)} marked followed up."

    if cmd == "sent":
        jid, err = _resolve(con, rest, ("ready", "scheduled"))
        if not jid:
            return err
        fu.cmd_mark_sent_manually(jid, "by text")
        return f"{handle_for(jid)} marked followed up. Nice."

    if cmd == "snooze":
        mm = re.match(r"(.*?)\s*(\d+)?\s*(?:days?)?$", rest)
        arg, days = (mm.group(1), int(mm.group(2) or 3)) if mm else (rest, 3)
        jid, err = _resolve(con, arg, ("ready", "scheduled"))
        if not jid:
            return err
        due = (datetime.now() + timedelta(days=max(1, min(30, days)))).strftime("%Y-%m-%d")
        con.execute("UPDATE jobs SET followup_due=?, followup_state='scheduled', "
                    "followup_notified=NULL WHERE id=?", (due, jid))
        con.commit()
        return f"{handle_for(jid)} snoozed. I'll message you again on {_nice_date(due)}."

    if cmd == "skip":
        jid, err = _resolve(con, rest, ("ready", "scheduled"), statuses=("applied", "new"))
        if not jid:
            return err
        r = _row(con, jid)
        if r[13] == "new":
            con.execute("UPDATE jobs SET status='skipped', status_date=? WHERE id=?",
                        (fu.today(), jid))
            con.commit()
            return f"{handle_for(jid)} dropped."
        con.execute("UPDATE jobs SET followup_state='skipped' WHERE id=?", (jid,))
        con.commit()
        return f"{handle_for(jid)}: no follow-up. Still tracked as applied."

    if cmd == "draft":
        jid, err = _resolve(con, rest, ("scheduled", "ready"))
        if not jid:
            return err
        if not fu.cmd_draft(jid, quiet=True):
            return "Drafting failed; I'll try again on the scheduled morning."
        con.execute("UPDATE jobs SET followup_notified=? WHERE id=?", (fu.today(), jid))
        con.commit()
        return followup_text(con, jid)

    # A bare contact (email / LinkedIn profile, optionally "#n") attaches to the
    # pending draft if there is exactly one, else to the latest application.
    if parsed["contact_email"] or parsed["contact_linkedin"] or parsed.get("contact_phone"):
        arg = _company_in_text(con, raw, ("applied", "new"))
        jid = None
        if arg:
            jid, err = _resolve(con, arg, ("ready", "scheduled", "sent", "skipped"),
                                statuses=("applied", "new"))
            if not jid:
                return err
        if not jid:
            # A follow-up that arrived today is what a bare contact answers;
            # otherwise the newest thing in flight.
            pend = con.execute(
                "SELECT id FROM jobs WHERE status='applied' AND followup_state='ready' "
                "AND followup_notified=? ORDER BY rowid DESC", (fu.today(),)).fetchall()
            if len(pend) == 1:
                jid = pend[0][0]
        if not jid:
            last = con.execute(
                "SELECT id FROM jobs WHERE status='applied' OR (status='new' AND source='manual') "
                "ORDER BY status_date DESC, rowid DESC LIMIT 1").fetchone()
            jid = last[0] if last else None
        if not jid:
            return "Nothing to attach that to yet. Send me the job link first."
        _attach_contact(con, jid, parsed)
        r = _row(con, jid)
        h = handle_for(jid)
        if r[6] == "ready":
            return (f"Got it, {h} ({r[1]}) goes to {parsed['contact_email'] or parsed['contact_linkedin']}. "
                    f"Reply SEND {h} to email it.")
        return f"Noted for {h} ({r[1]}). I'll write the follow-up to them on {_nice_date(r[5])}."

    # A pasted job description (long text, no link) for the latest lead whose
    # posting could not be read, or that has no description yet.
    if len(raw) >= 250:
        last = con.execute(
            "SELECT id, title, company, snippet, status, drafted FROM jobs WHERE source='manual' "
            "AND status IN ('new','applied') ORDER BY status_date DESC, rowid DESC LIMIT 1").fetchone()
        if last and (not last[3] or last[1].startswith("(couldn't read")):
            jid, title, company = last[0], last[1], last[2]
            if title.startswith("(couldn't read"):
                data = fu.extract_posting(last[2], {"text": raw})
                title, company = data["title"], data["company"]
            con.execute("UPDATE jobs SET snippet=?, title=?, company=? WHERE id=?",
                        (raw[:6000], title, company, jid))
            con.commit()
            if last[4] == "new" and not last[5]:
                fu._spawn(["prep", jid])
                return (f"Thanks, using that as the posting for {handle_for(jid)} ({title} at "
                        f"{company}). Building the resume and cover letter, about 5 minutes.")
            return f"Thanks, saved that as the posting for {handle_for(jid)} ({title} at {company})."
    return ask_agent(raw)


# ------------------------------------------------------------------ notify hook
def notify_ready(con, rows: list[tuple]) -> bool:
    """Called by followup.cmd_due with the drafts that became ready today."""
    if not configured():
        return False
    ok = True
    for jid, _company, _title in rows:
        try:
            send_sms(followup_text(con, jid))
        except Exception as e:
            log(f"sms: notify failed for {jid}: {e}")
            ok = False
    return ok


# ------------------------------------------------------------------ serve
def cmd_serve() -> None:
    if not configured():
        print("The line is not set up. Put twilio_account_sid, twilio_auth_token, my_phone "
              "and twilio_from in config.json (see the top of sms.py).")
        sys.exit(1)
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 8788))  # one poller at a time
    except OSError:
        print("poller already running")
        return
    state = _state()
    if not state.get("watermark"):
        state["watermark"] = datetime.now().astimezone().isoformat()
        _save_state(state)
    me = re.sub(r"\D", "", CONFIG["my_phone"])
    every = int(CONFIG.get("sms_poll_seconds", 30))
    log(f"line: polling {CONFIG['twilio_from']} every {every}s for messages from {CONFIG['my_phone']}")
    while True:
        try:
            for m in fetch_inbound(state):
                state.setdefault("seen", []).append(m["sid"])
                _save_state(state)
                if re.sub(r"\D", "", m["from"]) != me:
                    log(f"line: ignoring message from {m['from']}")
                    continue
                log(f"line: <- {m['body'][:80]!r}")
                try:
                    reply = handle_message(m["body"])
                except Exception as e:
                    log(f"line: handler error: {e}")
                    reply = f"Something broke on my side ({str(e)[:80]}). Try again in a minute."
                send_sms(reply, to=m["from"])
        except Exception as e:
            log(f"line: poll error: {e}")
            time.sleep(60)
        time.sleep(every)


def cmd_buy_number(area_code: str = "") -> None:
    sid, tok = creds()
    if not sid or not tok:
        print("put twilio_account_sid and twilio_auth_token in config.json first")
        sys.exit(1)
    params = {"SmsEnabled": "true", "PageSize": 5}
    if area_code:
        params["AreaCode"] = area_code
    avail = _twilio("GET", "/AvailablePhoneNumbers/CA/Local.json", params=params)
    nums = avail.get("available_phone_numbers", [])
    if not nums:
        print("no SMS-capable Canadian numbers available" + (f" in {area_code}" if area_code else ""))
        sys.exit(1)
    chosen = nums[0]["phone_number"]
    bought = _twilio("POST", "/IncomingPhoneNumbers.json",
                     {"PhoneNumber": chosen, "FriendlyName": "Job follow-ups"})
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    key = "twilio_sms_from" if str(cfg.get("twilio_from", "")).startswith("whatsapp:") else "twilio_from"
    cfg[key] = bought["phone_number"]
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"bought {bought['phone_number']} and saved it as {key}. "
          f"Save it in your phone as 'Job follow-ups'.")
    if CONFIG.get("my_phone") and key == "twilio_from":
        CONFIG["twilio_from"] = bought["phone_number"]
        send_sms("This is your job line. Send me a job link and I'll write the resume and "
                 "cover letter. Send HELP for everything else.")


def cmd_selftest() -> bool:
    checks = []
    checks.append(("chunks short", _chunks("hi") == ["hi"]))
    long = "\n".join(["line " * 100] * 6)
    parts = _chunks(long)
    checks.append(("chunks split under limit", len(parts) > 1 and all(len(p) <= MAX_SMS for p in parts)))
    checks.append(("chunks numbered", parts[0].startswith("(1/")))
    saved_from = CONFIG.get("twilio_from", "")
    CONFIG["twilio_from"] = "whatsapp:+14155238886"
    CONFIG["my_phone"] = CONFIG.get("my_phone") or "+14165550100"
    checks.append(("whatsapp prefix on my address", my_address().startswith("whatsapp:+1")))
    CONFIG["twilio_from"] = saved_from
    con = fu.fdb()
    con.execute("DELETE FROM jobs WHERE id LIKE 'test-sms-%'")
    con.execute("""INSERT INTO jobs(id, source, company, title, url, first_seen, passed_filter,
                   status, status_date, applied_at, followup_due, followup_state, followup_target,
                   followup_draft) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("test-sms-1", "manual", "Acme", "CSM", "https://x.test/1", "2026-09-01", 1,
                 "applied", "2026-09-01", "2026-09-01", "2026-09-08", "ready",
                 json.dumps({"name": "Jane", "role": "Recruiter", "email": "",
                             "search_url": "https://linkedin.com/search"}),
                 json.dumps({"subject": "Following up: CSM", "body": "Hello Jane,\n\nBody."})))
    con.execute("""INSERT INTO jobs(id, source, company, title, url, first_seen, passed_filter,
                   status, status_date, drafted) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("test-sms-2", "manual", "Globex", "Support Engineer", "https://x.test/2",
                 "2026-09-02", 1, "new", "2026-09-02", 1))
    con.commit()
    try:
        t = followup_text(con, "test-sms-1")
        checks.append(("followup text has handle+subject", "#" in t and "SUBJECT: Following up: CSM" in t))
        checks.append(("no email -> linkedin hint", "No email known" in t))
        lt = list_text(con)
        checks.append(("list shows both", "Acme" in lt and "Globex" in lt and "reply APPLIED" in lt))
        # Real pending drafts may exist in leads.db, so every command names the
        # test row by handle or company instead of relying on "only candidate".
        checks.append(("resolve by company", _resolve(con, "acme", ("ready",))[0] == "test-sms-1"))
        h = handle_for("test-sms-1")
        h2 = handle_for("test-sms-2")
        checks.append(("resolve by handle", _resolve(con, h, ("ready",))[0] == "test-sms-1"))
        checks.append(("resolve unknown handle", _resolve(con, "#9999", ("ready",))[0] is None))
        n_scout = len(con.execute("SELECT 1 FROM jobs WHERE status='new' AND source<>'manual'").fetchall())
        n_mine = len(con.execute("SELECT 1 FROM jobs WHERE status='new' AND source='manual'").fetchall())
        jid_or_none, msg = _resolve(con, "", ("scheduled",), statuses=("new",))
        checks.append(("scout leads never become candidates",
                       n_scout == 0 or jid_or_none is not None or msg.count("#") <= n_mine))
        checks.append(("send without email explains", "SEND <email>" in handle_message(f"send {h}")))
        checks.append(("email attaches by handle", "Reply SEND" in handle_message(f"{h} jane@acme.test")))
        checks.append(("email stored", con.execute("SELECT contact_email FROM jobs WHERE id='test-sms-1'").fetchone()[0] == "jane@acme.test"))
        checks.append(("snooze", "snoozed" in handle_message(f"snooze {h} 5")))
        checks.append(("snoozed state", con.execute("SELECT followup_state FROM jobs WHERE id='test-sms-1'").fetchone()[0] == "scheduled"))
        rep = handle_message(f"applied {h2} Sam Lee sam@globex.test")
        checks.append(("applied on prepped row", "Filed: Support Engineer at Globex" in rep and "sam@globex.test" in rep))
        checks.append(("typo 'appleid' counts", bool(APPLIED_RE.search("I appleid alredy"))))
        checks.append(("'apply' alone does not", not APPLIED_RE.search("apply when ready")))
        st = _state(); st["pending"] = "send"; _save_state(st)
        rep = handle_message("acme")  # answers "Which one?" -> runs "send acme"
        checks.append(("answer to Which one? resumes the command",
                       any(k in rep for k in ("SEND <email>", "Sent to", "Couldn't send"))))
        row = con.execute("SELECT status, followup_due, contact_name FROM jobs WHERE id='test-sms-2'").fetchone()
        checks.append(("applied scheduled + contact name", row[0] == "applied" and bool(row[1]) and row[2] == "Sam Lee"))
        checks.append(("skip", "no follow-up" in handle_message("skip acme")))
        checks.append(("help", handle_message("help") == HELP))
    finally:
        con.execute("DELETE FROM jobs WHERE id LIKE 'test-sms-%'")
        con.commit()
        st = _state()
        for k in ("test-sms-1", "test-sms-2"):
            st.get("handles", {}).pop(k, None)
        if str(st.get("last_applied", "")).startswith("test-sms-"):
            st.pop("last_applied", None)
        st.pop("pending", None)
        _save_state(st)
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok = ok and passed
    return ok


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        cmd_serve()
    elif cmd == "buy-number":
        cmd_buy_number(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "send" and len(sys.argv) > 2:
        send_sms(" ".join(sys.argv[2:]))
    elif cmd == "handle" and len(sys.argv) > 2:  # dry-run a message without Twilio
        print(handle_message(" ".join(sys.argv[2:])))
    elif cmd == "selftest":
        sys.exit(0 if cmd_selftest() else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
