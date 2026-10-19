"""Render the dashboard from leads.db.

generate(config)            -> writes static dashboard.html (links only)
render(config, interactive) -> html string; interactive=True adds action buttons,
                               the Ask-the-agent box, and Run-now (server.py routes)
"""
from __future__ import annotations

import html
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _age(datestr: str | None) -> int | None:
    try:
        return (datetime.now() - datetime.strptime(datestr, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def _e(s) -> str:
    return html.escape(str(s or ""))


STATUS_CHIP = {
    "applied":     ("→", "chip-neutral", "applied"),
    "followed-up": ("↻", "chip-neutral", "followed up"),
    "interview":   ("✓", "chip-good", "interview"),
    "check":       ("!", "chip-serious", "verify submission"),
}


def _coach_block() -> tuple[str, str]:
    f = ROOT / "coach.md"
    if not f.exists():
        return "", ""
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    date, body = (lines[0], lines[1:]) if lines else ("", [])
    parts: list[str] = []
    bullets: list[str] = []
    for ln in body:
        if ln.strip().startswith("- "):
            bullets.append(f"<li>{_e(ln.strip()[2:])}</li>")
        else:
            if bullets:
                parts.append("<ul>" + "".join(bullets) + "</ul>")
                bullets = []
            if ln.strip():
                parts.append(f"<p>{_e(ln.strip())}</p>")
    if bullets:
        parts.append("<ul>" + "".join(bullets) + "</ul>")
    return date, "".join(parts)


def render(config: dict, interactive: bool = False) -> str:
    con = sqlite3.connect(ROOT / "leads.db", timeout=15)
    hot_min = config.get("hot_threshold", 75)
    look_min = config.get("worth_a_look_threshold", 60)
    stale_days = config.get("stale_days", 60)
    window = (datetime.now() - timedelta(days=config.get("digest_window_days", 3))
              ).strftime("%Y-%m-%d")
    today_s = datetime.now().strftime("%Y-%m-%d")

    rows = con.execute(
        """SELECT id, company, title, location, url, posted_at, salary, score,
                  why, flags, drafted, draft_path, status, first_seen
           FROM jobs WHERE passed_filter=1 AND score>=? AND status='new'
           ORDER BY score DESC, posted_at DESC LIMIT 120""",
        (look_min,)).fetchall()
    leads = [r for r in rows
             if (r[7] >= hot_min and (_age(r[5]) or 0) <= stale_days)
             or r[13] >= window][:50]
    try:
        pipe = con.execute(
            """SELECT id, company, title, url, status, status_date, draft_path, prep_path,
                      followup_due, followup_state, contact_name, contact_email,
                      contact_linkedin, followup_target, followup_draft, applied_at
               FROM jobs WHERE status NOT IN ('new', 'skipped', 'closed', 'rejected', 'gone')
               ORDER BY status_date DESC""").fetchall()
    except sqlite3.OperationalError:  # follow-up columns not created yet
        pipe = [tuple(r) + (None,) * 8 for r in con.execute(
            """SELECT id, company, title, url, status, status_date, draft_path, prep_path
               FROM jobs WHERE status NOT IN ('new', 'skipped', 'closed', 'rejected', 'gone')
               ORDER BY status_date DESC""").fetchall()]
    fu_ready = [r for r in pipe if r[4] == "applied" and r[9] == "ready"]

    n_hot = sum(1 for r in leads if r[7] >= hot_min and ((_age(r[5]) or 0) <= stale_days))
    n_passed = con.execute(
        "SELECT count(*) FROM jobs WHERE passed_filter=1").fetchone()[0]
    n_ready = con.execute(
        "SELECT count(*) FROM jobs WHERE drafted=1 AND status='new'").fetchone()[0]
    n_applied = con.execute(
        "SELECT count(*) FROM jobs WHERE status IN ('applied','followed-up','check')"
    ).fetchone()[0]
    n_interview = con.execute(
        "SELECT count(*) FROM jobs WHERE status='interview'").fetchone()[0]
    n_seen = con.execute("SELECT count(*) FROM jobs").fetchone()[0]
    followup_days = config.get("followup_days", 7)

    # --- health warnings
    warn_html = ""
    try:
        run_age_h = (datetime.now() - datetime.fromtimestamp(
            (ROOT / "digests" / "latest.md").stat().st_mtime)).total_seconds() / 3600
        if run_age_h > 26:
            warn_html = (f'<div class="warn">⚠ The pipeline has not run in '
                         f'{int(run_age_h)} hours — the JobLeadsAgent task may be '
                         f'stuck. Try "Run pipeline now".</div>')
    except OSError:
        warn_html = '<div class="warn">⚠ The pipeline has never run.</div>'

    # --- activity: new leads per day, last 14 days
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(13, -1, -1)]
    day_counts = dict(con.execute(
        "SELECT first_seen, count(*) FROM jobs WHERE passed_filter=1 "
        "GROUP BY first_seen").fetchall())
    vals = [day_counts.get(d, 0) for d in days]
    mx = max(vals) or 1
    bars = "".join(
        f'<span class="bar" title="{d}: {v} new lead{"s" if v != 1 else ""}"'
        f' style="height:{max(4, round(v / mx * 34))}px"></span>'
        for d, v in zip(days, vals))
    applied_week = con.execute(
        "SELECT count(*) FROM jobs WHERE status IN "
        "('applied','followed-up','interview','check') AND status_date>=?",
        ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),)).fetchone()[0]

    # --- focus: the single next best action
    focus_html = ""
    candidates = [r for r in leads if r[10] and r[7] >= hot_min
                  and (_age(r[5]) or 0) <= stale_days]
    if candidates:
        f = max(candidates, key=lambda r: (r[7], -(_age(r[5]) or 0)))
        f_age = _age(f[5])
        bits = [f"score {int(f[7])}"]
        if f[6]:
            bits.append(_e(f[6])[:36])
        if f_age is not None:
            bits.append(f"posted {f_age}d ago")
        f_btns = ""
        if interactive:
            f_btns = (f'<button class="btn btn-p" onclick="applyAssisted(this,\'{f[0]}\')">'
                      f'▶ Apply</button>'
                      f'<button class="btn" title="Auto-submit without review" '
                      f'onclick="applyAuto(this,\'{f[0]}\')">⚡ Auto</button>')
        focus_html = (f'<div class="focus"><div><span class="focus-k">Next best move</span> '
                      f'<strong>{_e(f[2])}</strong> at <strong>{_e(f[1])}</strong>'
                      f' <span class="mut">— {" · ".join(bits)} · package ready</span></div>'
                      f'<div class="acts">{f_btns}</div></div>')

    coach_date, coach_html = _coach_block()
    coach_card = ""
    if coach_html:
        coach_card = (f'<details class="scard coach" open><summary>🧭 Coach briefing '
                      f'<span class="mut">{_e(coach_date)}</span></summary>'
                      f'{coach_html}</details>')

    funnel = [("Seen", n_seen, "f1"), ("Passed filters", n_passed, "f2"),
              ("Hot open", n_hot, "f3"), ("Applied", n_applied, "f4"),
              ("Interview", n_interview, "f5")]
    funnel_html = ('<div class="scard"><div class="scard-h">Funnel</div>'
                   + "".join(f'<div class="frow"><span><span class="fdot {c}"></span>'
                             f'{_e(l)}</span><span class="fv">{v}</span></div>'
                             for l, v, c in funnel) + "</div>")

    kpis = [("Hot now", n_hot), ("Packages", n_ready),
            ("Applied", n_applied), ("Interviews", n_interview)]
    kpi_html = ('<div class="kpis">'
                + "".join(f'<div class="kpi"><div class="kpi-v">{v}</div>'
                          f'<div class="kpi-l">{_e(l)}</div></div>' for l, v in kpis)
                + "</div>")

    activity_html = (f'<div class="scard"><div class="scard-h">New leads / day '
                     f'<span class="mut">14d</span></div><div class="bars">{bars}</div>'
                     f'<div class="mut" style="font-size:11.5px">applied last 7 days: '
                     f'{applied_week}</div></div>')

    def av(company: str) -> str:
        return f'<span class="av">{_e((company or "?")[0].upper())}</span>'

    def open_btn(path: str, label: str, title: str) -> str:
        if interactive:
            return (f'<button class="btn" title="{_e(title)}" '
                    f'onclick="openPath(\'{_e(path)}\')">{label}</button>')
        return (f'<a class="pkg" title="{_e(title)}" '
                f'href="file:///{_e(str(path).replace(chr(92), "/"))}">{label}</a>')

    fresh_days = config.get("fresh_days", 14)

    def lead_row(r) -> str:
        (_id, company, title, location, url, posted, salary, score,
         why, flags, drafted, draft_path, _status, first_seen) = r
        is_new = first_seen == today_s
        age = _age(posted)
        # Unknown posting date gets the benefit of the doubt for the fresh view
        is_fresh = age is None or age <= fresh_days
        stale = age is not None and age > stale_days
        posted_s = f"{age}d" if age is not None else "—"
        new_dot = '<span class="newdot" title="found today">●</span> ' if is_new else ""
        flag_s = f'<div class="flags">⚠ {_e(flags)}</div>' if flags else ""
        stale_s = '<span class="stale" title="posting open a long time">⏳</span>' if stale else ""
        if drafted and draft_path:
            pkg = open_btn(draft_path, "📄", "Open application package")
        elif interactive:
            pkg = (f'<button class="btn" title="Build resume + cover letter + outreach" '
                   f'onclick="buildPkg(this,\'{_id}\')">📦</button>')
        else:
            pkg = ""
        if interactive:
            actions = f"""<div class="acts">{pkg}
<button class="btn btn-p" onclick="applyAssisted(this,'{_id}')">▶ Apply</button>
<button class="btn" title="Auto-submit without review" onclick="applyAuto(this,'{_id}')">⚡</button>
<button class="btn btn-ok" title="Mark as applied" onclick="act(this,'/mark?id={_id}&status=applied')">✓</button>
<button class="btn btn-x" title="Skip this lead" onclick="skipLead(this,'{_id}')">✕</button>
</div>"""
        else:
            actions = (pkg + f' <code>py -3 apply.py {_e(_id)}</code>').strip()
        data_text = _e(f"{title} {company} {location}".lower())
        return f"""<tr class="lead" data-hot="{1 if score >= hot_min else 0}" \
data-new="{1 if is_new else 0}" data-pkg="{1 if drafted else 0}" \
data-fresh="{1 if is_fresh else 0}" data-text="{data_text}">
<td class="t">{new_dot}<a href="{_e(url)}" target="_blank" rel="noopener">{_e(title)}</a>{stale_s}
  <div class="why" title="{_e(why)}">{_e(why)}</div>{flag_s}</td>
<td class="co">{av(company)}{_e(company)}</td>
<td class="num"><span class="scorebar"><span style="width:{int(score)}%"></span></span>
  <span class="scorenum">{int(score)}</span></td>
<td><div class="sal" title="{_e(salary)}">{_e(salary) or '<span class="mut">—</span>'}</div></td>
<td class="num">{posted_s}</td>
<td>{actions}</td>
</tr>"""

    def pipe_row(r) -> str:
        (_id, company, title, url, status, sdate, draft_path, prep_path,
         fu_due, fu_state, c_name, c_email, c_linkedin, _t, _d, applied_at) = r
        icon, cls, label = STATUS_CHIP.get(status, ("•", "chip-neutral", status))
        age = _age(sdate)
        if status == "applied":
            due_in = None
            try:
                due_in = (datetime.strptime(fu_due, "%Y-%m-%d") - datetime.now()).days + 1
            except (ValueError, TypeError):
                pass
            if fu_state == "ready":
                icon, cls, label = ("!", "chip-warning", "follow-up drafted ↑")
            elif fu_state == "sent":
                icon, cls, label = ("↻", "chip-neutral", "followed up")
            elif fu_state == "skipped":
                icon, cls, label = ("→", "chip-neutral", "applied · no follow-up")
            elif due_in is not None and due_in <= 0:
                icon, cls, label = ("!", "chip-warning", "follow up now")
            elif due_in is not None:
                icon, cls, label = ("→", "chip-neutral",
                                    f"applied · follow up in {due_in}d")
            elif age is not None and age >= followup_days:
                icon, cls, label = ("!", "chip-warning", "follow up now")
            if title == "(reading posting…)":
                icon, cls, label = ("…", "chip-neutral", "reading the posting")
        since = f"{_e(sdate)}" + (f" · {age}d" if age is not None else "")
        extra = ""
        if status == "interview" and prep_path:
            extra = open_btn(prep_path, "🎤 prep", "Open interview prep pack")
        elif status == "interview" and interactive:
            extra = '<span class="mut">prep generating…</span>'
        if status == "applied" and interactive and fu_state not in ("ready", "sent", "skipped"):
            extra = (f'<button class="btn" title="Draft the follow-up now instead of waiting '
                     f'for the date" onclick="fuDraft(this,\'{_id}\')">✉ Draft follow-up</button>')
        if interactive and str(_id).startswith("ap-"):
            extra += (f'<button class="btn" title="Fix the role or company name" '
                      f'onclick="fuEdit(this,\'{_id}\',\'{_e(title)}\',\'{_e(company)}\')">✎</button>')
        if interactive:
            nxt = {
                "applied": [("🎤 Interview", "interview"), ("✕", "closed")],
                "followed-up": [("🎤 Interview", "interview"), ("✕", "closed")],
                "interview": [("✕", "closed")],
                "check": [("✓ It went through", "applied"), ("✕", "closed")],
            }.get(status, [("✕", "closed")])
            btns = "".join(
                f'<button class="btn" title="Mark {s}" '
                f'onclick="act(this,\'/mark?id={_id}&status={s}\')">{t}</button>'
                for t, s in nxt)
            extra = f'<div class="acts">{extra}{btns}</div>'
        return f"""<tr>
<td class="t"><a href="{_e(url)}" target="_blank" rel="noopener">{_e(title)}</a></td>
<td class="co">{av(company)}{_e(company)}</td>
<td><span class="chip {cls}"><span class="chip-ic">{icon}</span>{_e(label)}</span></td>
<td class="num">{since}</td>
<td>{extra}</td>
</tr>"""

    def fu_card(r) -> str:
        (_id, company, title, url, _s, _sd, draft_path, _p, fu_due, _st, c_name,
         c_email, c_linkedin, tgt_s, draft_s, applied_at) = r
        try:
            tgt = json.loads(tgt_s or "{}")
            draft = json.loads(draft_s or "{}")
        except ValueError:
            tgt, draft = {}, {}
        to_email = tgt.get("email") or c_email or ""
        name = tgt.get("name") or c_name or ""
        role = tgt.get("role") or ""
        li = tgt.get("linkedin_url") or c_linkedin or ""
        search = tgt.get("search_url") or ""
        who = " · ".join(x for x in (name, role) if x) or "no named contact yet"
        links = ""
        if li:
            links += f' <a href="{_e(li)}" target="_blank" rel="noopener">LinkedIn profile ↗</a>'
        elif search:
            links += f' <a href="{_e(search)}" target="_blank" rel="noopener">find them on LinkedIn ↗</a>'
        why = f'<div class="fu-why">{_e(tgt.get("why", ""))}</div>' if tgt.get("why") else ""
        channel_note = ("Send goes out from your Gmail." if to_email else
                        "No email address is known, so this one goes by LinkedIn message: "
                        "copy the text, paste it there, then press “I sent it”. Or type an "
                        "email address below and Send.")
        return f"""<div class="fu" id="fu-{_id}">
<div class="fu-head"><div><strong>{_e(title)}</strong> at <strong>{_e(company)}</strong>
 <span class="mut">· applied {_e(applied_at or _sd)} · <a href="{_e(url)}" target="_blank" rel="noopener">posting ↗</a></span></div>
<div class="acts"><button class="btn" title="Push the date out 3 days" onclick="act(this,'/followup/snooze?id={_id}&days=3')">⏰ 3 days</button>
<button class="btn btn-x" title="Never mind, no follow-up for this one" onclick="act(this,'/followup/skip?id={_id}')">✕</button></div></div>
<div class="fu-target"><span class="fu-k">To</span> {_e(who)}{links}{why}
<div class="mut" style="font-size:12px;margin-top:4px">{channel_note}</div></div>
<div class="fu-grid">
<label>Email address <input id="to-{_id}" type="email" value="{_e(to_email)}" placeholder="name@company.com (leave blank for LinkedIn)"></label>
<label>Subject <input id="sub-{_id}" type="text" value="{_e(draft.get("subject", ""))}"></label>
</div>
<textarea id="body-{_id}" rows="9">{_e(draft.get("body", ""))}</textarea>
<div class="acts fu-acts">
<button class="btn btn-p" onclick="fuSend(this,'{_id}')">✉ Send</button>
<button class="btn" onclick="fuCopy(this,'{_id}','{_e(li or search)}')">📋 Copy for LinkedIn</button>
<button class="btn btn-ok" title="Mark followed up (you sent it another way)" onclick="act(this,'/followup/sent?id={_id}&how=LinkedIn')">✓ I sent it</button>
<button class="btn" title="Redraft with a different contact" onclick="fuRedraft(this,'{_id}')">↻ Redraft</button>
</div></div>"""

    followups_html = ""
    if interactive and fu_ready:
        followups_html = (f'<h2 id="followups">Follow-ups ready to send '
                          f'<span class="count">{len(fu_ready)}</span></h2>'
                          + "".join(fu_card(r) for r in fu_ready))
    elif interactive:
        followups_html = '<span id="followups"></span>'

    applied_box = ""
    if interactive:
        applied_box = """<div class="applied-box">
<div class="fu-k" style="margin-bottom:6px">I just applied</div>
<textarea id="applied-text" rows="2" placeholder="Paste the job link (or several). Add a recruiter's name, email or LinkedIn profile if you have one. Then Enter."
 onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();fileApplied();}"></textarea>
<div class="acts" style="margin-top:6px"><button class="btn btn-p" id="applied-btn" onclick="fileApplied()">✓ File it</button>
<span class="mut" style="font-size:12px">Reads the posting, schedules the follow-up and the calendar events. Works for any link, not just the leads below.</span></div>
</div>"""

    leads_rows = "".join(lead_row(r) for r in leads) or \
        '<tr><td colspan="6" class="mut">No open scored leads — run the pipeline.</td></tr>'
    pipe_rows = "".join(pipe_row(r) for r in pipe) or \
        '<tr><td colspan="5" class="mut">Nothing in flight yet — apply to something!</td></tr>'

    try:
        last_run = datetime.fromtimestamp(
            (ROOT / "digests" / "latest.md").stat().st_mtime).strftime("%b %d, %H:%M")
    except OSError:
        last_run = "never"

    filters_html = f"""<div class="filters">
<input id="q" type="search" placeholder="Search roles, companies…" oninput="applyFilters()">
<button class="chip-f active" data-f="fresh" onclick="setFilter(this)">🕐 ≤ {fresh_days} days</button>
<button class="chip-f" data-f="all" onclick="setFilter(this)">All ages</button>
<button class="chip-f" data-f="hot" onclick="setFilter(this)">🔥 Hot</button>
<button class="chip-f" data-f="new" onclick="setFilter(this)">● New</button>
<button class="chip-f" data-f="pkg" onclick="setFilter(this)">📄 Ready</button>
</div>"""

    base_script = """function setFilter(el) {
  document.querySelectorAll('.chip-f').forEach(c => c.classList.remove('active'));
  el.classList.add('active'); applyFilters();
}
function applyFilters() {
  const f = document.querySelector('.chip-f.active').dataset.f;
  const q = (document.getElementById('q').value || '').toLowerCase();
  document.querySelectorAll('tr.lead').forEach(tr => {
    let ok = f === 'all' || tr.dataset[f] === '1';
    if (ok && q) ok = tr.dataset.text.includes(q);
    tr.hidden = !ok;
  });
}
function themeGet() { try { return localStorage.getItem('jl-theme'); } catch (e) { return null; } }
function themeSet(v) { try { v ? localStorage.setItem('jl-theme', v)
  : localStorage.removeItem('jl-theme'); } catch (e) {} }
function themeApply() {
  const t = themeGet();
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const b = document.getElementById('themebtn');
  if (b) { b.textContent = t === 'dark' ? '🌙' : t === 'light' ? '☀️' : '🖥️';
           b.title = 'Theme: ' + (t || 'system') + ' (click to change)'; }
}
function cycleTheme() {
  const t = themeGet();
  themeSet(t === null ? 'dark' : t === 'dark' ? 'light' : null);
  themeApply();
}
document.addEventListener('DOMContentLoaded', () => { themeApply(); applyFilters(); });
"""

    if interactive:
        header_extra = ('<a class="btn" href="/setup">⚙ Setup</a>'
                        '<button class="btn btn-p" onclick="runNow(this)">'
                        '↻ Run pipeline now</button>'
                        + (f'<button class="btn" onclick="applyAll(this)">'
                           f'⚡ Apply to all ready ({n_ready})</button>'
                           if n_ready else ""))
        ask_card = """<div class="scard"><div class="scard-h">Ask the agent</div>
<input id="askq" type="text" placeholder="Which should I do first?"
 onkeydown="if(event.key==='Enter')ask()">
<button class="btn btn-p" id="askbtn" onclick="ask()" style="margin-top:8px">Ask</button>
<div id="answer" class="answer" hidden></div></div>"""
        script = "<script>" + base_script + """
function snack(msg) {
  const s = document.getElementById('snack');
  s.textContent = msg; s.classList.add('show');
  clearTimeout(s._t); s._t = setTimeout(() => s.classList.remove('show'), 5000);
}
async function act(btn, path) {
  if (btn) { btn.disabled = true; }
  try { const r = await fetch(path, {method: 'POST'});
        if (r.ok) location.reload(); else { snack(await r.text()); if (btn) btn.disabled = false; } }
  catch (e) { snack('Console server not reachable'); if (btn) btn.disabled = false; }
}
function openPath(p) {
  fetch('/open?path=' + encodeURIComponent(p), {method: 'POST'})
    .catch(() => snack('Console server not reachable'));
}
function applyAssisted(btn, id) {
  btn.disabled = true; btn.textContent = 'Opening…';
  fetch('/apply?id=' + encodeURIComponent(id) + '&mode=assisted', {method: 'POST'})
    .then(r => { if (r.ok) snack('Browser opening with the form pre-filled. Review, click Submit on the site, close it, then press ✓.');
                 else r.text().then(snack);
                 btn.disabled = false; btn.textContent = '▶ Apply'; })
    .catch(() => { snack('Console server not reachable'); btn.disabled = false; });
}
function applyAuto(btn, id) {
  if (!confirm('Auto-submit this application without review?')) return;
  btn.disabled = true;
  fetch('/apply?id=' + encodeURIComponent(id) + '&mode=auto', {method: 'POST'})
    .then(r => { if (r.ok) snack('Auto-applying in the background — refresh in a minute or two.');
                 else r.text().then(snack); })
    .catch(() => snack('Console server not reachable'));
}
function skipLead(btn, id) {
  let r = prompt('Skip — why? (optional; teaches the scorer what you don\\'t want)');
  if (r === null) return;
  let path = '/mark?id=' + encodeURIComponent(id) + '&status=skipped';
  if (r.trim()) path += '&reason=' + encodeURIComponent(r.trim());
  act(btn, path);
}
function applyAll(btn) {
  if (!confirm('Auto-submit EVERY ready application (no per-form review)?\\n' +
               'Forms with CAPTCHAs are skipped and left for assisted mode.')) return;
  btn.disabled = true; btn.textContent = 'Applying to all…';
  fetch('/apply-all', {method: 'POST'})
    .then(r => { if (r.ok) snack('Applying to every ready lead in the background — check In flight in ~10 minutes.');
                 else r.text().then(snack); })
    .catch(() => snack('Console server not reachable'));
}
function buildPkg(btn, id) {
  btn.disabled = true;
  fetch('/draft?id=' + encodeURIComponent(id), {method: 'POST'})
    .then(r => { if (r.ok) snack('Building the package — ready in ~3 minutes; refresh then.');
                 else r.text().then(snack); })
    .catch(() => { snack('Console server not reachable'); btn.disabled = false; });
}
async function fileApplied() {
  const ta = document.getElementById('applied-text');
  const btn = document.getElementById('applied-btn');
  const text = ta.value.trim();
  if (!text) { snack('Paste a job link first.'); return; }
  btn.disabled = true; btn.textContent = 'Filing…';
  try {
    const r = await fetch('/applied', {method: 'POST', body: JSON.stringify({text})});
    if (r.ok) {
      const d = await r.json();
      snack('Filed ' + d.ids.length + ' application' + (d.ids.length === 1 ? '' : 's') +
            '. Reading the posting in the background; the row fills in within a minute.');
      ta.value = '';
      setTimeout(() => location.reload(), 1500);
    } else { snack(await r.text()); btn.disabled = false; btn.textContent = '✓ File it'; }
  } catch (e) { snack('Console server not reachable'); btn.disabled = false; btn.textContent = '✓ File it'; }
}
function fuDraft(btn, id) {
  btn.disabled = true; btn.textContent = 'Drafting…';
  fetch('/followup/draft?id=' + encodeURIComponent(id), {method: 'POST'})
    .then(r => { if (r.ok) { snack('Drafting the follow-up — about a minute, then it appears at the top.');
                             setTimeout(() => location.reload(), 60000); }
                 else r.text().then(snack); })
    .catch(() => snack('Console server not reachable'));
}
async function fuSend(btn, id) {
  const to = document.getElementById('to-' + id).value.trim();
  const subject = document.getElementById('sub-' + id).value.trim();
  const body = document.getElementById('body-' + id).value.trim();
  if (!to) { snack('No email address — use “Copy for LinkedIn” and “I sent it” instead, or type an address.'); return; }
  if (!confirm('Send this follow-up to ' + to + ' from your Gmail now?')) return;
  btn.disabled = true; btn.textContent = 'Sending…';
  try {
    const r = await fetch('/followup/send', {method: 'POST',
      body: JSON.stringify({id, to, subject, body})});
    const t = await r.text();
    snack(t);
    if (r.ok) setTimeout(() => location.reload(), 1200);
    else { btn.disabled = false; btn.textContent = '✉ Send'; }
  } catch (e) { snack('Console server not reachable'); btn.disabled = false; btn.textContent = '✉ Send'; }
}
function fuCopy(btn, id, link) {
  const body = document.getElementById('body-' + id).value;
  const done = () => { snack('Copied. Paste it into the LinkedIn message, then press “I sent it”.');
                       if (link) window.open(link, '_blank'); };
  if (navigator.clipboard) navigator.clipboard.writeText(body).then(done, done);
  else { document.getElementById('body-' + id).select(); document.execCommand('copy'); done(); }
}
function fuEdit(btn, id, title, company) {
  const t = prompt('Role title', title.startsWith('(') ? '' : title);
  if (t === null || !t.trim()) return;
  const c = prompt('Company', company) || company;
  const notes = prompt('Anything about the role worth remembering for the follow-up? (optional)') || '';
  fetch('/followup/edit', {method: 'POST', body: JSON.stringify({id, title: t.trim(), company: c.trim(), notes})})
    .then(r => { if (r.ok) location.reload(); else r.text().then(snack); })
    .catch(() => snack('Console server not reachable'));
}
function fuRedraft(btn, id) {
  const name = prompt('Who should this go to? (name, optional)') ;
  if (name === null) return;
  const email = prompt('Their email (optional, leave blank for LinkedIn)') || '';
  const linkedin = prompt('Their LinkedIn profile URL (optional)') || '';
  btn.disabled = true; btn.textContent = 'Redrafting…';
  fetch('/followup/contact', {method: 'POST',
    body: JSON.stringify({id, name, email, linkedin, redraft: true})})
    .then(r => { if (r.ok) { snack('Redrafting for ' + (name || 'the new contact') + ' — about a minute.');
                             setTimeout(() => location.reload(), 60000); }
                 else r.text().then(snack); })
    .catch(() => snack('Console server not reachable'));
}
function runNow(btn) {
  btn.disabled = true; btn.textContent = 'Running… (a few minutes)';
  fetch('/run', {method: 'POST'}).catch(() => snack('Console server not reachable'));
  setTimeout(() => location.reload(), 120000);
}
async function ask() {
  const q = document.getElementById('askq').value.trim();
  if (!q) return;
  const btn = document.getElementById('askbtn');
  const out = document.getElementById('answer');
  btn.disabled = true; btn.textContent = 'Thinking…';
  out.hidden = false; out.textContent = 'Reading your pipeline…';
  try {
    const r = await fetch('/ask', {method: 'POST', body: JSON.stringify({q})});
    out.textContent = await r.text();
  } catch (e) { out.textContent = 'Console server not reachable.'; }
  btn.disabled = false; btn.textContent = 'Ask';
}
</script>"""
        snack_div = '<div id="snack"></div>'
        sub_links = ""
    else:
        header_extra = ""
        ask_card = ""
        snack_div = ""
        script = "<script>" + base_script + "</script>"
        sub_links = ' · <a href="digests/latest.md">digest</a> · <a href="applications/">applications</a>'

    theme_btn = '<button class="btn" id="themebtn" onclick="cycleTheme()">🖥️</button>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎯</text></svg>">
