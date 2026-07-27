"""Watch Ster-Kinekor for new The Odyssey showtimes, and grade the seats.

Ster-Kinekor's site (FilmGrail/mars) exposes an unauthenticated JSON API behind
/api/ExecuteApiMethod. Two methods are all we need:

    QuickBuyWidget.getDates(movieId, locationId, note)      -> bookable dates
    QuickBuyWidget.getShowtimes(locationId, date, movieId, note) -> shows for a date

Ster-Kinekor releases showtimes a week at a time, so the signal for "a new date
dropped" is the last bookable date moving forward. This script snapshots every
watched cinema, diffs against the previous snapshot on disk, and reports what is
new -- flagging premium formats (IMAX, Cine Prestige) first.

It also grades the actual seats. GET /booking/seatmap/{showId} embeds the whole
seat plan server-side, each seat carrying a state of "available" or "booked", so a
plain HTTP GET reveals exactly what is left. Evening IMAX shows at V&A routinely
sell down to single-digit seats, so the point of catching a drop early is the
centre block -- and this reports whether it is still open.

Seatmaps are only fetched for premium shows on newly dropped dates (that is when
the answer matters and when the map is cheap to read), or on demand via --seats.

Usage:
    python3 sterkinekor_odyssey_monitor.py                  # diff + update state
    python3 sterkinekor_odyssey_monitor.py --dry-run        # diff, leave state alone
    python3 sterkinekor_odyssey_monitor.py --all-cinemas    # every Ster-Kinekor site
    python3 sterkinekor_odyssey_monitor.py --json           # machine-readable diff
    python3 sterkinekor_odyssey_monitor.py --seats 10-582163  # grade one show now
    python3 sterkinekor_odyssey_monitor.py --best-seats       # grade every premium show

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

# V&A is the only Cape Town site with both IMAX and Cine Prestige, and it is the
# one being watched. The rest are kept so --cinema can reach them by name.
WATCHED = ["V&A"]

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

# What counts as a good seat. Dead centre horizontally, about two-thirds back --
# the reference position for a big screen. A seat is "prime" when it sits inside
# the ellipse these two tolerances describe.
IDEAL_DEPTH = 0.66      # 0.0 = front row, 1.0 = back row
DEPTH_TOLERANCE = 0.30  # how far off ideal_depth a prime seat may sit
WIDTH_TOLERANCE = 0.45  # how far off centre, as a fraction of half the row

DEFAULT_GROUP = 2       # seats wanted side by side


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


def fetch_seatmap(show_id, retries=3):
    """Seat plan for a show. The whole map is server-rendered into the page."""
    url = f"{BASE}/booking/seatmap/{show_id}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=40) as resp:
                html = resp.read().decode("utf-8", "ignore")
            start = html.find("%7B%22_blockName%22%3A%22BookingSeatmap%22")
            if start < 0:
                raise ApiError(f"no seatmap block on the page for {show_id}")
            blob = re.match(r"%7B%22.*?(?=\")", html[start:])
            data = json.loads(urllib.parse.unquote(blob.group(0)))
            return data["serverData"]["seatmap"]["seats"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError,
                AttributeError, KeyError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise ApiError(f"seatmap {show_id} failed after {retries} tries: {last}")


def grade_seats(seats, group=DEFAULT_GROUP):
    """Score a seat plan and find the best run of `group` free seats together.

    Rows come back with coordY increasing away from the screen and coordX across
    the row, so the geometry works for any auditorium without hardcoding a
    layout. Returns None when the map has no real seats.
    """
    real = [s for s in seats if s.get("type") != "wheelchair"]
    if not real:
        return None

    xs = [s["coordX"] for s in real]
    centre_x = (min(xs) + max(xs)) / 2
    half = (max(xs) - min(xs)) / 2 or 1

    depths = sorted({s["coordY"] for s in real})
    span = (depths[-1] - depths[0]) or 1

    def offsets(seat):
        across = (seat["coordX"] - centre_x) / half
        back = (seat["coordY"] - depths[0]) / span
        return across, back

    def penalty(seat):
        across, back = offsets(seat)
        return ((across / WIDTH_TOLERANCE) ** 2
                + ((back - IDEAL_DEPTH) / DEPTH_TOLERANCE) ** 2)

    available = [s for s in real if s["state"] == "available"]

    # The ellipse suits a full-size auditorium, but a 19-seat Cine Prestige
    # lounge would collapse to a single "prime" seat. Keep the block at a
    # sensible fraction of the room by taking the best-scoring seats instead.
    ranked = sorted(real, key=penalty)
    strict = sum(1 for s in real if penalty(s) <= 1)
    prime_count = max(strict, min(len(real), max(4, round(0.15 * len(real)))))
    prime = ranked[:prime_count]
    prime_free = [s for s in prime if s["state"] == "available"]

    def label(seat):
        return f"{seat['rowSymbol']}{seat['columnSymbol']}"

    best_single = min(available, key=penalty) if available else None

    # Best run of `group` adjacent free seats. Adjacent = same row, next seat
    # along, with no aisle-sized gap between them.
    best_block, best_block_penalty = None, None
    by_row = {}
    for seat in available:
        by_row.setdefault(seat["rowSymbol"], []).append(seat)
    for row_seats in by_row.values():
        row_seats.sort(key=lambda s: s["coordX"])
        run = [row_seats[0]]
        for prev, seat in zip(row_seats, row_seats[1:]):
            gap = seat["coordX"] - prev["coordX"]
            if gap <= prev.get("width", 34) * 1.4:
                run.append(seat)
            else:
                run = [seat]
            if len(run) >= group:
                window = run[-group:]
                score = sum(penalty(s) for s in window) / group
                if best_block_penalty is None or score < best_block_penalty:
                    best_block, best_block_penalty = list(window), score

    return {
        "total": len(real),
        "available": len(available),
        "sold_pct": round(100 * (1 - len(available) / len(real))),
        "prime_total": len(prime),
        "prime_available": len(prime_free),
        "best_seat": label(best_single) if best_single else None,
        "best_seat_is_prime": bool(best_single) and penalty(best_single) <= 1,
        "best_block": [label(s) for s in best_block] if best_block else None,
        "best_block_is_prime": best_block_penalty is not None and best_block_penalty <= 1,
        "group": group,
    }


def seat_summary(grade):
    """One-line verdict on a graded show."""
    if not grade:
        return "no seat data"
    if grade["available"] == 0:
        return "SOLD OUT"
    bits = [f"{grade['available']}/{grade['total']} free ({grade['sold_pct']}% sold)"]
    if grade["prime_available"]:
        bits.append(f"{grade['prime_available']}/{grade['prime_total']} centre-block free")
    else:
        bits.append("centre block GONE")
    if grade["best_block"]:
        tag = "prime" if grade["best_block_is_prime"] else "best available"
        bits.append(f"{grade['group']} together at {'+'.join(grade['best_block'])} ({tag})")
    elif grade["best_seat"]:
        bits.append(f"only singles, best {grade['best_seat']}")
    return " | ".join(bits)


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


def premium_shows_in(changes):
    """Every newly-listed premium show, newest dates first."""
    found = []
    for entry in changes["new_dates"]:
        for show in entry["shows"]:
            if is_premium(show["formats"]):
                found.append(show)
    for show in changes["new_shows"]:
        if is_premium(show["formats"]):
            found.append(show)
    return found


def attach_seat_grades(shows, group=DEFAULT_GROUP, limit=30, workers=4):
    """Grade the seat plan for each show, in place. Best-effort: a seatmap that
    fails to load leaves the show ungraded rather than failing the run."""
    targets = shows[:limit]
    if not targets:
        return

    def grade(show):
        try:
            return show, grade_seats(fetch_seatmap(show["showId"]), group=group)
        except (ApiError, urllib.error.URLError, OSError):
            return show, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for show, result in pool.map(grade, targets):
            show["seats"] = result


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
        # The headline is what lands in a push notification, so say plainly
        # whether the seats worth having are actually still there.
        graded = [h for h in premium_hits if h[2].get("seats")]
        if graded:
            wide_open = [h for h in graded if h[2]["seats"]["prime_available"]
                         >= 0.8 * max(h[2]["seats"]["prime_total"], 1)]
            if wide_open:
                best = wide_open[0][2]["seats"]
                lines.append(
                    f"Centre block wide open: {best['prime_available']}/"
                    f"{best['prime_total']} prime seats free"
                    + (f", grab {'+'.join(best['best_block'])}" if best["best_block"] else "")
                )
            else:
                lines.append(f"Seats: {seat_summary(graded[0][2]['seats'])}")
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
                if show.get("seats"):
                    lines.append(f"                 seats: {seat_summary(show['seats'])}")
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
            if show.get("seats"):
                lines.append(f"                 seats: {seat_summary(show['seats'])}")

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
    names = args.cinema or WATCHED
    cinemas = []
    for name in names:
        if name in known:
            cinemas.append(known[name])
        elif name in CAPE_TOWN:
            cinemas.append({"id": CAPE_TOWN[name], "title": name, "province": "Western Cape"})
        else:
            print(f"warning: unknown cinema {name!r}, skipping", file=sys.stderr)
    return cinemas


def report_best_seats(args):
    """Grade every premium show currently on sale at the watched cinemas."""
    try:
        cinemas = resolve_cinemas(args)
        snapshot = take_snapshot(cinemas)
    except (ApiError, urllib.error.URLError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    shows = []
    for cinema, dates in snapshot.items():
        for date, day in dates.items():
            for show_id, show in day.items():
                if is_premium(show["formats"]):
                    shows.append(dict(show, cinema=cinema, date=date, showId=show_id,
                                      url=f"{BASE}/booking/seatmap/{show_id}"))
    shows.sort(key=lambda s: (s["date"], premium_rank(s["formats"]), s["start"]))
    if not shows:
        print("no premium shows on sale")
        return 0

    attach_seat_grades(shows, group=args.group, limit=len(shows))

    if args.json:
        print(json.dumps(shows, indent=1, sort_keys=True))
        return 0

    print(f"{MOVIE_TITLE} -- premium shows on sale, seats wanted together: {args.group}\n")
    current = None
    for show in shows:
        if show["date"] != current:
            current = show["date"]
            print(current)
        print(f"  {show['cinema']:<8} {show['time']:>9} "
              f"{premium_label(show['formats']):<16} {seat_summary(show.get('seats'))}")
        print(f"           {show['url']}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", default=STATE_PATH, help="snapshot file to diff against")
    parser.add_argument("--dry-run", action="store_true", help="do not write the state file")
    parser.add_argument("--all-cinemas", action="store_true", help="watch every Ster-Kinekor site")
    parser.add_argument("--cinema", action="append", help="cinema title, repeatable")
    parser.add_argument("--json", action="store_true", help="emit the diff as JSON")
    parser.add_argument("--seats", metavar="SHOWID", action="append",
                        help="grade one show's seat plan and exit, repeatable")
    parser.add_argument("--best-seats", action="store_true",
                        help="grade every premium show currently on sale and exit")
    parser.add_argument("--group", type=int, default=DEFAULT_GROUP,
                        help=f"seats wanted side by side (default {DEFAULT_GROUP})")
    args = parser.parse_args(argv)

    if args.seats:
        for show_id in args.seats:
            try:
                grade = grade_seats(fetch_seatmap(show_id), group=args.group)
            except (ApiError, urllib.error.URLError, OSError) as exc:
                print(f"{show_id}: error: {exc}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps({show_id: grade}, indent=1, sort_keys=True))
            else:
                print(f"{show_id}  {BASE}/booking/seatmap/{show_id}")
                print(f"    {seat_summary(grade)}")
        return 0

    if args.best_seats:
        return report_best_seats(args)

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

    # Seatmaps are big, so only read them when a drop actually happened -- that
    # is the moment the centre block is still open and the answer is worth having.
    if not first_run and has_changes(changes):
        attach_seat_grades(premium_shows_in(changes), group=args.group)

    if args.json:
        print(json.dumps({"firstRun": first_run, "changes": changes, "snapshot": snapshot},
                         indent=1, sort_keys=True))
    elif first_run:
        print(f"Baseline captured for {MOVIE_TITLE}.\n")
        print(describe(snapshot, {"new_dates": [], "new_shows": [], "gone": [],
                                  "horizon_moved": []}, cinemas))
    else:
        print(describe(snapshot, changes, cinemas))

    # Only rewrite state when the listings actually moved, so a no-change run
    # leaves the working tree clean and the scheduled job has nothing to commit.
    if not args.dry_run and (first_run or snapshot != old):
        save_state(args.state, snapshot)

    # First run has nothing to compare against, so it is never "a drop".
    return 10 if (has_changes(changes) and not first_run) else 0


if __name__ == "__main__":
    sys.exit(main())
