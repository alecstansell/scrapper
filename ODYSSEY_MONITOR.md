# Ster-Kinekor Odyssey monitor

Watches Ster-Kinekor for new *The Odyssey* showtimes so a new date drop can be
caught while the good seats are still there.

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

The movie is id **630** (`/f/odyssey-the/630`). Cinema ids come off the movie
page's `HeaderClassic` block, so they stay correct if Ster-Kinekor renumbers.

Showtimes go on sale a week at a time, so **the last bookable date moving
forward is the drop signal**. Booking link for any show is
`https://www.sterkinekor.com/booking/seatmap/{showId}`.

## Usage

```bash
python3 sterkinekor_odyssey_monitor.py               # diff against odyssey_state.json, then update it
python3 sterkinekor_odyssey_monitor.py --dry-run     # diff only, leave state alone
python3 sterkinekor_odyssey_monitor.py --all-cinemas # all 35 sites instead of Cape Town
python3 sterkinekor_odyssey_monitor.py --cinema Sandton --cinema Fourways
python3 sterkinekor_odyssey_monitor.py --json        # machine-readable diff
```

Exit codes: `0` no change, `10` something new, `1` error. Stdlib only, no deps.
A Cape Town run is ~70 requests and takes about 12s.

## What it watches

Cape Town and surrounds, premium screens flagged with `<<<`:

| Cinema | Premium screens for Odyssey |
| --- | --- |
| **V&A** | IMAX + Cine Prestige |
| Blue Route | IMAX |
| Capegate | IMAX |
| Bayside, N1 City, Somerset, Garden Route | 2D only |
| Tygervalley | not showing this title |

`PRIORITY_CINEMAS` in the script decides which cinema leads the headline line
(currently V&A). Within that, IMAX outranks Cine Prestige.

It reports three kinds of change:

- **New dates** — a day that was not bookable before. The weekly drop.
- **New showtimes** — extra sessions added to a day already on sale. Premium
  screens are often added a day or two after the initial drop.
- **Withdrawn** — shows that disappeared, so a pulled session is visible too.

The first run has nothing to compare against, so it records a baseline and
reports no change.

## State

`odyssey_state.json` holds the last snapshot (`{cinema: {date: {showId: ...}}}`)
plus a `checkedAt` timestamp. It must persist between runs for the diff to mean
anything — the scheduled job commits it back to this branch after each check.