<title>Job Leads Console</title>
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --mut: #898781; --grid: #e1e0d9; --ring: rgba(11,11,11,0.10);
  --seq: #2a78d6; --seq-track: #cde2fb;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a;
  --f1: #86b6ef; --f2: #5598e7; --f3: #2a78d6; --f4: #1c5cab; --f5: #0d366b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --mut: #898781; --grid: #2c2c2a; --ring: rgba(255,255,255,0.10);
    --seq: #3987e5; --seq-track: #184f95;
    --f1: #184f95; --f2: #256abf; --f3: #3987e5; --f4: #6da7ec; --f5: #9ec5f4;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --mut: #898781; --grid: #2c2c2a; --ring: rgba(255,255,255,0.10);
  --seq: #3987e5; --seq-track: #184f95;
  --f1: #184f95; --f2: #256abf; --f3: #3987e5; --f4: #6da7ec; --f5: #9ec5f4;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 22px; }}
.wrap {{ max-width: 1280px; margin: 0 auto; }}
.head {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
.head .spacer {{ flex: 1; }}
h1 {{ font-size: 20px; margin: 0; }}
.sub {{ color: var(--mut); font-size: 12.5px; margin: 4px 0 14px; }}
h2 {{ font-size: 13px; margin: 22px 0 8px; color: var(--ink-2);
  text-transform: uppercase; letter-spacing: .04em; font-weight: 600;
  display: flex; align-items: center; gap: 8px; }}
h2:first-child {{ margin-top: 0; }}
.count {{ background: var(--surface); border: 1px solid var(--ring);
  border-radius: 999px; padding: 0 8px; font-size: 11.5px; color: var(--mut);
  font-variant-numeric: tabular-nums; }}
.layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 18px;
  align-items: start; }}
