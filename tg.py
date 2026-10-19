"""The Telegram line. Same brain as sms.py (handle_message), Telegram transport.

Why Telegram: free, no join codes, no 24-hour rule, and files go straight into
the chat, so the resume and cover letter land on your phone as documents.

  you  -> https://jobs.acme.com/123
  bot  -> #4 Reading the posting and writing your resume + cover letter...
  bot  -> [Resume.pdf] [Resume.docx] [Cover_Letter.pdf] [Cover_Letter.docx]
          #4 Resume + cover letter for Customer Success Manager at Acme.
          Apply when you're ready, then reply APPLIED #4.
  you  -> APPLIED #4 Jane Doe jane@acme.com
  ... a week later ...
  bot  -> #4 Follow up: ... TO: Jane Doe jane@acme.com ... <drafted email>
  you  -> SEND
  bot  -> Sent to jane@acme.com. #4 marked followed up.

One-time setup:
  1. In Telegram open @BotFather, send /newbot, pick a name (e.g. "Matthew's
     job line") and a username ending in "bot". Copy the token it gives you.
  2. Put it in config.json as "telegram_bot_token". "telegram_username" is the
     only account the bot answers (already set).
  3. Start the poller: schtasks /run /tn JobLeadsLine (it also starts at login).
  4. Open your bot in Telegram and send it anything. It replies "Connected".

Runs by long-polling getUpdates (no public URL). `py -3 tg.py serve`.
"""
from __future__ import annotations

import json
import mimetypes
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from agent import CONFIG, log  # noqa: E402

STATE_FILE = ROOT / "tg_state.json"
MAX_TEXT = 4000  # Telegram allows 4096


# ------------------------------------------------------------------ config/state
def token() -> str:
    return str(CONFIG.get("telegram_bot_token", "")).strip()


def configured() -> bool:
    return bool(token())


def chat_id() -> str:
    return str(CONFIG.get("telegram_chat_id", "") or "").strip()


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"offset": 0}


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def _remember_chat(cid: str) -> None:
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["telegram_chat_id"] = str(cid)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    CONFIG["telegram_chat_id"] = str(cid)


# ------------------------------------------------------------------ api
def _api(method: str, data: dict | None = None, files: dict | None = None,
         timeout: int = 40) -> dict:
    url = f"https://api.telegram.org/bot{token()}/{method}"
    if files:
        boundary = "----jl" + uuid.uuid4().hex
        body = b""
        for k, v in (data or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                     f"{v}\r\n").encode()
        for k, spec in files.items():
            path, name = (spec if isinstance(spec, (tuple, list)) else (spec, None))
            path = Path(path)
            name = (name or path.name).replace('"', "'")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
            body += path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data or {}).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {e.code}: {e.read().decode()[:300]}") from None
    if not out.get("ok"):
        raise RuntimeError(f"Telegram: {out.get('description', 'unknown error')}")
    return out.get("result", {})


