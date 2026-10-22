#!/usr/bin/env python3
"""Every night at 10 pm (Toronto): move finished tasks off the Task Hub board.

Each item on the Task Hub that is in the "Completed task" group, or is marked Done in a
one-off group, is moved to the "Task Hub · Archive" board, into its "Completed task" group.
The archive is a copy of the Task Hub, so every column (Country included), comment and file
comes along. The daily and weekly checklists recur, so their items
are never moved, even when ticked Done.

The [slack …] / [hospitable …] / [quo …] keys in archived items' notes are still read by
slack_tasks.py, so a message that already produced a task never produces another one.

Environment:
  MONDAY_API_TOKEN  monday.com API token (required)
  DRY_RUN           "true" to list what would be moved without moving it
  FORCE             "true" to run at any hour (scheduled runs only act at 10 pm Toronto time)
"""

import datetime as dt
import os
import sys

from slack_tasks import (COUNTRY_COLUMN, DONE_GROUP, DONE_STATUS, HUB, LOCAL_TZ, RECURRING_GROUPS, annotate,
                         board_layout, log, monday)

ARCHIVE_HOUR = 22


def relation_ids(item, column_id):
    for c in item["column_values"]:
        if c["id"] == column_id:
            return [int(i) for i in c.get("linked_item_ids") or []]
    return []


def finished_items(board_id, status_col, relation_col, country_col=""):
    """Items to archive, with the properties they link to and their country."""
    fields = f"""id name group{{title}} column_values(ids:["{status_col}", "{relation_col}", "{country_col}"]){{
        id text ... on BoardRelationValue {{linked_item_ids}}}}"""
    page = monday(f"query($id:[ID!]){{boards(ids:$id){{items_page(limit:500){{cursor items{{{fields}}}}}}}}}",
                  {"id": [board_id]})["boards"][0]["items_page"]
    items = list(page["items"])
    while page.get("cursor"):
        page = monday(f"query($c:String!){{next_items_page(limit:500, cursor:$c){{cursor items{{{fields}}}}}}}",
                      {"c": page["cursor"]})["next_items_page"]
        items.extend(page["items"])
    out = []
    for it in items:
        group = (it.get("group") or {}).get("title")
        status = next((c["text"] for c in it["column_values"] if c["id"] == status_col), "")
        country = next((c["text"] for c in it["column_values"] if c["id"] == country_col), "") or "?"
        if group in RECURRING_GROUPS:
            continue
        if group == DONE_GROUP or status == DONE_STATUS:
            out.append({"id": int(it["id"]), "name": it["name"], "country": country,
                        "properties": relation_ids(it, relation_col)})
    return out


def archive(dry_run):
    cols, _ = board_layout(HUB["tasks_board"])
    _, archive_groups = board_layout(HUB["archive_board"])
    group_id = archive_groups.get(DONE_GROUP)
    if not group_id:
        raise RuntimeError(f"{HUB['name']} archive board has no '{DONE_GROUP}' group")
    relation_col = cols.get("Property Link", "")
    items = finished_items(HUB["tasks_board"], cols["Status"], relation_col, cols.get(COUNTRY_COLUMN, ""))
    report = []
    for it in items:
        country = it["country"]
        if dry_run:
            report.append(f"WOULD ARCHIVE [{country}] {it['name']}")
            continue
        moved = monday("""mutation($board:ID!, $group:ID!, $item:ID!){
            move_item_to_board(board_id:$board, group_id:$group, item_id:$item){id}}""",
                       {"board": HUB["archive_board"], "group": group_id, "item": it["id"]})
        if not moved.get("move_item_to_board"):
            raise RuntimeError(f"monday didn't move item {it['id']} ({it['name']})")
        # Keep the property link if the move dropped it.
        if it["properties"] and relation_col:
            after = monday(f"""query($id:[ID!]){{items(ids:$id){{column_values(ids:["{relation_col}"]){{
                id ... on BoardRelationValue {{linked_item_ids}}}}}}}}""", {"id": [it["id"]]})["items"][0]
            if not relation_ids(after, relation_col):
                monday("""mutation($board:ID!, $item:ID!, $vals:JSON!){
                    change_multiple_column_values(board_id:$board, item_id:$item, column_values:$vals){id}}""",
                       {"board": HUB["archive_board"], "item": it["id"],
                        "vals": f'{{"{relation_col}": {{"item_ids": {it["properties"]}}}}}'})
        log(f"> [{country}] {it['name']} (item {it['id']})")
        report.append(f"ARCHIVED [{country}] {it['name']}")
    return report


def main():
    if not os.environ.get("MONDAY_API_TOKEN"):
        sys.exit("Missing required environment variable MONDAY_API_TOKEN")
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    local = dt.datetime.now(LOCAL_TZ)
    # GitHub cron is UTC, so the workflow is scheduled for both 10 pm EDT and 10 pm EST.
    if not force and local.hour != ARCHIVE_HOUR:
        log(f"It's {local:%H:%M} in Toronto, not {ARCHIVE_HOUR}:00; nothing to do.")
        return
    report = archive(dry_run)
    summary = f"{sum(1 for r in report if r.startswith(('ARCHIVED', 'WOULD')))} finished task(s) " \
              f"{'would be ' if dry_run else ''}moved to {HUB['name']} · Archive"
    log(f"\nDone. {summary}")
    annotate(f"Archive{' (dry run)' if dry_run else ''}", [summary, *report])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=Archive failed::{type(e).__name__}: {str(e)[:900]}".replace("\n", " "))
        raise