@media (max-width: 1100px) {{ .layout {{ grid-template-columns: 1fr; }} }}
.warn {{ background: var(--surface); border: 1px solid var(--warning);
  border-radius: 10px; padding: 10px 14px; margin-bottom: 12px;
  color: var(--ink-2); font-weight: 600; }}
.focus {{ display: flex; justify-content: space-between; align-items: center;
  gap: 12px; flex-wrap: wrap; background: var(--surface);
  border: 1px solid var(--seq); border-radius: 10px; padding: 11px 14px;
  margin-bottom: 14px; }}
.focus-k {{ color: var(--seq); font-weight: 700; font-size: 12px;
  text-transform: uppercase; letter-spacing: .04em; margin-right: 6px; }}
.scard {{ background: var(--surface); border: 1px solid var(--ring);
  border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }}
.scard-h {{ font-weight: 700; font-size: 12.5px; margin-bottom: 8px;
  color: var(--ink); }}
.kpis {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  margin-bottom: 12px; }}
.kpi {{ background: var(--surface); border: 1px solid var(--ring);
  border-radius: 10px; padding: 10px 12px; }}
.kpi-v {{ font-size: 22px; font-weight: 650; }}
.kpi-l {{ color: var(--ink-2); font-size: 12px; }}
.frow {{ display: flex; justify-content: space-between; align-items: center;
  padding: 3px 0; color: var(--ink-2); font-size: 12.5px; }}
