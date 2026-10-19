"""Probe candidate company slugs on Greenhouse/Lever/Ashby public APIs.

Keeps slugs that respond with a real job board. Run any time you add
candidates below, then paste the printed JSON into watchlist.json.
"""
from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CANDIDATES = [
    # (ats, slug, display name)
    # Batch 2: ATS retries for earlier misses + Canadian companies + remote-friendly
    ("ashby", "1password", "1Password"),
    ("lever", "agilebits", "1Password"),
    ("ashby", "janeapp", "Jane App"),
    ("ashby", "jane", "Jane App"),
    ("ashby", "clio", "Clio"),
    ("lever", "clio", "Clio"),
    ("ashby", "jobber", "Jobber"),
    ("ashby", "borrowell", "Borrowell"),
    ("ashby", "neofinancial", "Neo Financial"),
    ("ashby", "benevity", "Benevity"),
    ("ashby", "vidyard", "Vidyard"),
    ("ashby", "loopio", "Loopio"),
    ("lever", "ecobee", "ecobee"),
    ("ashby", "ecobee", "ecobee"),
    ("ashby", "certn", "Certn"),
    ("ashby", "wattpad", "Wattpad"),
    ("greenhouse", "wattpad", "Wattpad"),
    ("greenhouse", "lightspeedhq", "Lightspeed"),
    ("greenhouse", "lightspeedcommerce", "Lightspeed"),
    ("greenhouse", "coveo", "Coveo"),
    ("lever", "coveo", "Coveo"),
    ("greenhouse", "docebo", "Docebo"),
    ("lever", "docebo", "Docebo"),
    ("greenhouse", "d2l", "D2L"),
    ("lever", "d2l", "D2L"),
    ("greenhouse", "verafin", "Verafin"),
    ("greenhouse", "absorbsoftware", "Absorb Software"),
    ("greenhouse", "trulioo", "Trulioo"),
    ("lever", "trulioo", "Trulioo"),
    ("greenhouse", "visier", "Visier"),
    ("lever", "visier", "Visier"),
    ("greenhouse", "unbounce", "Unbounce"),
    ("greenhouse", "benchaccounting", "Bench"),
    ("lever", "hopper", "Hopper"),
    ("greenhouse", "hopper", "Hopper"),
    ("greenhouse", "scoremediaandgaming", "theScore"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "zapier", "Zapier"),
    ("greenhouse", "stripe", "Stripe"),
    ("greenhouse", "block", "Block"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "elastic", "Elastic"),
    ("greenhouse", "remotecom", "Remote.com"),
    ("greenhouse", "shopify", "Shopify"),
    # Batch 1 (already validated ones are re-checked harmlessly)
    ("greenhouse", "ecobee", "ecobee"),
    ("greenhouse", "faire", "Faire"),
    ("greenhouse", "tophat", "Top Hat"),
    ("greenhouse", "applyboard", "ApplyBoard"),
    ("greenhouse", "league", "League"),
    ("greenhouse", "koho", "KOHO"),
    ("greenhouse", "jobber", "Jobber"),
    ("greenhouse", "neofinancial", "Neo Financial"),
    ("greenhouse", "benevity", "Benevity"),
    ("greenhouse", "clio", "Clio"),
    ("greenhouse", "vidyard", "Vidyard"),
    ("greenhouse", "loopio", "Loopio"),
    ("greenhouse", "achievers", "Achievers"),
    ("greenhouse", "borrowell", "Borrowell"),
    ("greenhouse", "flipp", "Flipp"),
    ("greenhouse", "indexexchange", "Index Exchange"),
    ("greenhouse", "tucows", "Tucows"),
    ("greenhouse", "wealthsimple", "Wealthsimple"),
    ("greenhouse", "1password", "1Password"),
    ("greenhouse", "pagerduty", "PagerDuty"),
    ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "instacart", "Instacart"),
    ("greenhouse", "affirm", "Affirm"),
    ("greenhouse", "ritual", "Ritual"),
    ("greenhouse", "wavehq", "Wave"),
    ("greenhouse", "freshbooks", "FreshBooks"),
    ("greenhouse", "geotab", "Geotab"),
    ("greenhouse", "hootsuite", "Hootsuite"),
    ("greenhouse", "dapperlabs", "Dapper Labs"),
    ("greenhouse", "methodcrm", "Method"),
    ("greenhouse", "method", "Method"),
    ("greenhouse", "clearco", "Clearco"),
    ("greenhouse", "relayfi", "Relay"),
    ("greenhouse", "klue", "Klue"),
    ("greenhouse", "janeapp", "Jane App"),
    ("greenhouse", "thinkific", "Thinkific"),
    ("greenhouse", "later", "Later"),
    ("greenhouse", "axonify", "Axonify"),
    ("greenhouse", "touchbistro", "TouchBistro"),
    ("greenhouse", "ada", "Ada"),
    ("greenhouse", "ada18", "Ada"),
    ("greenhouse", "vena", "Vena Solutions"),
    ("greenhouse", "venasolutions", "Vena Solutions"),
    ("greenhouse", "pointclickcare", "PointClickCare"),
    ("greenhouse", "uberflip", "Uberflip"),
    ("greenhouse", "securitycompass", "Security Compass"),
    ("greenhouse", "tealbook", "TealBook"),
    ("lever", "wealthsimple", "Wealthsimple"),
    ("lever", "1password", "1Password"),
    ("lever", "klue", "Klue"),
    ("lever", "janeapp", "Jane App"),
    ("lever", "later", "Later"),
    ("lever", "thinkific", "Thinkific"),
    ("lever", "hootsuite", "Hootsuite"),
    ("lever", "dapperlabs", "Dapper Labs"),
    ("lever", "axonify", "Axonify"),
    ("lever", "plooto", "Plooto"),
    ("lever", "touchbistro", "TouchBistro"),
    ("lever", "ada", "Ada"),
    ("lever", "koho", "KOHO"),
    ("lever", "kohofinancial", "KOHO"),
    ("lever", "clearco", "Clearco"),
    ("lever", "tealbook", "TealBook"),
    ("lever", "uberflip", "Uberflip"),
    ("lever", "securitycompass", "Security Compass"),
    ("lever", "borrowell", "Borrowell"),
    ("lever", "freshbooks", "FreshBooks"),
    ("lever", "ritual", "Ritual"),
    ("lever", "flipp", "Flipp"),
    ("lever", "tucows", "Tucows"),
    ("lever", "achievers", "Achievers"),
    ("lever", "loopio", "Loopio"),
    ("lever", "vidyard", "Vidyard"),
    ("lever", "benevity", "Benevity"),
    ("lever", "jobber", "Jobber"),
    ("lever", "clio", "Clio"),
    ("lever", "neofinancial", "Neo Financial"),
    ("ashby", "cohere", "Cohere"),
    ("ashby", "shakepay", "Shakepay"),
    ("ashby", "float", "Float"),
    ("ashby", "floatfinancial", "Float"),
    ("ashby", "notion", "Notion"),
    ("ashby", "linear", "Linear"),
    ("ashby", "openphone", "OpenPhone"),
    ("ashby", "ramp", "Ramp"),
    ("ashby", "clutch", "Clutch"),
    ("ashby", "wealthsimple", "Wealthsimple"),
    ("ashby", "koho", "KOHO"),
    ("ashby", "relay", "Relay"),
    ("ashby", "deel", "Deel"),
    ("ashby", "super", "Super.com"),
    ("ashby", "supercom", "Super.com"),
    ("ashby", "properly", "Properly"),
]

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def probe(entry):
    ats, slug, name = entry
    url = URLS[ats].format(slug=slug)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "job-leads-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        if ats == "greenhouse":
            jobs = data.get("jobs", [])
        elif ats == "lever":
            jobs = data if isinstance(data, list) else []
        else:
            jobs = data.get("jobs", [])
        return {"ats": ats, "slug": slug, "name": name, "job_count": len(jobs)}
    except Exception:
        return None


def main():
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = [r for r in ex.map(probe, CANDIDATES) if r]
    # Drop duplicate companies (same name on two ATSes: keep the one with more jobs)
    best = {}
    for r in results:
        key = r["name"].lower()
        if key not in best or r["job_count"] > best[key]["job_count"]:
            best[key] = r
    keep = sorted(best.values(), key=lambda r: -r["job_count"])
    print(json.dumps(keep, indent=2))
    print(f"\n# {len(keep)} companies valid, {sum(r['job_count'] for r in keep)} total open roles", flush=True)


if __name__ == "__main__":
    main()
