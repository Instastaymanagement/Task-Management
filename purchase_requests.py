#!/usr/bin/env python3
"""Every 5 minutes: copy cleaners' purchase and supply requests from the General Form to the
"Purchase Requests" board (Instastay → 📋 Task Hub).

A Field Team Submissions (General Form) item becomes a purchase request when it has any of:
  - "Purchase Made?" = Yes (the cleaner bought something and needs to be paid back),
  - "Supplies Needed?" with anything other than "NO RESTOCK NEEDED",
  - something in "Beddings Needed?" other than "No" / "None" / "N/A",
  - Form Type "Urgent Maintenance / Supplies Requests".

Each submission gives one subitem per item needed or bought. Submissions for a property that already
has an open request in New Requests are merged into it (one list per property), so each property has
at most one supply request and one purchase request waiting there. Once a request is moved out of
New Requests, the next submission for that property starts a new one. The request's "Form Item ID" column holds the
submission's item id, so a submission is never copied twice. Only submissions from the last
PURCHASE_LOOKBACK_DAYS days are looked at.

Environment:
  MONDAY_API_TOKEN         monday.com API token (required)
  DRY_RUN                  "true" to list the requests that would be added without adding them
  PURCHASE_LOOKBACK_DAYS   how many days of submissions to check (default 7)
"""

import datetime as dt
import json
import os
import re

from slack_tasks import LOCAL_TZ, POSITION_API_VERSION, log, monday

TARGET_BOARD = 18439221614  # Purchase Requests
NEW_GROUP = "topics"        # New Requests

# Purchase Requests columns
T_TYPE = "color_mm8zn7rf"
T_STATUS = "color_mm8zcyz0"
T_COUNTRY = "color_mm8zsvsh"
T_PROPERTY = "text_mm8zvm98"
T_BY = "text_mm8zznxw"
T_DATE = "date_mm8zfc06"
T_NOTES = "long_text_mm8zbne"   # receipts and the cleaner's notes
T_AMOUNT = "numeric_mm8z3m74"
T_PRIORITY = "color_mm8z6nce"
T_SUBMISSION = "link_mm8zjts6"
T_PROPERTY_LINK = "board_relation_mm8z5e62"
T_SOURCE = "text_mm8z4dqq"
SUB_STATUS = "color_mm8zdcgd"   # Status on the subitems board: Needed / Ordered / Done / Not needed
SUB_NEW_STATUS = "Needed"
SUB_DATE = "date0"              # Date on the subitems board: the day the item was asked for
SUB_DONE = {"done", "not needed"}

# Field Team Submissions boards. Both have the same form columns; dates and property links differ.
SOURCES = [
    {"board": 18435476614, "country": "Canada", "properties_board": 18435476437,
     "date_cols": ["date2j8vd08t", "date_mm8wh5kz"], "relation": "board_relation_mm84399d"},
    {"board": 18435502755, "country": "USA", "properties_board": 18435507823,
     "date_cols": ["datee9zspnrl"], "relation": "board_relation_mm843f79"},
]
S_PROPERTY = "single_selectp2c2cc7"
S_FORM_TYPE = "single_selectse0kb4z"
S_SUPPLIES = "multi_selectamye5ja2"
S_BEDDING = "short_textffptgktf"
S_URGENT = "long_textwy74n2rh"
S_PURCHASED = "single_selectd8mip9p"
S_ITEMS_PURCHASED = "short_textetxu7mcj"
S_RECEIPTS = "filen58bgisv"
S_AMOUNT = "numbervp12wa4c"
S_PRIORITY = "single_selectrywl034"
S_NOTES = "short_textm9jrpbyj"
S_COLUMNS = [S_PROPERTY, S_FORM_TYPE, S_SUPPLIES, S_BEDDING, S_URGENT, S_PURCHASED, S_ITEMS_PURCHASED,
             S_RECEIPTS, S_AMOUNT, S_PRIORITY, S_NOTES]

NO_RESTOCK = "no restock needed"
NOTHING_RE = re.compile(r"^(no|nk|nah|nope|none|nothing|nil|n/?a|-+|0|not needed|no need(ed)?|all good|good|ok(ay)?)"
                        r"[\s.!]*$", re.I)