.fv {{ font-variant-numeric: tabular-nums; color: var(--ink); font-weight: 650; }}
.fdot {{ display: inline-block; width: 9px; height: 9px; border-radius: 3px;
  margin-right: 7px; vertical-align: -1px; }}
.f1 {{ background: var(--f1); }} .f2 {{ background: var(--f2); }}
.f3 {{ background: var(--f3); }} .f4 {{ background: var(--f4); }}
.f5 {{ background: var(--f5); }}
.coach summary {{ font-weight: 700; font-size: 12.5px; cursor: pointer;
  list-style: none; }}
.coach summary::before {{ content: "▸ "; color: var(--mut); }}
.coach[open] summary::before {{ content: "▾ "; }}
.coach p {{ margin: 8px 0 4px; color: var(--ink-2); font-size: 13px; }}
.coach ul {{ margin: 6px 0 2px; padding-left: 18px; color: var(--ink-2);
  font-size: 13px; }}
.coach li {{ margin: 3px 0; }}
.bars {{ display: flex; gap: 3px; align-items: flex-end; height: 38px;
  margin-bottom: 6px; }}
.bar {{ flex: 1; max-width: 12px; border-radius: 3px 3px 0 0;
  background: var(--seq); display: inline-block; }}
.filters {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  margin: 0 0 10px; }}
.filters input {{ font: 13px system-ui, sans-serif; color: var(--ink);
  background: var(--surface); border: 1px solid var(--ring); border-radius: 8px;
  padding: 6px 10px; width: 220px; }}
