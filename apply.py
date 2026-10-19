"""Apply assistant — fills job application forms, and can submit them for you.

Modes:
  py -3 apply.py <job-id>              # assisted: fills everything, YOU click Submit
  py -3 apply.py <job-id> --auto       # auto: fills AND submits (headless), marks applied
  py -3 apply.py --auto-all            # auto-applies to every drafted, still-new lead
  py -3 apply.py <job-id> --test       # dry run: fill + screenshot + report, never submits
  add --no-questions to skip Claude-drafted custom answers

Auto mode safety mechanics (not optional):
  - If an interactive CAPTCHA is present, it does NOT submit (it cannot solve them and
    will not try to bypass them) — the job is left for assisted mode.
  - Before submitting it screenshots the filled form, after submitting it screenshots
    the confirmation, both saved to the application folder as an audit trail.
  - If no confirmation is detected after submitting, the lead is marked 'check'
    (not 'applied') so you can verify it yourself.

Job ids are shown in the digest under each lead.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent import CONFIG, DB_PATH, ROOT, claude_command, log

ANSWERS = json.loads((ROOT / "answers.json").read_text(encoding="utf-8"))

# Fields a bot must NEVER answer — identity/demographics stay human, and
# optional link/meta fields stay blank like a human would leave them.
NEVER_ANSWER = re.compile(
    r"pronunciation|pronoun|gender|race|ethnic|veteran|disab|diversity|"
    r"indigenous|lgbtq|salary|compensation|referr|github|twitter|portfolio|"
    r"website|personal site|other links|\burl\b", re.I)

FIELD_MAP = [
    (r"first\s*name", "first_name"),
    (r"last\s*name|surname|family\s*name", "last_name"),
    (r"^\s*name\s*\*?\s*$|full\s*name|your\s*name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile", "phone"),
    (r"current\s*location|location|city", "location"),
    (r"linked\s*in", "linkedin"),
    (r"website|portfolio|personal\s*site|github", "website"),
    (r"how\s*did\s*you\s*hear|referral\s*source", "how_heard"),
]


def find_documents(draft_path: str | None):
    """Returns (resume_pdf, cover_letter_text, cover_letter_pdf) — any may be None."""
    resume = cover = cover_pdf = None
    if draft_path and Path(draft_path).exists():
        pdfs = sorted(Path(draft_path).glob("*.pdf"))
        resumes = [p for p in pdfs if "resume" in p.name.lower()] or \
            [p for p in pdfs if "cover" not in p.name.lower()]
        if resumes:
            resume = str(resumes[0])
        covers = [p for p in pdfs if "cover" in p.name.lower()]
        if covers:
            cover_pdf = str(covers[0])
        cl = Path(draft_path) / "cover_letter.md"
        if cl.exists():
            cover = re.sub(r"^#.*\n+", "", cl.read_text(encoding="utf-8")).strip()
    return resume, cover, cover_pdf


def file_inputs_by_kind(page):
    """Pick the resume and cover-letter file inputs by their surrounding label;
    fall back to first/second file input when the form doesn't say."""
    resume_in = cover_in = None
    try:
        inputs = page.locator('input[type="file"]').all()
    except Exception:
        return None, None
    for inp in inputs:
        try:
            lbl = label_text(inp).lower()
        except Exception:
            lbl = ""
        if "cover" in lbl and cover_in is None:
            cover_in = inp
        elif ("resume" in lbl or "cv" in lbl or "curriculum" in lbl) and resume_in is None:
            resume_in = inp
    if resume_in is None and inputs:
        resume_in = inputs[0]
    if cover_in is None and len(inputs) > 1 and inputs[1] is not resume_in:
        cover_in = inputs[1]
    return resume_in, cover_in


def application_url(url: str) -> str:
    if "jobs.lever.co" in url and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply"
    if "jobs.ashbyhq.com" in url and "/application" not in url:
        return url.rstrip("/") + "/application"
    return url


