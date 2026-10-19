"""Regression test for apply.py's form-filling behavior.

Runs the REAL fill logic against a local fake form (test_fixtures/fake_form.html)
with the AI answer function stubbed out — no network, no job site, nothing
submitted. Asserts the human-likeness rules that real use has already taught us:

  - required fields get filled, optional ones stay blank
  - identity fields (pronouns, pronunciation) are NEVER answered, even required
  - eligibility/sponsorship/province/how-heard dropdowns get correct values
  - required free-text questions get an answer; optional ones stay empty

Run: py -3 test_apply_fill.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apply as apply_mod  # noqa: E402

apply_mod.draft_answer = lambda *a, **k: "CANNED AI ANSWER"

FIXTURE = (Path(__file__).resolve().parent / "test_fixtures" / "fake_form.html").as_uri()

checks: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    checks.append((name, ok))
    print(("PASS  " if ok else "FAIL  ") + name)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FIXTURE)
        filled, needs = apply_mod.fill_form(
            page, resume=None, cover="Test cover letter.",
            company="Fake Co", title="Software Engineer",
            snippet="A fake job.", do_questions=True)

        val = lambda sel: page.locator(sel).input_value()  # noqa: E731
        check("first name filled", val("#fn") == "Matthew")
        check("email filled", "@" in val("#em"))
        check("cover letter filled", val("#cl") == "Test cover letter.")
        check("eligibility -> Yes", val("#elig") == "Yes")
        check("sponsorship -> No", val("#spon") == "No")
        check("province -> Ontario", val("#prov") == "Ontario")
        check("how-heard via fallback -> Company Website",
              val("#hear") == "Company Website")
        check("PRONOUNS left untouched", val("#pron") == "")
        check("pronouns flagged for human",
              any("pronouns" in n.lower() for n in needs))
        check("name pronunciation left blank", val("#pronounce") == "")
        check("other-links left blank", val("#other") == "")
        check("required question answered", val("#why") == "CANNED AI ANSWER")
        check("optional question left blank", val("#extra") == "")

        apply_mod.scroll_to_form_start(page)
        apply_mod.inject_review_banner(page, filled, needs)
        check("review banner injected",
              page.locator("#jla-review").count() == 1)
        check("banner offers Jump to Submit",
              "Jump to Submit" in page.locator("#jla-review").inner_text())
        outline = (page.locator("button:has-text('Submit application')")
                   .evaluate("b => b.style.outline") or "")
        check("real submit button is red-outlined",
              "3px" in outline and "208, 59, 59" in outline)  # rgb(208,59,59) = #d03b3b

        # The stay-open wait must (a) not exit on its own, and (b) notice when the
        # window actually closes — the event has to be delivered mid-wait.
        import time
        hold = browser.new_page()
        hold.goto(FIXTURE)
        t0 = time.time()
        reason = apply_mod.wait_until_user_closes(hold, max_seconds=2)
        check("wait holds open until cap (no phantom exit)",
              reason == "time limit" and time.time() - t0 >= 1.8)
        # Chromium only lets script-opened windows close themselves, so simulate
        # "the user closed the window" with a popup that closes itself mid-wait.
        with hold.expect_popup() as popup_info:
            hold.evaluate("window.open('about:blank')")
        popup = popup_info.value
        popup.evaluate("setTimeout(() => window.close(), 1500)")
        t0 = time.time()
        reason = apply_mod.wait_until_user_closes(popup, max_seconds=10)
        check("wait notices the window closing (event delivered mid-wait)",
              reason.startswith("window closed") and time.time() - t0 < 8)

        # Submit-watch: a click on Submit mid-wait must be captured (screenshot +
        # page signals) and a confirmation must be recognised as 'submitted'.
        import tempfile
        audit = Path(tempfile.mkdtemp(prefix="jla-audit-"))
        page.evaluate("setTimeout(() => document.querySelector("
                      "'button:not(#jla-review button)').click(), 1500)")
        reason = apply_mod.wait_until_user_closes(page, max_seconds=9, audit_dir=audit)
        check("submit click captured with screenshot",
              (audit / "after-submit-1.png").exists())
        check("confirmation recognised as submitted", "(submitted)" in reason)
        check("job page's own 'thank you for your interest' is NOT a confirmation",
              not any(k in "thank you for your interest in joining us"
                      for k in apply_mod.CONFIRM_WORDS))
        browser.close()

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} passed"
          + (f" — FAILURES: {failed}" if failed else " — all good"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