.chip-f {{ font: 12.5px system-ui, sans-serif; color: var(--ink-2);
  background: var(--surface); border: 1px solid var(--ring);
  border-radius: 999px; padding: 5px 12px; cursor: pointer; }}
.chip-f.active {{ color: var(--seq); border-color: var(--seq); font-weight: 600; }}
.card {{ background: var(--surface); border: 1px solid var(--ring);
  border-radius: 10px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; min-width: 780px; }}
th {{ text-align: left; color: var(--mut); font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .03em; padding: 9px 12px;
  border-bottom: 1px solid var(--grid); }}
td {{ padding: 10px 12px; border-bottom: 1px solid var(--grid);
  vertical-align: top; }}
tr:last-child td {{ border-bottom: 0; }}
tr:hover td {{ background: color-mix(in srgb, var(--seq) 4%, transparent); }}
a {{ color: var(--seq); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.t a {{ color: var(--ink); font-weight: 600; }}
.why {{ color: var(--ink-2); font-size: 12.5px; margin-top: 2px; max-width: 380px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; }}
.flags {{ color: var(--ink-2); font-size: 12px; margin-top: 2px; }}
.num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
.sal {{ font-size: 12px; max-width: 150px; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
.mut {{ color: var(--mut); }}
.stale {{ margin-left: 6px; }}
.newdot {{ color: var(--good); font-size: 10px; vertical-align: 2px; }}
.co {{ white-space: nowrap; }}
.av {{ display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 50%; background: var(--page);
  border: 1px solid var(--ring); color: var(--ink-2); font-size: 11px;
  font-weight: 700; margin-right: 7px; vertical-align: -5px; }}
.scorebar {{ display: inline-block; width: 56px; height: 6px; border-radius: 4px;
  background: color-mix(in srgb, var(--seq-track) 45%, transparent);
  vertical-align: 2px; overflow: hidden; }}
.scorebar > span {{ display: block; height: 100%; border-radius: 4px;
  background: var(--seq); }}
.scorenum {{ margin-left: 6px; font-weight: 650; font-variant-numeric: tabular-nums; }}
code {{ font: 12px ui-monospace, Consolas, monospace; background: var(--page);
  border: 1px solid var(--grid); border-radius: 6px; padding: 2px 6px;
  white-space: nowrap; user-select: all; }}
.chip {{ display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
  font-weight: 600; border: 1px solid var(--ring); border-radius: 999px;
  padding: 2px 9px; color: var(--ink-2); white-space: nowrap; }}
.chip-ic {{ font-weight: 700; }}
.chip-good .chip-ic {{ color: var(--good); }}
.chip-warning .chip-ic {{ color: var(--warning); }}
.chip-serious .chip-ic {{ color: var(--serious); }}
.pkg {{ font-size: 12.5px; white-space: nowrap; }}
.acts {{ display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }}
.btn {{ font: 12px system-ui, sans-serif; color: var(--ink-2);
  background: var(--surface); border: 1px solid var(--ring); border-radius: 7px;
  padding: 4px 8px; cursor: pointer; white-space: nowrap;
  transition: border-color .12s, color .12s; }}
.btn:hover {{ border-color: var(--seq); color: var(--seq); }}
.btn:disabled {{ opacity: .55; cursor: default; }}
.btn-p {{ color: var(--seq); border-color: var(--seq); font-weight: 600; }}
.btn-ok:hover {{ border-color: var(--good); color: var(--good); }}
.btn-x:hover {{ border-color: var(--serious); color: var(--serious); }}
.scard input {{ width: 100%; font: 13px system-ui, sans-serif; color: var(--ink);
  background: var(--page); border: 1px solid var(--ring); border-radius: 8px;
  padding: 7px 10px; }}
.answer {{ margin-top: 10px; white-space: pre-wrap; color: var(--ink-2);
  font-size: 12.5px; border-top: 1px solid var(--grid); padding-top: 10px; }}
#snack {{ position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%)
  translateY(80px); background: var(--ink); color: var(--page);
  border-radius: 10px; padding: 10px 18px; font-size: 13px; max-width: 560px;
  opacity: 0; transition: all .25s ease; pointer-events: none; z-index: 50; }}
#snack.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
[hidden] {{ display: none !important; }}
.foot {{ color: var(--mut); font-size: 12px; margin-top: 18px; }}
.applied-box {{ background: var(--surface); border: 1px solid var(--seq);
  border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }}
.applied-box textarea, .fu textarea {{ width: 100%; font: 13px/1.45 system-ui, sans-serif;
  color: var(--ink); background: var(--page); border: 1px solid var(--ring);
  border-radius: 8px; padding: 8px 10px; resize: vertical; }}
.fu-k {{ color: var(--seq); font-weight: 700; font-size: 12px;
  text-transform: uppercase; letter-spacing: .04em; }}
.fu {{ background: var(--surface); border: 1px solid var(--warning);
  border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }}
.fu-head {{ display: flex; justify-content: space-between; gap: 10px;
  flex-wrap: wrap; align-items: flex-start; }}
.fu-target {{ margin: 8px 0; padding: 8px 10px; background: var(--page);
  border: 1px solid var(--grid); border-radius: 8px; font-size: 13px; }}
.fu-target .fu-k {{ margin-right: 6px; }}
.fu-why {{ color: var(--ink-2); font-size: 12.5px; margin-top: 3px; }}
.fu-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 8px 0; }}
@media (max-width: 700px) {{ .fu-grid {{ grid-template-columns: 1fr; }} }}
.fu-grid label {{ font-size: 12px; color: var(--ink-2); font-weight: 600; }}
.fu-grid input {{ display: block; width: 100%; margin-top: 3px; font: 13px system-ui, sans-serif;
  color: var(--ink); background: var(--page); border: 1px solid var(--ring);
  border-radius: 8px; padding: 7px 10px; }}
