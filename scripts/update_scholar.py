#!/usr/bin/env python3
"""Fetch public Google Scholar profile stats and write data/scholar.json.

Google Scholar can't be embedded (it sends X-Frame-Options: SAMEORIGIN) and
has no CORS or public API, so the homepage reads this JSON file instead. The
GitHub Actions workflow in .github/workflows/scholar.yml runs this script on a
schedule and commits the result. Standard library only, so no installs.

Scholar blocks some GitHub runner IPs (HTTP 403) but not others, so the
workflow runs daily: a scheduled run skips the fetch while the stored data is
younger than REFRESH_DAYS, and otherwise retries each day until one succeeds.
Set FORCE=true to fetch regardless (manual and push-triggered runs do).

If Scholar can't be reached or parsed, the existing JSON is kept. The run only
fails once that data is older than STALE_DAYS, so a one-off block stays quiet
but a lasting one shows up as a failed workflow.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

USER = os.environ.get("SCHOLAR_USER", "AGS_dK8AAAAJ")
URL = f"https://scholar.google.com/citations?user={USER}&hl=en"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "scholar.json")
REFRESH_DAYS = 7
STALE_DAYS = 21
FORCE = os.environ.get("FORCE", "").lower() == "true"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def fetch(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def parse(page):
    table = re.search(r'<table id="gsc_rsb_st">(.*?)</table>', page, re.S)
    if not table:
        raise ValueError("citation table not found (blocked, CAPTCHA, or layout change)")
    t = table.group(1)

    headers = re.findall(r'<th class="gsc_rsb_sth">([^<]*)</th>', t)
    recent_label = headers[2].strip() if len(headers) > 2 else "Recent"

    rows = {}
    for label, all_time, recent in re.findall(
        r'class="gsc_rsb_f[^"]*"[^>]*>([^<]+)</a></td>'
        r'<td class="gsc_rsb_std">(\d+)</td><td class="gsc_rsb_std">(\d+)</td>',
        t,
    ):
        rows[label.strip().lower()] = {"all": int(all_time), "recent": int(recent)}
    missing = {"citations", "h-index", "i10-index"} - rows.keys()
    if missing:
        raise ValueError(f"missing rows in citation table: {sorted(missing)}")

    # Citations per year. Years with zero citations have a label but no bar;
    # each bar's z-index is its position counted from the right (1 = latest).
    hist = re.search(r'<div class="gsc_md_hist_b">(.*?)</div>', page, re.S)
    if not hist:
        raise ValueError("citations-per-year histogram not found")
    h = hist.group(1)
    years = [int(y) for y in re.findall(r'<span class="gsc_g_t"[^>]*>(\d{4})</span>', h)]
    counts = [0] * len(years)
    for z, n in re.findall(
        r'<a [^>]*class="gsc_g_a"[^>]*z-index:(\d+)[^>]*><span class="gsc_g_al">(\d+)</span>', h
    ):
        i = len(years) - int(z)
        if 0 <= i < len(years):
            counts[i] = int(n)
    if not years or years != sorted(years):
        raise ValueError(f"unexpected histogram years: {years}")

    return {
        "profile": URL,
        "recent_label": recent_label,
        "citations": rows["citations"],
        "h_index": rows["h-index"],
        "i10_index": rows["i10-index"],
        "per_year": [{"year": y, "citations": c} for y, c in zip(years, counts)],
    }


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def utc_today():
    return dt.datetime.now(dt.timezone.utc).date()


def age_in_days(data):
    updated = (data or {}).get("updated")
    return (utc_today() - dt.date.fromisoformat(updated)).days if updated else None


def main():
    previous = load_previous()
    age = age_in_days(previous)
    if not FORCE and age is not None and age < REFRESH_DAYS:
        print(f"Stored data is {age} day(s) old; next refresh due at {REFRESH_DAYS} days. Skipping.")
        return 0

    data, error = None, None
    for attempt, wait in enumerate((0, 20, 60), start=1):
        time.sleep(wait)
        try:
            data = parse(fetch(URL))
            prev_total = (previous or {}).get("citations", {}).get("all", 0)
            if data["citations"]["all"] < 0.8 * prev_total:
                raise ValueError(
                    f"citations dropped from {prev_total} to {data['citations']['all']}; "
                    "treating the page as bad"
                )
            break
        except Exception as exc:  # network errors, 429s, CAPTCHA pages, parse errors
            error = exc
            data = None
            print(f"Attempt {attempt} failed: {exc}", file=sys.stderr)

    if data is None:
        if age is not None and age <= STALE_DAYS:
            print(f"::warning::Could not refresh Google Scholar stats ({error}); "
                  f"keeping data from {previous['updated']} ({age} days old). Will retry next run.")
            return 0
        print(f"::error::Could not refresh Google Scholar stats ({error}); "
              f"stored data is {'missing' if age is None else f'{age} days old'}.")
        return 1

    data = {"updated": utc_today().isoformat(), **data}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    c = data["citations"]
    print(f"Citations {c['all']} ({data['recent_label']}: {c['recent']}), "
          f"h-index {data['h_index']['all']}, i10-index {data['i10_index']['all']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