def draft_answer(question: str, company: str, title: str, cover: str | None,
                 snippet: str | None, short: bool = False) -> str | None:
    facts = (ROOT / "facts.md").read_text(encoding="utf-8")
    stdin_doc = (
        "FACTS:\n" + facts +
        ("\n\nCOVER LETTER ALREADY WRITTEN FOR THIS JOB:\n" + cover if cover else "") +
        f"\n\nJOB: {title} at {company}\n" +
        ("JOB DESCRIPTION EXCERPT:\n" + snippet[:800] if snippet else "")
    )
    length_rule = ("one concise line (a short field, not an essay)" if short
                   else "2-5 sentences (or shorter if the question calls for it)")
    instruction = (
        "An online job application form asks the candidate this question:\n"
        f"\"{question.strip()[:400]}\"\n"
        f"Write the answer Matthew should type, first person, truthful to FACTS only, "
        f"{length_rule}, plain text, no markdown. Respond with ONLY the answer text."
    )
    try:
        proc = subprocess.run(
            claude_command() + ["-p", instruction, "--model", CONFIG.get("draft_model", "sonnet")],
            input=stdin_doc, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        answer = proc.stdout.strip()
        return answer if proc.returncode == 0 and 0 < len(answer) < 3000 else None
    except Exception:
        return None


def label_text(el) -> str:
    return el.evaluate(
        """el => {
            if (el.id) {
                const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (l) return l.innerText;
            }
            const wrap = el.closest('label');
            if (wrap) return wrap.innerText;
            const aria = el.getAttribute('aria-label');
            if (aria) return aria;
            const cont = el.closest('div,fieldset');
            return cont ? cont.innerText.slice(0, 160) : '';
        }""") or ""


def is_required(el, label: str) -> bool:
    try:
        if el.evaluate("el => el.required || el.getAttribute('aria-required') === 'true'"):
            return True
    except Exception:
        pass
    return "*" in label


def select_fuzzy(sel_el, candidates: list[str]) -> bool:
    """Pick a <select> option matching the first candidate that exists —
    exact label first, then any option containing the candidate text."""
    for answer in candidates:
        try:
            sel_el.select_option(label=answer)
            return True
        except Exception:
            pass
        try:
            for opt in sel_el.locator("option").all_inner_texts():
                if answer.lower() in opt.lower():
                    sel_el.select_option(label=opt)
                    return True
        except Exception:
            pass
    return False


def rule_candidates(rule: dict) -> list[str]:
    return [rule["answer"]] + list(rule.get("fallbacks", []))


def scroll_to_form_start(page) -> None:
    """After filling, put the user at the top of the form (not the bottom where
    the last field was filled) so they can review top-down."""
    try:
        first = page.get_by_label(re.compile(r"first\s*name|^\s*name\b", re.I)).first
        if first.count():
            first.scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, -140)")
            return
    except Exception:
        pass
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


def inject_review_banner(page, filled: list[str], needs_review: list[str]) -> None:
    """Show what was filled and what needs human eyes, inside the page itself,
    outline the REAL submit button in red, and offer a jump-to-it button."""
    try:
        page.evaluate(
            """(data) => {
                const old = document.getElementById('jla-review');
                if (old) old.remove();
                // Find the real submit control (the last button that says Submit);
                // on Greenhouse the blue 'Apply' at the top only scrolls down.
                const btns = [...document.querySelectorAll('button, input[type=submit]')];
                const submit = btns.filter(b => /submit/i.test(b.innerText || b.value || '')).pop();
                if (submit) {
                    submit.style.outline = '3px solid #d03b3b';
                    submit.style.outlineOffset = '3px';
                    submit.style.boxShadow = '0 0 0 8px rgba(208,59,59,.22)';
                }
                const d = document.createElement('div');
                d.id = 'jla-review';
                d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
                  + 'background:#12233b;color:#fff;font:13px/1.5 system-ui,sans-serif;'
                  + 'padding:10px 46px 10px 16px;box-shadow:0 2px 10px rgba(0,0,0,.35)';
                const strong = document.createElement('strong');
                strong.textContent = 'Agent filled this form. Review each field top to bottom, '
                  + 'then click the RED-outlined "Submit application" button at the very '
                  + 'bottom (the blue "Apply" near the top only scrolls). ';
                d.appendChild(strong);
                const jump = document.createElement('button');
                jump.textContent = submit ? 'Jump to Submit ↓' : 'No submit button found';
                jump.style.cssText = 'margin-left:8px;font:600 12px system-ui;color:#12233b;'
                  + 'background:#fff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer';
                jump.onclick = () => { if (submit) {
                    submit.scrollIntoView({behavior: 'smooth', block: 'center'});
                    submit.focus(); } };
                d.appendChild(jump);
                if (data.needs.length) {
                    const ul = document.createElement('ul');
                    ul.style.cssText = 'margin:6px 0 0;padding-left:20px';
                    for (const item of data.needs) {
                        const li = document.createElement('li');
                        li.textContent = item;
                        ul.appendChild(li);
                    }
                    d.appendChild(ul);
                } else {
                    d.appendChild(document.createTextNode(
                        ' Nothing was flagged — still give it one pass.'));
                }
                const x = document.createElement('button');
                x.textContent = '✕';
                x.style.cssText = 'position:absolute;top:8px;right:10px;background:none;'
                  + 'border:none;color:#fff;font-size:16px;cursor:pointer';
                x.onclick = () => d.remove();
                d.appendChild(x);
                document.body.prepend(d);
                document.body.style.paddingTop = (d.offsetHeight + 4) + 'px';
            }""",
            {"needs": [n[:120] for n in needs_review[:8]]})
    except Exception as e:
        log(f"review banner could not be shown: {e}")


