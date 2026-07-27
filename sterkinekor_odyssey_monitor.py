"""Watch Ster-Kinekor for new The Odyssey showtimes.

Ster-Kinekor's site (FilmGrail/mars) exposes an unauthenticated JSON API behind
/api/ExecuteApiMethod. Two methods are all we need:

    QuickBuyWidget.getDates(movieId, locationId, note)      -> bookable dates
    QuickBuyWidget.getShowtimes(locationId, date, movieId, note) -> shows for a date

Ster-Kinekor releases showtimes a week at a time, so the signal for "a new date
dropped" is the last bookable date moving forward. This script snapshots every
watched cinema, diffs against the previous snapshot on disk, and reports what is
new -- flagging premium formats (IMAX, Cine Prestige, 4DX) first, since those
are the seats worth racing for.

Usage:
    python3 sterkinekor_odyssey_monitor.py                  # diff + update state
    python3 sterkinekor_odyssey_monitor.py --dry-run        # diff, leave state alone
    python3 sterkinekor_odyssey_monitor.py --all-cinemas    # every Ster-Kinekor site
    python3 sterkinekor_odyssey_monitor.py --json           # machine-readable diff

Exit codes: 0 = no change, 10 = something new, 1 = error.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://www.sterkinekor.com"
MOVIE_PAGE = "/f/odyssey-the/630"
MOVIE_ID = 630
MOVIE_TITLE = "The Odyssey"

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odyssey_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Cape Town + surrounds. Ordered by how much we care: V&A is the only site with
# both IMAX and Cine Prestige for this title.
CAPE_TOWN = {
    "V&A": 10,
    "Blue Route": 17,
    "Capegate": 7,
    "Bayside": 15,
    "N1 City": 34,
    "Somerset": 41,
    "Tygervalley": 44,
    "Garden Route": 26,
}

# A new show in one of these is the whole point of the monitor, best first.
PREMIUM = ("IMAX", "CINE PRESTIGE", "PRESTIGE", "4DX", "D-BOX")

# Cinemas whose premium screens we shout about loudest.
PRIORITY_CINEMAS = ("V&A",)


class ApiError(RuntimeError):
    pass


def _post(block, method, args, retries=4):
    """Call a mars block method. Retries with backoff on transient failures."""
    url = f"{BASE}/api/ExecuteApiMethod?blockName={block}&methodName={method}"
    body = json.dumps(
        {"blockData": {"_blockName": block, "Id": "monitor", "children": None}, "methodData": args}
    ).encode()
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": BASE,
        "Referer": BASE + MOVIE_PAGE,
    }
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise ApiError(f"{block}.{method}{args} failed after {retries} tries: {last}")


def fetch_locations():
    """Scrape the cinema list (id, name, province) off the movie page."""
    req = urllib.request.Request(BASE + MOVIE_PAGE, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "ignore")

    for blob in re.findall(r"%7B%22.{50,}?(?=\"|')", html):
        decoded = urllib.parse.unquote(blob)
        if '"HeaderClassic"' not in decoded:
            continue
        try:
            data = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        if data.get("locations"):
            return [
                {"id": loc["id"], "title": loc["title"], "province": loc.get("country", "")}
                for loc in data["locations"]
            ]
    raise ApiError("could not find the cinema list on the movie page")


def snapshot_cinema(cinema):
    """{date: {showId: 'HH:MM AM | IMAX,2D | Screen 5'}} for one cinema."""
    dates = _post("QuickBuyWidget", "getDates", [MOVIE_ID, cinema["id"], ""])
    out = {}
    for entry in dates:
        date = entry["value"]
        shows = _post("QuickBuyWidget", "getShowtimes", [cinema["id"], date, MOVIE_ID, ""])
        out[date] = {
            s["showId"]: {
                "time": s.get("startTimeTransformed", ""),
                "start": s.get("startTime", ""),
                "formats": [n for n in s.get("notes", []) if n],
                "screen": s.get("screenName", ""),
            }
            for s in shows
        }
    return out


def take_snapshot(cinemas, workers=6):
    snap = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda c: (c["title"], snapshot_cinema(c)), cinemas)
        for title, dates in results:
            snap[title] = dates
    return snap


def is_premium(formats):
    return any(f.upper() in PREMIUM for f in formats)


def premium_rank(formats):
    """Lower is better. IMAX beats Cine Prestige beats everything else."""
    return min((PREMIUM.index(f.upper()) for f in formats if f.upper() in PREMIUM),
               default=len(PREMIUM))


def premium_label(formats):
    """The formats worth naming -- '2D' is not news."""
    good = [f for f in formats if f.upper() in PREMIUM]
    return "/".join(good) if good else "/".join(formats) or "2D"


def horizon(dates):
    """Last bookable date for a cinema, or None."""
    return max(dates) if dates else None


def diff(old, new):
    """Compare two snapshots. Returns a dict describing everything that is new."""
    changes = {"new_dates": [], "new_shows": [], "gone": [], "horizon_moved": []}

    for cinema, dates in new.items():
        prev = old.get(cinema, {})
        prev_horizon = horizon(prev)
        curr_horizon = horizon(dates)
        if curr_horizon and curr_horizon != prev_horizon:
            changes["horizon_moved"].append(
                {"cinema": cinema, "was": prev_horizon, "now": curr_horizon}
            )

        for date, shows in sorted(dates.items()):
            if date not in prev:
                # A whole new bookable day -- this is the drop we're waiting for.
                changes["new_dates"].append(
                    {
                        "cinema": cinema,
                        "date": date,
                        "shows": [
                            dict(show, showId=sid, url=f"{BASE}/booking/seatmap/{sid}")
                            for sid, show in sorted(shows.items(), key=lambda kv: kv[1]["start"])
                        ],
                    }
                )
                continue
            # Known day, but extra sessions can be added later (often the good ones).
            for sid, show in sorted(shows.items(), key=lambda kv: kv[1]["start"]):
                if sid not in prev[date]:
                    changes["new_shows"].append(
                        dict(show, cinema=cinema, date=date, showId=sid,
                             url=f"{BASE}/booking/seatmap/{sid}")
                    )

        for date, shows in prev.items():
            if date not in dates:
                changes["gone"].append({"cinema": cinema, "date": date, "count": len(shows)})
            else:
                for sid in shows:
                    if sid not in dates[date]:
                        changes["gone"].append(
                            {"cinema": cinema, "date": date, "showId": sid,
                             "time": shows[sid].get("time", "")}
                        )
    return changes


def has_changes(changes):
    return any(changes[k] for k in ("new_dates", "new_shows"))


def describe(snapshot, changes, cinemas):
    """Human-readable report. First line is the headline for a push notification."""
    lines = []
    premium_hits = []

    for entry in changes["new_dates"]:
        for show in entry["shows"]:
            if is_premium(show["formats"]):
                premium_hits.append((entry["cinema"], entry["date"], show))
    for show in changes["new_shows"]:
        if is_premium(show["formats"]):
            premium_hits.append((show["cinema"], show["date"], show))

    # Best seat first: priority cinema, then format, then earliest date.
    premium_hits.sort(
        key=lambda h: (h[0] not in PRIORITY_CINEMAS, premium_rank(h[2]["formats"]), h[1],
                       h[2]["start"])
    )

    if premium_hits:
        cinema, date, show = premium_hits[0]
        new_days = sorted({e["date"] for e in changes["new_dates"]})
        lines.append(
            f"NEW {premium_label(show['formats'])} at {cinema} -- {date} {show['time']}"
            + (f" (+{len(premium_hits) - 1} more premium seats"
               f"{', dates ' + ', '.join(new_days) if new_days else ''})"
               if len(premium_hits) > 1 else "")
        )
    elif changes["new_dates"]:
        first = changes["new_dates"][0]
        lines.append(
            f"NEW date {first['date']} at {first['cinema']}"
            f" ({len(changes['new_dates'])} cinema/date combos added)"
        )
    elif changes["new_shows"]:
        lines.append(f"{len(changes['new_shows'])} new showtime(s) added")
    else:
        ends = sorted({h for h in (horizon(d) for d in snapshot.values()) if h})
        lines.append(
            f"No change. {MOVIE_TITLE} still bookable only through "
            f"{ends[-1] if ends else 'nothing'}."
        )

    if changes["horizon_moved"]:
        lines.append("")
        lines.append("Booking horizon moved:")
        for move in sorted(changes["horizon_moved"], key=lambda m: m["cinema"]):
            lines.append(f"  {move['cinema']:<14} {move['was'] or '-'} -> {move['now']}")

    if changes["new_dates"]:
        lines.append("")
        lines.append("New dates on sale:")
        for entry in sorted(changes["new_dates"], key=lambda e: (e["date"], e["cinema"])):
            lines.append(f"  {entry['cinema']} -- {entry['date']}")
            for show in entry["shows"]:
                tag = " <<<" if is_premium(show["formats"]) else ""
                fmt = "/".join(show["formats"]) or "2D"
                lines.append(
                    f"      {show['time']:>9}  {fmt:<22} {show['screen']:<10} {show['url']}{tag}"
                )
            if not any(is_premium(s["formats"]) for s in entry["shows"]):
                lines.append("      (no premium screens on this date yet)")

    if changes["new_shows"]:
        lines.append("")
        lines.append("Extra showtimes added to dates already on sale:")
        for show in sorted(changes["new_shows"], key=lambda s: (s["date"], s["cinema"], s["start"])):
            tag = " <<<" if is_premium(show["formats"]) else ""
            fmt = "/".join(show["formats"]) or "2D"
            lines.append(
                f"  {show['cinema']:<14} {show['date']} {show['time']:>9}  "
                f"{fmt:<22} {show['url']}{tag}"
            )

    if changes["gone"]:
        lines.append("")
        lines.append(f"Withdrawn: {len(changes['gone'])} showtime(s)/date(s) no longer listed.")

    lines.append("")
    lines.append("Current booking horizon:")
    for cinema in cinemas:
        title = cinema["title"]
        dates = snapshot.get(title, {})
        end = horizon(dates)
        if not end:
            lines.append(f"  {title:<14} not showing")
            continue
        premium = sorted(
            {f for day in dates.values() for s in day.values() for f in s["formats"]
             if f.upper() in PREMIUM}
        )
        lines.append(
            f"  {title:<14} through {end}  ({len(dates)} days, "
            f"{sum(len(d) for d in dates.values())} shows"
            + (f", {'/'.join(premium)}" if premium else "") + ")"
        )
    return "\n".join(lines)


def load_state(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh).get("snapshot", {})
        except json.JSONDecodeError:
            return {}


def save_state(path, snapshot):
    payload = {
        "movie": MOVIE_TITLE,
        "movieId": MOVIE_ID,
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot": snapshot,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")


def resolve_cinemas(args):
    if args.all_cinemas:
        return fetch_locations()
    known = {c["title"]: c for c in fetch_locations()}
    names = args.cinema or list(CAPE_TOWN)
    cinemas = []
    for name in names:
        if name in known:
            cinemas.append(known[name])
        elif name in CAPE_TOWN:
            cinemas.append({"id": CAPE_TOWN[name], "title": name, "province": "Western Cape"})
        else:
            print(f"warning: unknown cinema {name!r}, skipping", file=sys.stderr)
    return cinemas


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", default=STATE_PATH, help="snapshot file to diff against")
    parser.add_argument("--dry-run", action="store_true", help="do not write the state file")
    parser.add_argument("--all-cinemas", action="store_true", help="watch every Ster-Kinekor site")
    parser.add_argument("--cinema", action="append", help="cinema title, repeatable")
    parser.add_argument("--json", action="store_true", help="emit the diff as JSON")
    args = parser.parse_args(argv)

    try:
        cinemas = resolve_cinemas(args)
        if not cinemas:
            print("no cinemas to watch", file=sys.stderr)
            return 1
        snapshot = take_snapshot(cinemas)
    except (ApiError, urllib.error.URLError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    old = load_state(args.state)
    changes = diff(old, snapshot)
    first_run = not old

    if args.json:
        print(json.dumps({"firstRun": first_run, "changes": changes, "snapshot": snapshot},
                         indent=1, sort_keys=True))
    elif first_run:
        print(f"Baseline captured for {MOVIE_TITLE}.\n")
        print(describe(snapshot, {"new_dates": [], "new_shows": [], "gone": [],
                                  "horizon_moved": []}, cinemas))
    else:
        print(describe(snapshot, changes, cinemas))

    if not args.dry_run:
        save_state(args.state, snapshot)

    # First run has nothing to compare against, so it is never "a drop".
    return 10 if (has_changes(changes) and not first_run) else 0


if __name__ == "__main__":
    sys.exit(main())
