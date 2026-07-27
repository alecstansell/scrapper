# Ster-Kinekor Odyssey monitor

Watches **V&A Waterfront** for new *The Odyssey* dates and reports whether the
good seats are still there.

## Why it works

Ster-Kinekor's site is a FilmGrail/mars app that exposes an unauthenticated JSON
API at `POST /api/ExecuteApiMethod`. No key, no cookie, no browser needed:

```
QuickBuyWidget.getDates(movieId, locationId, note)
    -> [{"title": "Mon 27 Jul", "value": "2026-07-27"}, ...]

QuickBuyWidget.getShowtimes(locationId, date, movieId, note)
    -> [{"showId": "10-582160", "startTimeTransformed": "12:15 PM",
         "notes": ["2D", "IMAX"], "screenName": "Screen 2", ...}, ...]
```

The movie is id **630** (`/f/odyssey-the/630`), V&A is location **10**. Cinema
ids are scraped from the movie page's `HeaderClassic` block, so they stay
correct if Ster-Kinekor renumbers.

Showtimes go on sale a week at a time, so **the last bookable date moving
forward is the drop signal**.

### Seats

`GET /booking/seatmap/{showId}` server-renders the entire seat plan into the
page, each seat carrying `state: "available" | "booked"` plus row/column labels
and x/y coordinates. A plain GET therefore reveals exactly what is left:

```json
{"id": 123468, "coordX": 40, "coordY": 0, "rowSymbol": "F",
 "columnSymbol": "11", "state": "available", "type": ""}
```

This matters because V&A IMAX evening shows genuinely sell out — a 8pm show one
day ahead was down to **4 of 177 seats**, all front row.

## Usage

```bash
python3 sterkinekor_odyssey_monitor.py               # diff against odyssey_state.json, then update it
python3 sterkinekor_odyssey_monitor.py --dry-run     # diff only, leave state alone
python3 sterkinekor_odyssey_monitor.py --best-seats   # grade every premium show on sale (~36s)
python3 sterkinekor_odyssey_monitor.py --seats 10-582163   # grade one show
python3 sterkinekor_odyssey_monitor.py --group 4     # want 4 seats side by side
python3 sterkinekor_odyssey_monitor.py --cinema Sandton --cinema "Blue Route"
python3 sterkinekor_odyssey_monitor.py --all-cinemas # all 35 sites
python3 sterkinekor_odyssey_monitor.py --json        # machine-readable
```

Exit codes: `0` no change, `10` something new, `1` error. Stdlib only, no deps.
A V&A check is 12 requests and takes about 3s.

## How seats are graded

Seat quality is geometric, so it works on any auditorium without hardcoding a
layout. Each seat gets a penalty from how far it sits from the reference
position — dead centre horizontally, `IDEAL_DEPTH` (0.66) of the way back:

```
penalty = (across / WIDTH_TOLERANCE)^2 + ((back - IDEAL_DEPTH) / DEPTH_TOLERANCE)^2
```

Seats with `penalty <= 1` form the **centre block**. Because that ellipse would
collapse to a single seat in the 19-seat Cine Prestige lounge, the block is
widened to at least 15% of the room (minimum 4 seats), taking the best-scoring
seats. On V&A's 177-seat IMAX the centre block is 32 seats and its sweet spot
is around **F10–F12**.

The report then finds the best run of `--group` *adjacent* free seats — same
row, no aisle-sized gap — so "2 together at F11+F10 (prime)" means exactly that.
`(best available)` instead of `(prime)` means the centre block is already gone.

## What it reports

- **New dates** — a day that was not bookable before. The weekly drop.
- **New showtimes** — extra sessions added to a day already on sale. Premium
  screens are often added a day or two after the initial drop.
- **Withdrawn** — shows that disappeared.

Seatmaps are ~875 KB each, so they are only fetched for premium (IMAX / Cine
Prestige) shows on newly dropped dates — which is precisely when the centre
block is still open — or on demand via `--best-seats` / `--seats`.

The first run has nothing to compare against, so it records a baseline and
reports no change.

## State

`odyssey_state.json` holds the last snapshot (`{cinema: {date: {showId: ...}}}`)
plus a `checkedAt` timestamp. It must persist between runs for the diff to mean
anything — the scheduled job commits it back to this branch after each check.
A no-change run rewrites nothing, so the git history is a log of exactly when
each drop landed.