# Phrases that only appear once an application has actually gone through.
# (Deliberately NOT plain "thank you" — Ada's job description itself says
# "Thank you for your interest", which fooled the earlier check.)
CONFIRM_WORDS = ("thank you for applying", "thanks for applying",
                 "application has been submitted", "application has been received",
                 "application received", "we've received your application",
                 "we have received your application", "successfully submitted",
                 "application submitted")
ERROR_WORDS = ("error", "try again", "captcha", "unable to", "invalid",
               "is required", "something went wrong", "could not")

SUBMIT_LISTENER = """() => {
    if (window.__jlaHooked) return;
    window.__jlaHooked = true;
    document.addEventListener('click', e => {
        const el = e.target && e.target.closest ? e.target.closest('button, input[type=submit]') : null;
        if (el && /submit/i.test(el.innerText || el.value || '')) window.__jlaSubmitClicked = Date.now();
    }, true);
}"""


def page_signals(page) -> dict:
    """What the site is saying right now: confirmation, or error-ish lines."""
    try:
        text = page.inner_text("body")[:8000]
    except Exception:
        return {"confirmed": False, "errors": []}
    low = text.lower()
    errors = [ln.strip() for ln in text.splitlines()
              if ln.strip() and any(k in ln.lower() for k in ERROR_WORDS)][:6]
    return {"confirmed": any(k in low for k in CONFIRM_WORDS), "errors": errors}


def wait_until_user_closes(page, max_seconds: float = 4 * 3600,
                           audit_dir: Path | None = None) -> str:
    """Block until the user closes the page/browser, then say why the wait ended.

    Uses page.wait_for_timeout (a real Playwright call) rather than time.sleep so
    the driver keeps processing events — otherwise the 'close' event is never
    delivered and the script lingers for hours after the window is gone.

    With audit_dir set, it also watches for the user clicking Submit: a few
    seconds later it screenshots the page and logs what the site said (a
    confirmation, or the error text), so a failed submission is diagnosable
    without the user having to describe it. Returns '... (submitted)' once a
    confirmation was seen."""
    deadline = time.time() + max_seconds
    next_beat = time.time() + 300
    start_url = None
    click_seen_at = 0.0
    captures = 0
    submitted = False
    try:
        start_url = page.url
    except Exception:
        pass
    while time.time() < deadline:
        try:
            if page.is_closed():
                return "window closed" + (" (submitted)" if submitted else "")
            page.wait_for_timeout(1000)
        except Exception as e:
            return f"window closed ({type(e).__name__})" + (" (submitted)" if submitted else "")
        if audit_dir is not None:
            try:
                page.evaluate(SUBMIT_LISTENER)  # re-arm after any navigation
                clicked = page.evaluate("window.__jlaSubmitClicked || 0")
                navigated = start_url is not None and page.url != start_url
            except Exception:
                clicked, navigated = 0, False
            if (clicked or navigated) and not click_seen_at:
                click_seen_at = time.time()
                log("submit clicked — watching what the site does")
            if click_seen_at and time.time() - click_seen_at >= 4 and captures < 3:
                captures += 1
                sig = page_signals(page)
                try:
                    page.screenshot(path=str(audit_dir / f"after-submit-{captures}.png"),
                                    full_page=True)
                except Exception:
                    pass
                if sig["confirmed"]:
                    if not submitted:
                        log("site shows a confirmation — application went through")
                    submitted = True
                elif sig["errors"]:
                    log("site responded after submit: " + " | ".join(sig["errors"]))
                else:
                    log("no confirmation or error text visible yet after submit")
                # arm again so a retry click gets its own capture
                click_seen_at = 0.0
                try:
                    page.evaluate("window.__jlaSubmitClicked = 0")
                    start_url = page.url
                except Exception:
                    pass
        if time.time() >= next_beat:
            log("browser still open — waiting for you to review and submit")
            next_beat = time.time() + 300
    return "time limit" + (" (submitted)" if submitted else "")


