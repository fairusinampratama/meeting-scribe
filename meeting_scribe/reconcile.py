"""Compare our action list against the counterpart's own tracker.

Two independent status views of the same project disagreeing is a finding
neither produces alone. The counterpart's tracker is usually visible on screen
during a meeting -- it turns up in the keyframes -- so it can be transcribed
into a small CSV and diffed.

Matching is deliberately NOT fuzzy-by-default. "Datastore" and "Submit Data Store
access request" are the same item; "CRM Integrations" and "Ask CRM how DB
scripts live across repos" are not, and no token-overlap score separates those
two cases reliably. Links are explicit, recorded in a `counterpart` column.
Fuzzy matching is offered only as a *suggestion* for unlinked rows, clearly
labelled, so a human makes the call once and it stays made.
"""
import csv
import re

OPEN_STATES = ("open", "in_progress")

# Their status vocabulary mapped onto ours. Anything unrecognised is kept
# verbatim rather than guessed at.
STATUS_MAP = {
    "open": "open",
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "wip": "in_progress",
    "done": "closed",
    "closed": "closed",
    "completed": "closed",
}

_STOP = {"the", "a", "an", "to", "for", "of", "and", "on", "in", "with", "from",
         "how", "they", "we", "our", "their", "is", "are", "be", "it", "that",
         "this", "provide", "confirm", "check", "ask", "submit", "request"}


def load_theirs(path):
    """CSV of what the counterpart's tracker shows: item,status[,seen]."""
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        raw = (r.get("status") or "").strip().lower()
        r["status_norm"] = STATUS_MAP.get(raw, raw)
        r["item"] = (r.get("item") or "").strip()
    return rows


def _token_list(s):
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if t not in _STOP and len(t) > 2]


def _stem(t):
    """Crude plural strip. "Integrations" and "integration" are the same word
    and the deck spells the same item both ways from one slide to the next."""
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


def _keys(s):
    """Stems, plus every adjacent pair joined together.

    The join is what lets "work list" reach "Worklist" and "Data Store" reach
    "Datastore" without a synonym table. Both spellings occur in the same deck.
    """
    toks = _token_list(s)
    stems = [_stem(t) for t in toks]
    joins = [_stem(toks[i] + toks[i + 1]) for i in range(len(toks) - 1)]
    return stems, set(stems) | set(joins)


def build_idf(texts):
    """Down-weight words that are everywhere in THIS corpus.

    "Integration", "document" and "billing" appear in most items on both lists,
    so sharing one of them says nothing; "crm" or "refund" appears once and
    says almost everything. Measuring that from the two lists being compared
    beats a hand-kept stopword list, which goes stale the moment the project's
    vocabulary moves on.
    """
    import math
    from collections import Counter
    n = len(texts) or 1
    df = Counter()
    for t in texts:
        for k in _keys(t)[1]:
            df[k] += 1
    return {k: math.log(1 + n / v) for k, v in df.items()}


def _directional(stems_x, keys_y, idf):
    """How much of x, by weight, is present in y."""
    if not stems_x:
        return 0.0
    den = sum(idf.get(k, 1.0) for k in stems_x)
    if not den:
        return 0.0
    num = sum(idf.get(k, 1.0) for k in stems_x if k in keys_y)
    return num / den


def similarity(a, b, idf=None):
    """Weighted containment, not Jaccard: their items are short labels and ours
    are full sentences, so symmetric overlap scores every genuine pair near zero.

    Containment is measured in both directions and the better one wins, because
    which side is the short one varies.

    Without `idf` every word counts the same, which is only safe on a pair in
    isolation -- run that way over a real list, one shared generic word covers
    most of a short label and everything matches everything. `reconcile` always
    passes one.
    """
    idf = idf or {}
    sa, ka = _keys(a)
    sb, kb = _keys(b)
    return max(_directional(sa, kb, idf), _directional(sb, ka, idf))


def reconcile(ours, theirs, suggest_threshold=0.5):
    linked, disagree, theirs_only, ours_only, suggestions = [], [], [], [], []

    # Weights come from both lists together: a word is uninformative if it is
    # common across the project, not across English.
    idf = build_idf([t["item"] for t in theirs] + [o["action"] for o in ours])

    by_item = {r["item"].lower(): r for r in theirs}
    linked_items = set()

    for o in ours:
        # Semicolon-separated: the two lists are not 1:1. One of our rows
        # ("Arrange ERP and SSO interface discussions") routinely answers for
        # several rows on a summary deck, and forcing a single link would leave
        # the others reported as untracked when they are not.
        links = [x.strip() for x in (o.get("counterpart") or "").split(";") if x.strip()]
        for link in links:
            t = by_item.get(link.lower())
            if not t:
                ours_only.append({"ours": o, "why": f"counterpart '{link}' not on their tracker"})
                continue
            linked_items.add(t["item"].lower())
            pair = {"ours": o, "theirs": t}
            linked.append(pair)
            ours_closed = o["status"] not in OPEN_STATES
            theirs_closed = t["status_norm"] == "closed"
            if ours_closed != theirs_closed:
                disagree.append({**pair,
                                 "note": f"we say {o['status']}, they say {t['status_norm'] or '?'}"})

    for t in theirs:
        if t["item"].lower() in linked_items:
            continue
        best = max(((similarity(t["item"], o["action"], idf), o) for o in ours),
                   key=lambda x: x[0], default=(0.0, None))
        entry = {"theirs": t}
        if best[0] >= suggest_threshold and best[1] is not None:
            entry["suggest"] = {"id": best[1]["id"], "action": best[1]["action"],
                                "score": round(best[0], 2)}
            suggestions.append(entry)
        else:
            theirs_only.append(entry)

    return {"linked": linked, "disagree": disagree,
            "theirs_only": theirs_only, "ours_only": ours_only,
            "suggestions": suggestions}


def render_markdown(result):
    out = []
    n = len(result["linked"])
    out.append(f"**{n} item(s) linked · {len(result['disagree'])} disagreement(s) · "
               f"{len(result['theirs_only'])} on their tracker only · "
               f"{len(result['ours_only'])} of ours missing from theirs**")

    if result["disagree"]:
        out += ["", "### Status disagreements", "",
                "| Their item | They say | We say | Ours |", "|---|---|---|---|"]
        for d in result["disagree"]:
            out.append(f"| {d['theirs']['item']} | {d['theirs']['status_norm'] or '?'} | "
                       f"{d['ours']['status']} | {d['ours']['id']} |")

    if result["theirs_only"]:
        out += ["", "### On their tracker, not on ours", "",
                "| Their item | Status |", "|---|---|"]
        for e in result["theirs_only"]:
            out.append(f"| {e['theirs']['item']} | {e['theirs']['status_norm'] or '?'} |")

    if result["suggestions"]:
        out += ["", "### Possible matches — confirm before linking", "",
                "| Their item | Might be | Overlap |", "|---|---|---|"]
        for e in result["suggestions"]:
            s = e["suggest"]
            out.append(f"| {e['theirs']['item']} | **{s['id']}** {s['action'][:50]} | {s['score']} |")

    if result["ours_only"]:
        out += ["", "### Linked to something no longer on their tracker", "",
                "| Ours | Note |", "|---|---|"]
        for e in result["ours_only"]:
            out.append(f"| **{e['ours']['id']}** {e['ours']['action'][:50]} | {e['why']} |")

    return "\n".join(out)
