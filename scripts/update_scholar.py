#!/usr/bin/env python3
"""Fetch Google Scholar profile stats and write data/scholar.json.

Google Scholar can't be embedded (it sends X-Frame-Options: SAMEORIGIN) and
has no CORS or public API, so the homepage reads this JSON file instead. The
GitHub Actions workflow in .github/workflows/scholar.yml runs this script once
a day and commits the result. Standard library only, so no installs.

Scholar refuses requests from GitHub's servers (HTTP 403), so after one
direct try the script reads the same profile through FORWARDER, a PHP page on
kiharalab.org that fetches Scholar from that server. The forwarder shows the
all-time numbers and the per-year chart but not the "since" column, so the
recent citation count is summed from the chart and the recent h-index and
i10-index carry over from the previous data.

A scheduled run skips the fetch while the stored data is younger than
REFRESH_DAYS. Set FORCE=true to fetch regardless (manual and push-triggered
runs do). If a fetch fails, the existing JSON is kept; once that data is older
than STALE_DAYS the run fails, so a lasting problem shows up as a failed
workflow (and GitHub's failure email) instead of a quiet warning.
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
REFRESH_DAYS = 2  # fetch once the stored data is two days old
STALE_DAYS = 3  # fail the run once a refresh has been missing this long
FORCE = os.environ.get("FORCE", "").lower() == "true"
FORWARDER = os.environ.get(
    "SCHOLAR_FORWARDER", "https://kiharalab.org/dove/xiaowang/php_scholar/pub.php"
)
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

    # Citations per year. Years with zero citations have a label but no bar.
    per_year = per_year_from_hist(
        page, r'<a [^>]*class="gsc_g_a"[^>]*z-index:(\d+)[^>]*><span class="gsc_g_al">(\d+)</span>'
    )

    return {
        "profile": URL,
        "recent_label": recent_label,
        "citations": rows["citations"],
        "h_index": rows["h-index"],
        "i10_index": rows["i10-index"],
        "per_year": per_year,
    }


def per_year_from_hist(page, bar_pattern):
    """Citations per year from Scholar's histogram markup."""
    hist = re.search(r'<div class="gsc_md_hist_b">(.*?</div>)\s*</div>', page, re.S)
    if not hist:
        raise ValueError("citations-per-year histogram not found")
    h = hist.group(1)
    years = [int(y) for y in re.findall(r'<span class="gsc_g_t"[^>]*>(\d{4})</span>', h)]
    counts = [0] * len(years)
    for z, n in re.findall(bar_pattern, h):  # z-index 1 = rightmost (latest) bar
        i = len(years) - int(z)
        if 0 <= i < len(years):
            counts[i] = int(n)
    if not years or years != sorted(years):
        raise ValueError(f"unexpected histogram years: {years}")
    return [{"year": y, "citations": c} for y, c in zip(years, counts)]


def parse_forwarder(page, previous):
    """Same fields as parse(), from the kiharalab forwarder page."""
    total = re.search(r"real-time citations:\s*<a [^>]*>(\d+)</a>", page)
    h_index = re.search(r"h-index:\s*(\d+)", page)
    i10_index = re.search(r"i10-index:\s*(\d+)", page)
    if not (total and h_index and i10_index):
        raise ValueError("forwarder page has no citation numbers (Scholar may have refused it)")
    # The forwarder swaps each bar's link for <div class="gsc_g_a" style="...">count</div>.
    per_year = per_year_from_hist(page, r'<div class="gsc_g_a" style="[^"]*z-index:(\d+)[^"]*">(\d+)</div>')

    since = utc_today().year - 5  # Scholar's recent column covers the last five years plus this one
    prev = previous or {}
    def recent(name):
        return (prev.get(name) or {}).get("recent", 0)
    return {
        "profile": URL,
        "recent_label": f"Since {since}",
        "citations": {"all": int(total.group(1)),
                      "recent": sum(p["citations"] for p in per_year if p["year"] >= since)},
        "h_index": {"all": int(h_index.group(1)), "recent": recent("h_index")},
        "i10_index": {"all": int(i10_index.group(1)), "recent": recent("i10_index")},
        "per_year": per_year,
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

    sources = [  # (name, fetch-and-parse, waits before each attempt)
        ("the Scholar page", lambda: parse(fetch(URL)), (0,)),
        ("the kiharalab forwarder", lambda: parse_forwarder(fetch(FORWARDER), previous), (0, 30, 90)),
    ]
    data, errors, source = None, [], None
    for source, get, waits in sources:
        for attempt, wait in enumerate(waits, start=1):
            time.sleep(wait)
            try:
                data = get()
                prev_total = (previous or {}).get("citations", {}).get("all", 0)
                if data["citations"]["all"] < 0.8 * prev_total:
                    raise ValueError(
                        f"citations dropped from {prev_total} to {data['citations']['all']}; "
                        "treating the response as bad"
                    )
                break
            except Exception as exc:  # network errors, 403/429s, CAPTCHA pages, parse errors
                data = None
                errors.append(f"{source}: {exc}")
                print(f"Attempt {attempt} ({source}) failed: {exc}", file=sys.stderr)
        if data is not None:
            break

    if data is None:
        error = "; ".join(dict.fromkeys(errors))  # unique messages, in order
        if age is not None and age <= STALE_DAYS:
            print(f"::warning::Could not refresh Google Scholar stats ({error}); "
                  f"keeping data from {previous['updated']} ({age} days old).")
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
    print(f"From {source}: citations {c['all']} ({data['recent_label']}: {c['recent']}), "
          f"h-index {data['h_index']['all']}, i10-index {data['i10_index']['all']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
