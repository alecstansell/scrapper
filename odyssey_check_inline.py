"""Self-contained Odyssey drop check for scheduled runs.

A compact version of sterkinekor_odyssey_monitor.py with no state file and no
repo, small enough to paste directly into a scheduled job's prompt. That matters:
fetching a script from the internet and executing it is a download-and-run
pattern that sandbox classifiers block, so the job carries the code with it
instead of downloading any.

Edit CUTOFF to the last date already on sale. Anything later is a new release.
Exit 0 = nothing new, 10 = new dates, 1 = error.
"""

import json
import sys
import urllib.request

CUTOFF = "2026-08-06"     # last date already on sale
MOVIE, LOCATION = 630, 10  # The Odyssey, V&A Waterfront
GROUP = 2                  # seats wanted side by side
BASE = "https://www.sterkinekor.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
PREMIUM = ("IMAX", "CINE PRESTIGE")


def api(method, args):
    body = json.dumps({"blockData": {"_blockName": "QuickBuyWidget", "Id": "m",
                                     "children": None}, "methodData": args}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/ExecuteApiMethod?blockName=QuickBuyWidget&methodName={method}",
        data=body, headers={"Content-Type": "application/json;charset=UTF-8", **UA})
    return json.load(urllib.request.urlopen(req, timeout=30))


def seats_for(show_id):
    """Grade a seat plan: centre-block availability and best adjacent pair."""
    import re
    import urllib.parse
    html = urllib.request.urlopen(
        urllib.request.Request(f"{BASE}/booking/seatmap/{show_id}", headers=UA),
        timeout=40).read().decode("utf-8", "ignore")
    i = html.find("%7B%22_blockName%22%3A%22BookingSeatmap%22")
    if i < 0:
        return "seat data unavailable"
    data = json.loads(urllib.parse.unquote(re.match(r"%7B%22.*?(?=\")", html[i:]).group(0)))
    seats = [s for s in data["serverData"]["seatmap"]["seats"] if s.get("type") != "wheelchair"]
    if not seats:
        return "no seats"

    xs = [s["coordX"] for s in seats]
    cx, half = (min(xs) + max(xs)) / 2, ((max(xs) - min(xs)) / 2) or 1
    ys = sorted({s["coordY"] for s in seats})
    span = (ys[-1] - ys[0]) or 1

    def pen(s):
        return (((s["coordX"] - cx) / half / 0.45) ** 2
                + (((s["coordY"] - ys[0]) / span - 0.66) / 0.30) ** 2)

    free = [s for s in seats if s["state"] == "available"]
    if not free:
        return "SOLD OUT"
    n_prime = max(sum(1 for s in seats if pen(s) <= 1),
                  min(len(seats), max(4, round(0.15 * len(seats)))))
    prime = sorted(seats, key=pen)[:n_prime]
    prime_free = [s for s in prime if s["state"] == "available"]

    best, best_pen = None, None
    rows = {}
    for s in free:
        rows.setdefault(s["rowSymbol"], []).append(s)
    for row in rows.values():
        row.sort(key=lambda s: s["coordX"])
        run = [row[0]]
        for prev, s in zip(row, row[1:]):
            run = run + [s] if s["coordX"] - prev["coordX"] <= prev.get("width", 34) * 1.4 else [s]
            if len(run) >= GROUP:
                w = run[-GROUP:]
                sc = sum(pen(x) for x in w) / GROUP
                if best_pen is None or sc < best_pen:
                    best, best_pen = w, sc

    out = [f"{len(free)}/{len(seats)} free ({round(100 * (1 - len(free) / len(seats)))}% sold)"]
    out.append(f"{len(prime_free)}/{len(prime)} centre free" if prime_free else "centre block GONE")
    if best:
        out.append(f"{GROUP} together at {'+'.join(s['rowSymbol'] + s['columnSymbol'] for s in best)}"
                   f" ({'prime' if best_pen <= 1 else 'best available'})")
    return " | ".join(out)


def main():
    dates = [d["value"] for d in api("getDates", [MOVIE, LOCATION, ""])]
    if not dates:
        print("ERROR: no dates returned - the API or the movie id may have changed")
        return 1
    new = sorted(d for d in dates if d > CUTOFF)
    if not new:
        print(f"NO CHANGE. V&A still bookable only through {max(dates)} (cutoff {CUTOFF}).")
        return 0

    print(f"NEW DATES ON SALE: {', '.join(new)}  (was {CUTOFF})\n")
    for date in new:
        print(date)
        for s in sorted(api("getShowtimes", [LOCATION, date, MOVIE, ""]),
                        key=lambda s: s["startTime"]):
            fmt = [n for n in s["notes"] if n.upper() in PREMIUM]
            if not fmt:
                continue
            print(f"  {s['startTimeTransformed']:>9}  {'/'.join(fmt):<14} "
                  f"{BASE}/booking/seatmap/{s['showId']}")
            try:
                print(f"             seats: {seats_for(s['showId'])}")
            except Exception as exc:                      # noqa: BLE001 - best effort
                print(f"             seats: unavailable ({exc})")
    return 10


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                              # noqa: BLE001
        print(f"ERROR: monitor failed: {exc}")
        sys.exit(1)
