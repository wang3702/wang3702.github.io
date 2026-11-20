#!/usr/bin/env python3
"""Fetch visitor counts per country from GoatCounter and write data/visitors.json.

The homepage's world map reads this file. GoatCounter's location stats need an
API key, which can't be shipped to browsers, so the GitHub Actions workflow in
.github/workflows/visitors.yml runs this script with the key from the
GOATCOUNTER_TOKEN repository secret and commits the result. GoatCounter works
out the country when it counts a visit and doesn't store IP addresses.

The file only changes when the counts do, so quiet periods add no commits.
Standard library only, so no installs.
"""

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SITE = os.environ.get("GOATCOUNTER_SITE", "https://wang3702.goatcounter.com")
SINCE = "2026-11-19"  # first day of counting; keep in sync with VISIT_START in index.html
TOKEN = os.environ.get("GOATCOUNTER_TOKEN", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "visitors.json")
PAGE = 100


def api(path, **params):
    url = f"{SITE}/api/v0/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_countries():
    # Location stats are kept per day in the account's timezone, so a range
    # ending "now" in UTC can cut off part of today. Pad both ends by a day.
    start = (dt.date.fromisoformat(SINCE) - dt.timedelta(days=1)).isoformat() + "T00:00:00Z"
    end = (dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=2)).isoformat() + "T00:00:00Z"
    countries, offset = [], 0
    while True:
        page = api("stats/locations", start=start, end=end, limit=PAGE, offset=offset)
        for s in page.get("stats") or []:
            if s.get("count"):
                countries.append({"id": s.get("id") or "", "name": s.get("name") or "Unknown",
                                  "count": int(s["count"])})
        if not page.get("more"):
            break
        offset += PAGE
    countries.sort(key=lambda c: (-c["count"], c["name"]))
    return countries


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    if not TOKEN:
        print("::warning::GOATCOUNTER_TOKEN secret is not set; skipping the visitor map update.")
        return 0
    try:
        countries = fetch_countries()
    except urllib.error.HTTPError as exc:
        try:  # GoatCounter explains errors as {"error": "..."} or {"errors": {...}}
            body = exc.read().decode("utf-8", "replace")
            detail = json.loads(body)
            detail = detail.get("error") or detail.get("errors") or detail
        except Exception:
            detail = body[:200] if "body" in locals() else ""
        if exc.code in (401, 403):
            print(f"::error::GoatCounter rejected the API key (HTTP {exc.code}: {detail}). "
                  "Check that GOATCOUNTER_TOKEN is valid and has the 'Read statistics' permission.")
            return 1
        print(f"::error::GoatCounter API error (HTTP {exc.code}: {detail}); keeping the existing map data.")
        return 1
    except Exception as exc:  # network errors, bad JSON
        print(f"::warning::Could not reach GoatCounter ({exc}); keeping the existing map data.")
        return 0

    previous = load_previous()
    if previous and previous.get("since") == SINCE and previous.get("countries") == countries:
        print(f"No change: {sum(c['count'] for c in countries)} visitors from {len(countries)} countries.")
        return 0

    data = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "since": SINCE,
        "countries": countries,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    print(f"Wrote {sum(c['count'] for c in countries)} visitors from {len(countries)} countries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