def interactive_captcha_present(page) -> bool:
    """Visible interactive CAPTCHA widgets only — the invisible reCAPTCHA v3 badge
    does not require interaction and does not count."""
    try:
        return page.evaluate(
            "() => !!document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile')")
    except Exception:
        return False


def find_submit_button(page):
    try:
        btn = page.get_by_role(
            "button", name=re.compile(r"submit\s*application|submit|send\s*application",
                                      re.I)).last
        if btn.count() and btn.is_visible():
            return btn
    except Exception:
        pass
    return None


def fill_form(page, resume, cover, company, title, snippet, do_questions,
              cover_pdf=None):
    filled, needs_review = [], []

    for sel in ("a:has-text('Apply for this job')", "button:has-text('Apply for this')",
                "a:has-text('Apply now')", "button:has-text('Apply now')"):
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                el.click()
                page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    resume_in, cover_in = file_inputs_by_kind(page)
    if resume:
        try:
            (resume_in or page.locator('input[type="file"]').first).set_input_files(resume)
            filled.append("resume uploaded")
            page.wait_for_timeout(4000)
        except Exception:
            needs_review.append("resume upload (attach manually)")

    for pattern, key in FIELD_MAP:
        value = ANSWERS.get(key, "")
        if not value:
            continue
        try:
            el = page.get_by_label(re.compile(pattern, re.I)).first
            if el.count() and el.is_visible() and not el.input_value():
                el.fill(value)
                filled.append(key)
        except Exception:
            pass

    clicked_manual = False
    if cover:
        try:
            btn = page.get_by_role("button", name=re.compile(r"enter manually", re.I)).first
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(800)
                clicked_manual = True
        except Exception:
            pass
        done = False
        try:
            el = page.get_by_label(re.compile(r"cover\s*letter|additional\s*information",
                                              re.I)).first
            if el.count() and el.is_visible() and not el.input_value():
                el.fill(cover)
                filled.append("cover letter")
                done = True
        except Exception:
            pass
        if not done:
            for ta in page.locator("textarea:visible").all():
                try:
                    if ta.input_value().strip():
                        continue
                    if re.search(r"cover\s*letter", label_text(ta), re.I) or clicked_manual:
                        ta.fill(cover)
                        filled.append("cover letter")
                        done = True
                        break
                except Exception:
                    pass
        if not done and cover_pdf and cover_in is not None:
            try:
                cover_in.set_input_files(cover_pdf)
                filled.append("cover letter attached (PDF)")
                done = True
                page.wait_for_timeout(2000)
            except Exception:
                pass
        if not done:
            needs_review.append("cover letter (paste or attach from application folder)")

    choice_rules = (ANSWERS.get("yes_no_rules", []) + ANSWERS.get("select_rules", []))
    try:
        for sel_el in page.locator("select:visible").all():
            try:
                raw = label_text(sel_el)
                lbl = raw.lower()
                matched = False
                # Pronouns: bot never guesses, but types YOUR declared answer if set
                if re.search(r"pronoun", raw, re.I) and ANSWERS.get("pronouns"):
                    if select_fuzzy(sel_el, [ANSWERS["pronouns"]]):
                        filled.append(f"select: pronouns -> {ANSWERS['pronouns']}")
                        matched = True
                elif not NEVER_ANSWER.search(raw):
                    for rule in choice_rules:
                        if re.search(rule["pattern"], lbl):
                            matched = True
                            if select_fuzzy(sel_el, rule_candidates(rule)):
                                filled.append(f"select: {lbl[:50]} -> {rule['answer']}")
                            break
                if not matched and not sel_el.input_value() and is_required(sel_el, raw):
                    needs_review.append(f"LEFT FOR YOU (required): {raw.strip()[:60]}")
            except Exception:
                pass
    except Exception:
        pass

    try:
        for combo in page.get_by_role("combobox").all():
            try:
                if combo.evaluate("el => el.tagName") == "SELECT":
                    continue
                raw = label_text(combo)
                lbl = raw.lower()
                if re.search(r"pronoun", raw, re.I) and ANSWERS.get("pronouns"):
                    rules_to_try = [{"pattern": "pronoun",
                                     "answer": ANSWERS["pronouns"]}]
                elif NEVER_ANSWER.search(raw):
                    if is_required(combo, raw):
                        needs_review.append(
                            f"LEFT FOR YOU (required): {raw.strip()[:60]}")
                    continue
                else:
                    rules_to_try = choice_rules
                for rule in rules_to_try:
                    if re.search(rule["pattern"], lbl):
                        combo.click()
                        page.wait_for_timeout(500)
                        picked = False
                        for cand in rule_candidates(rule):
                            opt = page.get_by_role(
                                "option",
                                name=re.compile(rf"^{re.escape(cand)}", re.I)).first
                            if not opt.count():
                                opt = page.get_by_role(
                                    "option",
                                    name=re.compile(re.escape(cand), re.I)).first
                            if opt.count():
                                opt.click()
                                filled.append(f"dropdown: {lbl[:50]} -> {cand}")
                                picked = True
                                break
                        if not picked:
                            page.keyboard.press("Escape")
                            needs_review.append(f"dropdown I could not set: {lbl[:60]}")
                        break
            except Exception:
                pass
    except Exception:
        pass

    if do_questions:
        # Only answer REQUIRED questions — optional fields stay blank, like a
        # human leaves them. Identity/demographic fields are never answered by
        # the bot, even when required: they get flagged for the human instead.
        ai_answers, ai_cap = 0, 6

        def consider(el, q: str, short: bool) -> None:
            nonlocal ai_answers
            if NEVER_ANSWER.search(q):
                if is_required(el, q):
                    needs_review.append(f"LEFT FOR YOU (required): {q.strip()[:60]}")
                return
            if not is_required(el, q):
                return  # optional — a human would leave it blank
            log(f"drafting {'short ' if short else ''}answer: {q.strip()[:80]}...")
            ans = draft_answer(q, company, title, cover, snippet, short=short)
            if ans:
                el.fill(ans.splitlines()[0][:300] if short else ans)
                ai_answers += 1
                needs_review.append(f"AI-drafted answer — READ IT: {q.strip()[:60]}")
            else:
                needs_review.append(f"unanswered required question: {q.strip()[:60]}")

        try:
            for ta in page.locator("textarea:visible").all():
                if ai_answers >= ai_cap:
                    break
                try:
                    if ta.input_value().strip():
                        continue
                    q = label_text(ta)
                    if len(q.strip()) >= 8:
                        consider(ta, q, short=False)
                except Exception:
                    pass
        except Exception:
            pass

        known_patterns = [p for p, _ in FIELD_MAP]
        try:
            for inp in page.locator("input:visible").all():
                if ai_answers >= ai_cap:
                    break
                try:
                    if (inp.evaluate("el => el.type") or "text") not in ("text", "search"):
                        continue
                    if inp.input_value().strip():
                        continue
                    q = label_text(inp)
                    if len(q.strip()) < 12:
                        continue
                    if any(re.search(p, q, re.I) for p in known_patterns):
                        continue
                    if any(re.search(r["pattern"], q.lower())
                           for r in ANSWERS.get("yes_no_rules", [])
                           + ANSWERS.get("select_rules", [])):
                        continue
                    consider(inp, q, short=True)
                except Exception:
                    pass
        except Exception:
            pass

    return filled, needs_review