URGENT_FORM = "urgent maintenance / supplies"
PRIORITIES = {"Low", "Medium", "High"}
ITEM_SPLIT_RE = re.compile(r"\s*(?:[,;\n]+\s*(?:and\s+|&\s*)?|\s+and\s+|\s+&\s+)\s*", re.I)

TYPE_PURCHASE = "Purchase made"
TYPE_URGENT = "Urgent request"
TYPE_SUPPLIES = "Supplies needed"

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
LOOKBACK_DAYS = float(os.environ.get("PURCHASE_LOOKBACK_DAYS") or 7)
# Submissions for work before this date are never copied (the board starts with November 2026).
SINCE_DATE = os.environ.get("PURCHASE_SINCE") or "2026-11-01"
NAME_LIMIT = 90


def meaningful(text):
    text = (text or "").strip()
    return bool(text) and not NOTHING_RE.match(text)


def supplies(text):
    return [s.strip() for s in (text or "").split(",") if s.strip() and s.strip().lower() != NO_RESTOCK]


def split_items(text):
    """One entry per thing in a free-text list: "Toilet paper, paper towel, and coffee" → 3 items."""
    out = []
    for part in ITEM_SPLIT_RE.split(text or ""):
        part = part.strip(" -•*.").strip()
        if part and meaningful(part) and part.lower() not in (p.lower() for p in out):
            out.append(part[:1].upper() + part[1:])
    return out


def parse_request(sub):
    """The purchase request a submission makes, or None. `sub` maps column ids to their text."""
    needed = supplies(sub.get(S_SUPPLIES))
    bedding = (sub.get(S_BEDDING) or "").strip() if meaningful(sub.get(S_BEDDING)) else ""
    urgent_form = URGENT_FORM in (sub.get(S_FORM_TYPE) or "").lower()
    urgent = (sub.get(S_URGENT) or "").strip()
    purchased = (sub.get(S_PURCHASED) or "").strip().lower() == "yes"
    bought = (sub.get(S_ITEMS_PURCHASED) or "").strip()

    if purchased:
        kind = TYPE_PURCHASE
    elif urgent_form and (urgent or needed or bedding):
        kind = TYPE_URGENT
    elif needed or bedding or (urgent and meaningful(urgent)):
        kind = TYPE_SUPPLIES
    else:
        return None

    lines = []  # the items themselves go in subitems; Notes keeps receipts and the cleaner's notes
    receipts = (sub.get(S_RECEIPTS) or "").strip()
    if receipts:
        lines.append(f"Receipts: {receipts}")
    notes = (sub.get(S_NOTES) or "").strip()
    if meaningful(notes):
        lines.append(f"Cleaner's notes: {notes}")

    if kind == TYPE_PURCHASE:
        things = split_items(bought) or ["Items not listed (see receipts)"]
    else:
        things = needed + [f"Bedding: {b}" for b in split_items(bedding)] + split_items(urgent)

    summary = bought or ", ".join(needed + ([f"bedding ({bedding})"] if bedding else [])) or urgent
    summary = " ".join(summary.split())
    amount = None
    try:
        amount = float(sub.get(S_AMOUNT)) if sub.get(S_AMOUNT) not in (None, "") else None
    except ValueError:
        pass
    return {"type": kind, "summary": summary, "details": "\n".join(lines), "amount": amount,
            "items": things}


def item_name(prop, requester=""):
    """The request's name is just the property; the items are its subitems."""
    prop = (prop or "").strip()
    if not prop or prop.lower().startswith("other"):
        return f"Other property ({requester})" if requester else "Other property"
    return prop[:NAME_LIMIT]


# --------------------------------------------------------------------- properties

def property_match(label, masters):
    """Properties Master item id for a form label like "Evans 1456" or "Spruce Main 306", or None."""
    label = (label or "").lower()
    numbers = re.findall(r"\d+", label)
    words = [w for w in re.findall(r"[a-z]+", label) if len(w) > 2 and w not in ("unit", "the")]
    if not numbers or not words:
        return None
    hits = [m for m in masters
            if numbers[0] in re.findall(r"\d+", m["name"].lower()) and words[0] in m["name"].lower()]
    if len(hits) > 1:  # e.g. Spruce Main / Spruce Upstairs: use every word to narrow it down
        hits = [m for m in hits if all(w in m["name"].lower() for w in words)]
    return int(hits[0]["id"]) if len(hits) == 1 else None


