"""Ranked view of a running actions file.

A flag that fires on half the list is not a flag. Measured on a real project at
79 rows: 66 open, 34 of them "silent 5+ days". That table opened every brief and
nobody could act on it.

This ranks instead of listing, and caps the output. The point is that the top of
the list should be the things a reader would actually do something about today.
"""
import csv
import datetime
import os

COLUMNS = ["id", "first_seen", "last_discussed", "due", "owner", "org",
           "action", "status", "source_ts", "notes", "blocks"]

OPEN_STATES = ("open", "in_progress")


def _date(s):
    return datetime.date(*map(int, s.split("-"))) if s else None


def load(path):
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r.setdefault("blocks", "")
    return rows


def save(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})


def score(row, asof):
    """Higher = more likely to need action today.

    Deliberately weights *blocking* and *unowned* above mere age: an item nobody
    owns cannot progress by itself, and a blocker stops other work. Age alone is
    the weakest signal -- plenty of items are legitimately quiet.
    """
    # None means "not a candidate at all" -- distinct from a low score. Using a
    # numeric sentinel here silently dropped items whose score went negative.
    if row["status"] not in OPEN_STATES:
        return None, []

    pts, why = 0, []
    if row.get("blocks"):
        pts += 100; why.append(f"blocks {row['blocks']}")
    if not row.get("owner"):
        pts += 70; why.append("UNOWNED")

    due = _date(row.get("due"))
    if due:
        days = (due - asof).days
        if days < 0:
            pts += 120; why.append(f"overdue {-days}d")
        elif days <= 7:
            pts += 80; why.append(f"due in {days}d")

    # Silence is weighted hard, and recency counts AGAINST urgency: an item
    # discussed today is in somebody's hands, whereas a blocker nobody has
    # mentioned in a fortnight is the one that quietly sinks a delivery date.
    # The "what moved" story belongs in its own section, not in this table.
    silent = (asof - _date(row["last_discussed"])).days
    if silent >= 14:
        pts += 90; why.append(f"silent {silent}d")
    elif silent >= 10:
        pts += 60; why.append(f"silent {silent}d")
    elif silent >= 5:
        pts += 25; why.append(f"silent {silent}d")
    elif silent == 0:
        pts -= 40   # discussed today; needs less attention, not more

    pts += min((asof - _date(row["first_seen"])).days, 30)   # gentle age tiebreak
    return pts, why


def ranked(rows, asof, top=8):
    scored = []
    for r in rows:
        s, why = score(r, asof)
        if s is not None:
            scored.append((s, why, r))
    scored.sort(key=lambda x: -x[0])
    return scored[:top]


def summary(rows, asof):
    from collections import Counter
    c = Counter(r["status"] for r in rows)
    openish = [r for r in rows if r["status"] in OPEN_STATES]
    return {
        "total": len(rows),
        "by_status": dict(c),
        "open": len(openish),
        "unowned": sum(1 for r in openish if not r.get("owner")),
        "blocking": sum(1 for r in openish if r.get("blocks")),
        "with_due": sum(1 for r in openish if r.get("due")),
        "silent_10": sum(1 for r in openish
                         if (asof - _date(r["last_discussed"])).days >= 10),
    }


def render_markdown(rows, asof, top=8):
    s = summary(rows, asof)
    out = [f"**{s['total']} tracked · " +
           " · ".join(f"{k} {v}" for k, v in sorted(s["by_status"].items())) + "**",
           "",
           f"{s['open']} open · {s['blocking']} blocking · {s['unowned']} unowned · "
           f"{s['with_due']} with a due date · {s['silent_10']} silent 10+ days",
           "",
           "| Item | Owner | Why it is here |",
           "|---|---|---|"]
    for _, why, r in ranked(rows, asof, top):
        owner = r.get("owner") or "**UNOWNED**"
        out.append(f"| **{r['id']}** {r['action'][:58]} | {owner} | {' · '.join(why)} |")
    shown = len(ranked(rows, asof, top))
    if s["open"] > shown:
        out += ["", f"*{s['open'] - shown} further open items not shown — "
                    f"ranked by blocking, ownership, due date, then silence.*"]
    return "\n".join(out)