def required_fields_missing(page) -> list[str]:
    missing = []
    try:
        for el in page.locator("input:visible[required], textarea:visible[required], "
                               "select:visible[required]").all():
            try:
                if el.evaluate("el => el.type") in ("file", "hidden", "checkbox", "radio"):
                    continue
                if not el.input_value().strip():
                    missing.append(label_text(el).strip()[:60] or "(unlabelled field)")
            except Exception:
                pass
    except Exception:
        pass
    return missing


def launch_browser(p, headless: bool, human: bool):
    """Open the browser the session deserves.

    human=True (assisted/console — a person reviews and clicks Submit): the
    user's real installed Chrome (Edge as fallback) with a persistent profile
    under the agent folder, launched as a normal browser rather than in
    'automated test' mode. Cookies/logins persist between sessions and the
    'controlled by automated software' bar is gone, so the submission is the
    human's own, from an ordinary browser.

    human=False (--auto, unattended): plain Playwright Chromium, automation
    flags intact. A robot submitting stays labelled as a robot; if a site
    rejects that, the audit screenshots show it.
    Returns (context_or_browser, page) — both expose .close()."""
    viewport = {"width": 1280, "height": 1600}
    if not human:
        browser = p.chromium.launch(headless=headless)
        return browser, browser.new_page(viewport=viewport)
    common = dict(headless=headless, viewport=viewport,
                  ignore_default_args=["--enable-automation"],
                  args=["--no-first-run", "--no-default-browser-check"])
    profile, spare = ROOT / "browser-profile", ROOT / "browser-profile-2"
    attempts = [dict(user_data_dir=str(profile), channel="chrome"),
                dict(user_data_dir=str(spare), channel="chrome"),   # profile in use
                dict(user_data_dir=str(profile), channel="msedge"),
                dict(user_data_dir=str(spare))]                      # bundled fallback
    last: Exception | None = None
    for extra in attempts:
        try:
            ctx = p.chromium.launch_persistent_context(**extra, **common)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return ctx, page
        except Exception as e:
            last = e
    raise RuntimeError(f"could not launch a browser: {last}")