def master_properties(board_id, cache={}):
    if board_id not in cache:
        page = monday("query($id:[ID!]){boards(ids:$id){items_page(limit:500){items{id name}}}}",
                      {"id": [board_id]})["boards"][0]["items_page"]
        cache[board_id] = page["items"]
    return cache[board_id]


# ------------------------------------------------------------------------ monday

def existing_sources():
    """Form item ids already on the Purchase Requests board."""
    fields = f'items{{column_values(ids:["{T_SOURCE}"]){{text}}}}'
    page = monday(f"query($id:[ID!]){{boards(ids:$id){{items_page(limit:500){{cursor {fields}}}}}}}",
                  {"id": [TARGET_BOARD]})["boards"][0]["items_page"]
    items = list(page["items"])
    while page.get("cursor"):
        page = monday(f"query($c:String!){{next_items_page(limit:500, cursor:$c){{cursor {fields}}}}}",
                      {"c": page["cursor"]})["next_items_page"]
        items.extend(page["items"])
    return {s.strip() for it in items for c in it["column_values"] for s in (c["text"] or "").split(",")} - {""}


def recent_submissions(source, since):
    cols = S_COLUMNS + source["date_cols"] + [source["relation"]]
    fields = """cursor items{id name url created_at
        column_values(ids:$cols){id text ... on BoardRelationValue{linked_item_ids}}}"""
    query = f"""query($id:[ID!], $cols:[String!]){{boards(ids:$id){{items_page(limit:100,
        query_params:{{order_by:[{{column_id:"__creation_log__", direction:desc}}]}}){{{fields}}}}}}}"""
    page = monday(query, {"id": [source["board"]], "cols": cols})["boards"][0]["items_page"]
    out = []
    while True:
        for it in page["items"]:
            created = dt.datetime.fromisoformat(it["created_at"].replace("Z", "+00:00"))
            if created < since:
                return out
            out.append(it)
        if not page.get("cursor"):
            return out
        page = monday(f"""query($c:String!, $cols:[String!]){{next_items_page(limit:100, cursor:$c){{{fields}}}}}""",
                      {"c": page["cursor"], "cols": cols})["next_items_page"]


def submission_values(item):
    values = {}
    for c in item["column_values"]:
        values[c["id"]] = c.get("text")
        if "linked_item_ids" in c:
            values[c["id"]] = [int(i) for i in c.get("linked_item_ids") or []]
    return values


def submission_day(source, item, sub):
    """The date the cleaner gave in the form, or the day it was submitted (Toronto time)."""
    created = dt.datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
    return next((sub[c] for c in source["date_cols"] if sub.get(c)), None) or \
        created.astimezone(LOCAL_TZ).date().isoformat()


def notes_block(day, who, details):
    return "\n".join(f"{day} · {who}: {line}" for line in details.splitlines() if line.strip())


def build_values(source, item, sub, req):
    day = submission_day(source, item, sub)
    prop = (sub.get(S_PROPERTY) or "").strip()
    links = sub.get(source["relation"]) or []
    if not links and prop:
        match = property_match(prop, master_properties(source["properties_board"]))
        links = [match] if match else []
    values = {
        T_TYPE: {"label": req["type"]},
        T_STATUS: {"label": "New"},
        T_COUNTRY: {"label": source["country"]},
        T_PROPERTY: prop,
        T_BY: item["name"].strip(),
        T_DATE: {"date": day},
        T_NOTES: {"text": notes_block(day, item["name"].strip(), req["details"])},
        T_SUBMISSION: {"url": item["url"], "text": "Form submission"},
        T_SOURCE: str(item["id"]),
    }
    if req["amount"] is not None:
        values[T_AMOUNT] = str(req["amount"])
    if sub.get(S_PRIORITY) in PRIORITIES:
        values[T_PRIORITY] = {"label": sub[S_PRIORITY]}
    if links:
        values[T_PROPERTY_LINK] = {"item_ids": links}
    return values


def merge_key(country, prop, kind):
    """Requests with the same key share one list: supplies and urgent requests together, purchases
    (which are paid back to the cleaner) on their own."""
    return (country, " ".join((prop or "").lower().split()), "purchase" if kind == TYPE_PURCHASE else "supplies")