.fu-acts {{ margin-top: 8px; }}
</style>{script}</head><body><div class="wrap">
<div class="head"><h1>🎯 Job Leads Console</h1>{header_extra}
<span class="spacer"></span>{theme_btn}</div>
<div class="sub">Pipeline last ran {_e(last_run)} · runs 8:00 &amp; 13:00 and at
 every login{sub_links}</div>
{warn_html}{applied_box}{followups_html}{focus_html}
<div class="layout">
<div class="main">
<h2>Open leads <span class="count">{len(leads)}</span></h2>
{filters_html}
<div class="card"><table>
<thead><tr><th>Role</th><th>Company</th><th>Score</th><th>Salary</th><th>Posted</th>
<th>Actions</th></tr></thead>
<tbody>{leads_rows}</tbody></table></div>
<h2>In flight <span class="count">{len(pipe)}</span></h2>
<div class="card"><table>
<thead><tr><th>Role</th><th>Company</th><th>Status</th><th>Since</th><th>Actions</th></tr></thead>
<tbody>{pipe_rows}</tbody></table></div>
<div class="foot">▶ Apply pre-fills the form for you to review and submit ·
⚡ auto-submits (skips CAPTCHA forms) · 📄 opens the package · 📦 builds one ·
✓ applied · ✕ skip · ✉ drafts the follow-up early · page refreshes every 15 minutes.</div>
</div>
<aside class="side">
{kpi_html}{coach_card}{funnel_html}{activity_html}{ask_card}
</aside>
</div>
</div>{snack_div}</body></html>"""


def generate(config: dict) -> Path:
    out = ROOT / "dashboard.html"
    out.write_text(render(config, interactive=False), encoding="utf-8")
    return out