def apply_one(job_id: str, auto: bool, test: bool, do_questions: bool,
              console: bool = False, headless: bool = False,
              max_wait: float = 4 * 3600) -> str:
    """Returns one of: applied / check / captcha / incomplete / assisted / error."""
    con = sqlite3.connect(DB_PATH, timeout=15)
    r = con.execute(
        "SELECT company, title, url, draft_path, snippet FROM jobs WHERE id=?",
        (job_id,)).fetchone()
    if not r:
        log(f"no job with id {job_id}")
        return "error"
    company, title, url, draft_path, snippet = r
    resume, cover, cover_pdf = find_documents(draft_path)
    target = application_url(url)
    log(f"=== {title} at {company}")
    log(f"form: {target}")
    audit_dir = Path(draft_path) if draft_path and Path(draft_path).exists() \
        else ROOT / "logs"
    audit_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser, page = launch_browser(p, headless=(auto or test or headless),
                                           human=not auto)
        except Exception as e:
            log(f"browser launch failed: {e}")
            return "error"
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"page failed to load: {e}")
            browser.close()
            return "error"
        page.wait_for_timeout(3000)

        try:
            filled, needs_review = fill_form(page, resume, cover, company, title,
                                             snippet, do_questions, cover_pdf=cover_pdf)
        except Exception as e:
            # A mid-fill hiccup (e.g. the user interacting with the page) must
            # never kill the browser out from under them.
            log(f"fill interrupted ({e}) — finish the remaining fields by hand")
            filled, needs_review = [], ["fill interrupted — check every field"]
        log(f"filled: {', '.join(filled) if filled else 'nothing (unusual form?)'}")
        for item in needs_review:
            log(f"note: {item}")

        captcha = interactive_captcha_present(page)
        submit = find_submit_button(page)
        missing = required_fields_missing(page)

        if test:
            # Show exactly what the user would see, banner and highlight included
            scroll_to_form_start(page)
            inject_review_banner(page, filled, needs_review)
            shot = ROOT / "logs" / f"apply-test-{datetime.now().strftime('%H%M%S')}.png"
            page.screenshot(path=str(shot), full_page=True)
            log(f"test: captcha={'YES (interactive)' if captcha else 'no'}, "
                f"submit button={'found' if submit else 'NOT FOUND'}, "
                f"missing required={missing or 'none'}")
            log(f"test: screenshot -> {shot} (nothing submitted)")
            browser.close()
            return "test"

        if auto:
            if captcha:
                log("interactive CAPTCHA on this form — cannot auto-submit; "
                    f"run assisted instead: py -3 apply.py {job_id}")
                browser.close()
                return "captcha"
            if not submit:
                log("no submit button found — run assisted mode to finish by hand")
                browser.close()
                return "incomplete"
            if missing:
                log(f"required fields still empty ({', '.join(missing[:4])}) — "
                    "not submitting; run assisted mode")
                browser.close()
                return "incomplete"
            page.screenshot(path=str(audit_dir / "submitted-form.png"), full_page=True)
            before_url = page.url
            submit.click()
            page.wait_for_timeout(7000)
            page.screenshot(path=str(audit_dir / "submit-confirmation.png"), full_page=True)
            sig = page_signals(page)
            confirmed = sig["confirmed"] or (page.url != before_url and not sig["errors"])
            if sig["errors"] and not sig["confirmed"]:
                log("site responded after submit: " + " | ".join(sig["errors"]))
            today = datetime.now().strftime("%Y-%m-%d")
            if confirmed:
                con.execute("UPDATE jobs SET status='applied', status_date=? WHERE id=?",
                            (today, job_id))
                con.commit()
                log(f"SUBMITTED and marked applied (audit screenshots in {audit_dir})")
                browser.close()
                return "applied"
            con.execute("UPDATE jobs SET status='check', status_date=? WHERE id=?",
                        (today, job_id))
            con.commit()
            log("submitted but no confirmation detected — marked 'check'; "
                f"verify via {audit_dir / 'submit-confirmation.png'}")
            browser.close()
            return "check"

        # assisted mode launched from the console dashboard: no terminal attached.
        # Leave the filled form open; the user submits, closes the window, and
        # marks the lead applied with the dashboard's button.
        if console:
            scroll_to_form_start(page)
            inject_review_banner(page, filled, needs_review)
            log("form filled — review, click Submit on the site, then close the "
                "browser window and press '✓ Applied' on the dashboard")
            reason = wait_until_user_closes(page, max_wait, audit_dir=audit_dir)
            log(f"assisted session ended: {reason}")
            try:
                browser.close()
            except Exception:
                pass
            if "(submitted)" in reason:
                con.execute("UPDATE jobs SET status='applied', status_date=? WHERE id=?",
                            (datetime.now().strftime("%Y-%m-%d"), job_id))
                con.commit()
                log(f"marked applied automatically: {job_id}")
                return "applied"
            return "assisted"

        # assisted mode
        scroll_to_form_start(page)
        inject_review_banner(page, filled, needs_review)
        print("\n" + "=" * 70)
        print("  REVIEW EVERY FIELD, solve any CAPTCHA, then click Submit YOURSELF.")
        print("  After you have submitted, press Enter here to mark applied")
        print("  (Ctrl+C to abort without marking).")
        print("=" * 70)
        try:
            input()
            con.execute("UPDATE jobs SET status='applied', status_date=? WHERE id=?",
                        (datetime.now().strftime("%Y-%m-%d"), job_id))
            con.commit()
            log(f"marked applied: {job_id}")
            browser.close()
            return "applied"
        except (EOFError, KeyboardInterrupt):
            log("not marked; browser stays open 5 more minutes")
            page.wait_for_timeout(300000)
            browser.close()
            return "assisted"