def open_requests():
    """The requests in New Requests, keyed by merge_key, with their columns and subitems."""
    cols = [T_TYPE, T_COUNTRY, T_PROPERTY, T_BY, T_DATE, T_NOTES, T_AMOUNT, T_PRIORITY, T_SOURCE]
    data = monday("""query($id:[ID!], $group:[String], $cols:[String!]){boards(ids:$id){groups(ids:$group){
        items_page(limit:500){items{id name column_values(ids:$cols){id text}
        subitems{name column_values(ids:["%s"]){text}}}}}}}""" % SUB_STATUS,
                  {"id": [TARGET_BOARD], "group": [NEW_GROUP], "cols": cols})
    groups = data["boards"][0]["groups"]
    out = {}
    for it in (groups[0]["items_page"]["items"] if groups else []):
        values = {c["id"]: c["text"] or "" for c in it["column_values"]}
        if not values.get(T_PROPERTY):
            continue  # nothing to match on
        req = {"id": it["id"], "values": values,
               "open_items": {" ".join(si["name"].lower().split()) for si in it.get("subitems") or []
                              if (si["column_values"][0]["text"] or "").lower() not in SUB_DONE}}
        out.setdefault(merge_key(values[T_COUNTRY], values[T_PROPERTY], values[T_TYPE]), req)
    return out


def merge_values(existing, values, req):
    """Column changes that add a new submission (`values`, from build_values) to an open request."""
    old = existing["values"]
    sources = [s.strip() for s in old.get(T_SOURCE, "").split(",") if s.strip()]
    people = [p.strip() for p in old.get(T_BY, "").split(",") if p.strip()]
    who = values[T_BY]
    out = {
        T_SOURCE: ", ".join(sources + [values[T_SOURCE]]),
        T_BY: ", ".join(people + ([who] if who.lower() not in (p.lower() for p in people) else [])),
        T_DATE: {"date": max(old.get(T_DATE) or "", values[T_DATE]["date"])},
        T_SUBMISSION: values[T_SUBMISSION],
    }
    new_notes = values[T_NOTES]["text"]
    if new_notes:
        out[T_NOTES] = {"text": "\n".join(n for n in (old.get(T_NOTES, "").strip(), new_notes) if n)}
    if old.get(T_TYPE) != TYPE_URGENT and req["type"] == TYPE_URGENT:
        out[T_TYPE] = {"label": TYPE_URGENT}
    if req["amount"] is not None:
        try:
            total = float(old.get(T_AMOUNT) or 0) + req["amount"]
        except ValueError:
            total = req["amount"]
        out[T_AMOUNT] = str(round(total, 2))
    rank = ["", "Low", "Medium", "High"]
    new_priority = (values.get(T_PRIORITY) or {}).get("label", "")
    if rank.index(new_priority) > rank.index(old.get(T_PRIORITY, "") if old.get(T_PRIORITY) in rank else ""):
        out[T_PRIORITY] = {"label": new_priority}
    return out


def new_items(existing, names):
    """The names not already open on the request (asking for sugar twice gives one Sugar)."""
    seen = set(existing["open_items"]) if existing else set()
    out = []
    for name in names:
        key = " ".join(name.lower().split())
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def top_item(group_id=NEW_GROUP):
    """The item at the top of the group, or None if it's empty."""
    data = monday("""query($id:[ID!], $group:[String]){boards(ids:$id){groups(ids:$group){
        items_page(limit:1){items{id}}}}}""", {"id": [TARGET_BOARD], "group": [group_id]})
    groups = data["boards"][0]["groups"]
    items = groups[0]["items_page"]["items"] if groups else []
    return items[0]["id"] if items else None


def create_request(name, values):
    """Create the request at the top of New Requests, so the newest is always first."""
    variables = {"board": TARGET_BOARD, "group": NEW_GROUP, "name": name, "values": json.dumps(values)}
    first = top_item()
    if first:
        data = monday("""mutation($board:ID!, $group:String!, $name:String!, $values:JSON!, $first:ID!){
            create_item(board_id:$board, group_id:$group, item_name:$name, column_values:$values,
                        create_labels_if_missing:false, relative_to:$first, position_relative_method:before_at){id}}""",
                      {**variables, "first": first}, api_version=POSITION_API_VERSION)
    else:
        data = monday("""mutation($board:ID!, $group:String!, $name:String!, $values:JSON!){
            create_item(board_id:$board, group_id:$group, item_name:$name, column_values:$values,
                        create_labels_if_missing:false){id}}""", variables)
    return data["create_item"]["id"]