def _chunks(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= MAX_TEXT:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > MAX_TEXT and cur:
            parts.append(cur)
            cur = ""
        cur = (cur + "\n" + para) if cur else para
    if cur:
        parts.append(cur)
    return parts


def send(text: str, to: str | None = None) -> None:
    cid = to or chat_id()
    if not cid:
        raise RuntimeError("Telegram is not connected yet: open the bot and send it a message")
    for part in _chunks(text):
        _api("sendMessage", {"chat_id": cid, "text": part, "disable_web_page_preview": "true"})
    log(f"telegram: -> {cid} ({len(text)} chars)")


def send_documents(items: list, to: str | None = None) -> int:
    """items: paths, or (path, caption, display_filename) tuples. The caption
    sits under the file in the chat and the display name replaces the on-disk
    name, so 'Resume' vs 'Cover letter' is obvious even when Telegram
    truncates long filenames."""
    cid = to or chat_id()
    if not cid:
        raise RuntimeError("Telegram is not connected yet")
    n = 0
    for item in items:
        p, caption, name = (item if isinstance(item, (tuple, list)) else (item, "", ""))
        p = Path(p)
        if not p.exists() or p.stat().st_size > 50_000_000:
            continue
        data = {"chat_id": cid}
        if caption:
            data["caption"] = caption[:1000]
        _api("sendDocument", data, files={"document": (p, name or p.name)}, timeout=120)
        n += 1
    log(f"telegram: -> {cid} {n} file(s)")
    return n


# ------------------------------------------------------------------ hooks
def notify_ready(con, rows: list[tuple]) -> bool:
    """followup.cmd_due: message each drafted follow-up."""
    if not configured() or not chat_id():
        return False
    import sms
    ok = True
    for jid, _c, _t in rows:
        try:
            send(sms.followup_text(con, jid))
        except Exception as e:
            log(f"telegram: notify failed for {jid}: {e}")
            ok = False
    return ok


# ------------------------------------------------------------------ serve
def _authorized(msg: dict) -> bool:
    user = (msg.get("from") or {}).get("username", "") or ""
    want = str(CONFIG.get("telegram_username", "")).lstrip("@").lower()
    cid = str((msg.get("chat") or {}).get("id", ""))
    if chat_id() and cid == chat_id():
        return True
    return bool(want) and user.lower() == want


def cmd_serve() -> None:
    if not configured():
        print("telegram_bot_token is not set in config.json (see the top of tg.py)")
        sys.exit(1)
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", 8789))  # one poller at a time
    except OSError:
        print("telegram poller already running")
        return
    import sms
    me = _api("getMe")
    log(f"telegram: polling as @{me.get('username')} for @{CONFIG.get('telegram_username', '')}")
    state = _state()
    while True:
        try:
            updates = _api("getUpdates", {"offset": state.get("offset", 0), "timeout": 30,
                                          "allowed_updates": json.dumps(["message"])},
                           timeout=45)
            for u in updates:
                state["offset"] = u["update_id"] + 1
                _save_state(state)
                msg = u.get("message") or {}
                text = (msg.get("text") or msg.get("caption") or "").strip()
                cid = str((msg.get("chat") or {}).get("id", ""))
                if not _authorized(msg):
                    log(f"telegram: ignoring message from @{(msg.get('from') or {}).get('username', '?')}")
                    continue
                first = not chat_id()
                if first:
                    _remember_chat(cid)
                if not text:
                    continue
                log(f"telegram: <- {text[:80]!r}")
                if first and not re.search(r"https?://", text):
                    send("Connected. Send me a job link and I'll write your resume and cover "
                         "letter. Send HELP for everything else.", to=cid)
                    if text.lower() in ("/start", "hi", "hello", "hey", "test"):
                        continue
                try:
                    reply = sms.handle_message(text)
                except Exception as e:
                    log(f"telegram: handler error: {e}")
                    reply = f"Something broke on my side ({str(e)[:80]}). Try again in a minute."
                if reply:
                    send(reply, to=cid)
        except Exception as e:
            log(f"telegram: poll error: {e}")
            time.sleep(30)


def cmd_selftest() -> bool:
    checks = []
    checks.append(("chunks short", _chunks("hi") == ["hi"]))
    parts = _chunks("\n".join(["x" * 1000] * 9))
    checks.append(("chunks split", len(parts) == 3 and all(len(p) <= MAX_TEXT for p in parts)))
    saved = dict(CONFIG)
    CONFIG["telegram_username"] = "@Matthew_McCusker"
    CONFIG["telegram_chat_id"] = ""
    checks.append(("authorize by username, case-insensitive",
                   _authorized({"from": {"username": "matthew_mccusker"}, "chat": {"id": 1}})))
    checks.append(("reject stranger",
                   not _authorized({"from": {"username": "someone"}, "chat": {"id": 2}})))
    CONFIG["telegram_chat_id"] = "777"
    checks.append(("authorize by chat id", _authorized({"from": {}, "chat": {"id": 777}})))
    CONFIG.clear()
    CONFIG.update(saved)
    ok = True
    for name, passed in checks:
        print(("PASS " if passed else "FAIL ") + name)
        ok = ok and passed
    return ok


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        cmd_serve()
    elif cmd == "send" and len(sys.argv) > 2:
        send(" ".join(sys.argv[2:]))
    elif cmd == "selftest":
        sys.exit(0 if cmd_selftest() else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