def main() -> None:
    args = sys.argv[1:]
    max_wait = 4 * 3600.0
    if "--max-wait" in args:  # test hook: cap the stay-open wait (seconds)
        i = args.index("--max-wait")
        try:
            max_wait = float(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            pass
    test = "--test" in args
    auto = "--auto" in args or "--auto-all" in args
    console = "--console" in args
    headless = "--headless" in args
    do_questions = "--no-questions" not in args
    ids = [a for a in args if not a.startswith("--")]

    if "--auto-all" in args:
        con = sqlite3.connect(DB_PATH, timeout=15)
        ids = [r[0] for r in con.execute(
            """SELECT id FROM jobs WHERE passed_filter=1 AND status='new' AND drafted=1
               AND score>=? ORDER BY score DESC""",
            (CONFIG.get("draft_threshold", 80),)).fetchall()]
        log(f"auto-all: {len(ids)} drafted leads to apply to")

    if not ids:
        sys.exit(__doc__)

    results: dict[str, list[str]] = {}
    for job_id in ids:
        outcome = apply_one(job_id, auto=auto, test=test, do_questions=do_questions,
                            console=console, headless=headless, max_wait=max_wait)
        results.setdefault(outcome, []).append(job_id)

    if len(ids) > 1:
        log("--- summary ---")
        for outcome, group in results.items():
            log(f"{outcome}: {len(group)}")
            if outcome in ("captcha", "incomplete", "check", "error"):
                for j in group:
                    log(f"  {outcome}: {j}")


if __name__ == "__main__":
    main()