def update_request(item_id, values):
    monday("""mutation($board:ID!, $item:ID!, $values:JSON!){change_multiple_column_values(board_id:$board,
        item_id:$item, column_values:$values, create_labels_if_missing:false){id}}""",
           {"board": TARGET_BOARD, "item": item_id, "values": json.dumps(values)})


def move_to_top(item_id):
    first = top_item()
    if first and str(first) != str(item_id):
        monday("""mutation($item:ID!, $first:ID!){change_item_position(item_id:$item, relative_to:$first,
            position_relative_method:before_at){id}}""", {"item": item_id, "first": first},
               api_version=POSITION_API_VERSION)


def create_subitems(parent_id, names, day=None):
    """One subitem per thing requested or bought, so each can be ticked off on its own."""
    sub_values = {SUB_STATUS: {"label": SUB_NEW_STATUS}}
    if day:
        sub_values[SUB_DATE] = {"date": day}
    values = json.dumps(sub_values)
    for name in names:
        monday("""mutation($parent:ID!, $name:String!, $values:JSON!){
            create_subitem(parent_item_id:$parent, item_name:$name, column_values:$values,
                           create_labels_if_missing:false){id}}""",
               {"parent": parent_id, "name": name[:255], "values": values})


def main():
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=LOOKBACK_DAYS)
    done = existing_sources()
    pending = open_requests()
    added = merged = 0
    # Oldest first: each request goes on top, so the newest submission ends up first.
    found = []
    for source in SOURCES:
        for item in recent_submissions(source, since):
            sub = submission_values(item)
            day = submission_day(source, item, sub)
            if day >= SINCE_DATE:
                found.append((day, item["created_at"], source, item, sub))
    found.sort(key=lambda f: f[:2])
    for day, _created, source, item, sub in found:
        if str(item["id"]) in done or item["name"].strip().upper().startswith("TEST"):
            continue
        req = parse_request(sub)
        if not req:
            continue
        name = item_name(sub.get(S_PROPERTY), item["name"].strip())
        values = build_values(source, item, sub, req)
        key = merge_key(source["country"], values[T_PROPERTY], req["type"])
        existing = pending.get(key) if values[T_PROPERTY] and not name.startswith("Other property") else None
        items = new_items(existing, req["items"])
        if DRY_RUN:
            what = f"merge into {existing['id']}" if existing else "new request"
            log(f"[dry run] {source['country']}: {name} — {req['type']}, {what}, from {item['name']} ({item['url']})")
            for thing in items:
                log(f"    - {thing}")
            continue
        if existing:
            changes = merge_values(existing, values, req)
            update_request(existing["id"], changes)
            create_subitems(existing["id"], items, day)
            move_to_top(existing["id"])
            existing["values"].update({k: v if isinstance(v, str) else v.get("text") or v.get("date") or v.get("label")
                                       for k, v in changes.items() if k != T_SUBMISSION})
            existing["open_items"].update(" ".join(i.lower().split()) for i in items)
            merged += 1
            log(f"Merged {item['name']}'s request into {name} (item {existing['id']}): {', '.join(items) or 'no new items'}")
        else:
            new_id = create_request(name, values)
            create_subitems(new_id, items, day)
            if values[T_PROPERTY] and not name.startswith("Other property"):
                pending[key] = {"id": new_id, "open_items": {" ".join(i.lower().split()) for i in items},
                                "values": {T_SOURCE: values[T_SOURCE], T_BY: values[T_BY], T_DATE: day,
                                           T_NOTES: values[T_NOTES]["text"], T_TYPE: req["type"],
                                           T_AMOUNT: values.get(T_AMOUNT, ""),
                                           T_PRIORITY: (values.get(T_PRIORITY) or {}).get("label", "")}}
            added += 1
            log(f"Added {name} ({req['type']}) from {item['name']} → item {new_id}")
        done.add(str(item["id"]))
    log(f"Purchase requests: {added} added, {merged} merged" + (" (dry run)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
