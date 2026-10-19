"""Scout — weekly watchlist growth and board health for the Job Leads Agent.

Finds new companies worth watching (from Remotive/Adzuna rows already in leads.db
and the latest Hacker News "Who is hiring" thread), guesses their ATS slugs, probes
the public Greenhouse/Lever/Ashby APIs, and appends validated boards to
watchlist.json. Also health-checks existing boards and removes ones that have
failed three scout runs in a row. State lives in scout_state.json.

Run: py -3 scout.py   (scheduled weekly via the JobLeadsScout task)
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
WATCHLIST_PATH = ROOT / "watchlist.json"
STATE_PATH = ROOT / "scout_state.json"
DB_PATH = ROOT / "leads.db"
MAX_NEW_COMPANIES_PER_RUN = 50

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
}
DISCOVER_ATS = ("greenhouse", "lever", "ashby")  # slug-guessing only works for these


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] scout: {msg}", flush=True)


def http_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "job-leads-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(ats: str, slug: str) -> int | None:
    """Job count if the board exists, else None."""
    try:
        data = http_json(URLS[ats].format(slug=slug))
        if ats == "greenhouse":
            return len(data.get("jobs", []))
        if ats == "lever":
            return len(data) if isinstance(data, list) else 0
        if ats == "smartrecruiters":
            return data.get("totalFound", 0)
        return len(data.get("jobs", []))
    except Exception:
        return None


def candidate_names() -> set[str]:
    names: set[str] = set()
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        for (c,) in con.execute(
                "SELECT DISTINCT company FROM jobs WHERE source IN "
                "('remotive', 'adzuna', 'google-jobs', 'linkedin-alert')"):
            if c and 2 <= len(c.strip()) <= 40:
                names.add(c.strip())
    try:
        hits = http_json("https://hn.algolia.com/api/v1/search_by_date"
                         "?tags=story,author_whoishiring&hitsPerPage=10")["hits"]
        story = next((h for h in hits if "who is hiring" in (h.get("title") or "").lower()), None)
        if story:
            item = http_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", timeout=20)
            for child in item.get("children", []):
                plain = re.sub(r"<[^>]+>", " ", child.get("text") or "")
                if not any(k in plain.lower() for k in ("canada", "toronto", "remote")):
                    continue
                first = re.sub(r"\(.*?\)", "", plain.split("|")[0]).strip()
                if 2 <= len(first) <= 30 and re.match(r"^[A-Za-z0-9&.\-' ]+$", first):
                    names.add(first)
            log(f"HN thread '{story.get('title')}' scanned")
    except Exception as e:
        log(f"HN scan failed (non-fatal): {e}")
    return names


def slug_variants(name: str) -> set[str]:
    # Full-name slugs only: a partial slug (e.g. "apex" for "Apex Dental Partners")
    # can resolve to a DIFFERENT company's board and poison the watchlist.
    low = name.lower()
    joined = re.sub(r"[^a-z0-9]+", "", low)
    dashed = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    return {v for v in (joined, dashed) if 2 <= len(v) <= 30}


def greenhouse_name_matches(slug: str, candidate: str) -> bool:
    """Greenhouse exposes the board's real company name — verify it before adding."""
    try:
        board = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}").get("name", "")
    except Exception:
        return False
    a = re.sub(r"[^a-z0-9]+", "", candidate.lower())
    b = re.sub(r"[^a-z0-9]+", "", board.lower())
    return bool(a and b) and (a in b or b in a)


def main() -> None:
    watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    state = (json.loads(STATE_PATH.read_text(encoding="utf-8"))
             if STATE_PATH.exists() else {"tried": [], "fails": {}})
    have_names = {w["name"].lower() for w in watchlist}
    have_slugs = {(w["ats"], w["slug"]) for w in watchlist}
    tried = set(state.get("tried", []))

    fresh = sorted(n for n in candidate_names()
                   if n.lower() not in have_names and n.lower() not in tried)
    fresh = fresh[:MAX_NEW_COMPANIES_PER_RUN]
    probes = [(ats, slug, name)
              for name in fresh
              for slug in slug_variants(name)
              for ats in DISCOVER_ATS
              if (ats, slug) not in have_slugs]
    log(f"{len(fresh)} candidate companies, {len(probes)} endpoint probes")

    added: dict[str, dict] = {}
    if probes:
        with ThreadPoolExecutor(max_workers=12) as ex:
            results = list(ex.map(lambda t: (t, probe(t[0], t[1])), probes))
        for (ats, slug, name), count in results:
            key = name.lower()
            if count and count > 0 and key not in added and key not in have_names:
                if ats == "greenhouse" and not greenhouse_name_matches(slug, name):
                    log(f"SKIP {name} ({ats}:{slug}) — board name does not match")
                    continue
                added[key] = {"ats": ats, "slug": slug, "name": name}
        for entry in added.values():
            log(f"ADD {entry['name']} ({entry['ats']}:{entry['slug']})")
        watchlist.extend(added.values())

    # Health check existing boards; drop after 3 consecutive failed runs
    with ThreadPoolExecutor(max_workers=12) as ex:
        health = list(ex.map(lambda w: (w, probe(w["ats"], w["slug"])), watchlist))
    fails = state.get("fails", {})
    keep = []
    for entry, count in health:
        key = f"{entry['ats']}:{entry['slug']}"
        if count is None:
            fails[key] = fails.get(key, 0) + 1
            if fails[key] >= 3:
                log(f"REMOVE {entry['name']} ({key}) — unreachable {fails[key]} runs")
                continue
        else:
            fails.pop(key, None)
        keep.append(entry)

    WATCHLIST_PATH.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")
    state["tried"] = sorted(tried | {n.lower() for n in fresh})
    state["fails"] = fails
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    log(f"done: +{len(added)} added, {len(watchlist) - len(keep)} removed, "
        f"{len(keep)} companies watched")


if __name__ == "__main__":
    main()
