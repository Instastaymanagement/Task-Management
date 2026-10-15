#!/usr/bin/env python3
"""Every 5 minutes: turn tasks found in Slack conversations into monday.com items.

1. Reads new messages (and new thread replies) since the last run from every
   Slack channel the bot has been added to.
2. Has Claude pick out the actionable tasks, who should do them, any deadline,
   and whether each one belongs to the USA or the Canada portfolio.
3. Creates each task on the "Daily Tasks" board of the matching monday.com
   workspace (USA Automated / Canada Automated), in the "Other task" group,
   with the person assigned so monday notifies them.

Each item's Notes carry a "[slack <channel> <ts>]" key for the message the task
came from. That key is checked before creating anything, so overlapping or
re-run windows never create the same task twice.

Environment:
  SLACK_BOT_TOKEN       Slack bot token (xoxb-...) (required)
  MONDAY_API_TOKEN      monday.com API token (required)
  ANTHROPIC_API_KEY     Claude API key (required)
  ANTHROPIC_WORKSPACE_ID  workspace for the key; needed only if the key isn't scoped to one
  DRY_RUN               "true" to log the tasks without creating them or moving the checkpoint
  STATE_FILE            where the last-run checkpoint is kept (default state/slack_state.json)
  FIRST_RUN_HOURS       how far back to read when there is no checkpoint yet (default 2)
  SLACK_CHANNELS        optional comma-separated channel names to limit the scan to
  DEFAULT_COUNTRY       board to use when a task can't be tied to a country (default Canada)
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import anthropic

SLACK_API = "https://slack.com/api"
MONDAY_API = "https://api.monday.com/v2"
MODEL = "claude-opus-5-5"

# Daily Tasks board and Properties Master board in each workspace
WORKSPACES = {
    "USA": {"name": "USA Automated (Trial)", "tasks_board": 18435520178, "properties_board": 18435507823},
    "Canada": {"name": "Canada Automated(Trial)", "tasks_board": 18435476578, "properties_board": 18435476437},
}
TASK_GROUP = "Other task"
NEW_STATUS = "Not done"

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
STATE_FILE = os.environ.get("STATE_FILE") or "state/slack_state.json"
FIRST_RUN_HOURS = float(os.environ.get("FIRST_RUN_HOURS") or 2)
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY") or "Canada"
THREAD_LOOKBACK_DAYS = 3   # thread replies are picked up on parents up to this old
CONTEXT_MESSAGES = 15      # earlier messages per channel shown to Claude for context
NOTES_LIMIT = 1900

SOURCE_KEY_RE = re.compile(r"\[slack (\w+) (\d+\.\d+)\]")
SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                 "channel_archive", "channel_unarchive", "pinned_item", "unpinned_item", "bot_add",
                 "bot_remove", "message_deleted", "message_changed", "tombstone"}

SYSTEM_PROMPT = """You read Slack conversations for Instastay, a short-term rental management company with properties in the USA and Canada, and pick out tasks that someone on the team needs to do.

A task is a concrete piece of work someone has been asked to do, has agreed to do, or clearly needs to do: fix, buy, clean, inspect, call, send, book, pay, follow up, etc.

Not tasks: greetings, FYIs, status updates about work already finished, questions answered in the same conversation, general discussion, jokes, or automated notifications with nothing to act on.

Rules:
- Only take tasks from messages marked [NEW]. Messages marked [context] are earlier conversation shown so you understand the [NEW] ones; never create a task from a [context] message alone.
- Skip a task if a later message in the conversation shows it is already done or cancelled.
- Skip a task if it is already on the list of open tasks you are given.
- One task per piece of work. If several messages talk about the same job, return it once, citing the message where it was asked for.
- title: a short imperative line (under 80 characters) that names the property when one is mentioned, e.g. "Replace smoke detector battery at 600 Sarnia Rd".
- details: one to three sentences with what is needed and any specifics from the conversation (amounts, times, guest names, unit numbers). Don't invent anything.
- assignee_ids: the monday user ids of the people who should do it, chosen from the team list. Use whoever was asked or tagged (@name), or who said they would do it. If nobody can be identified, return an empty list.
- country: "USA" or "Canada", from the property, city, or channel. If you can't tell, return "Unknown".
- deadline: YYYY-MM-DD if a date or relative day ("tomorrow", "by Friday") is given, resolved against the message date; otherwise an empty string.
- source_channel and source_ts: copy the channel id and ts of the [NEW] message the task comes from, exactly as shown.

If there are no tasks, return an empty list."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "assignee_ids": {"type": "array", "items": {"type": "integer"}},
                    "country": {"type": "string", "enum": ["USA", "Canada", "Unknown"]},
                    "deadline": {"type": "string"},
                    "source_channel": {"type": "string"},
                    "source_ts": {"type": "string"},
                },
                "required": ["title", "details", "assignee_ids", "country", "deadline",
                             "source_channel", "source_ts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tasks"],
    "additionalProperties": False,
}


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- HTTP

class HTTPStatusError(RuntimeError):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status


def http_json(url, *, headers, body=None, method=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                log(f"  HTTP {e.code} from {url}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise HTTPStatusError(e.code, url, e.read().decode()) from e
        except urllib.error.URLError:
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def slack(method, **params):
    url = f"{SLACK_API}/{method}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    resp = http_json(url, headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"})
    if not resp.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {resp.get('error')} {resp.get('needed') or ''}".strip())
    return resp


def slack_paged(method, key, **params):
    cursor = None
    while True:
        resp = slack(method, cursor=cursor, limit=200, **params)
        yield from resp.get(key, [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def monday(query, variables=None):
    resp = http_json(MONDAY_API, headers={
        "Authorization": os.environ["MONDAY_API_TOKEN"],
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }, body={"query": query, "variables": variables or {}})
    if resp.get("errors"):
        raise RuntimeError(f"monday API error: {json.dumps(resp['errors'])[:1000]}")
    return resp["data"]


# -------------------------------------------------------------------------- state

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


# -------------------------------------------------------------------------- Slack

def slack_users():
    """Slack user id -> {"name", "email"} for real people."""
    users = {}
    for u in slack_paged("users.list", "members"):
        if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT":
            continue
        p = u.get("profile") or {}
        users[u["id"]] = {
            "name": p.get("real_name") or p.get("display_name") or u.get("name") or u["id"],
            "email": (p.get("email") or "").lower(),
        }
    return users


def member_channels(only_names):
    chans = [c for c in slack_paged("conversations.list", "channels",
                                    types="public_channel,private_channel", exclude_archived="true")
             if c.get("is_member")]
    if only_names:
        chans = [c for c in chans if c["name"] in only_names]
    return chans


def readable(text, users):
    """Swap Slack's <@U123>, <#C123|name> and <url|label> markup for plain text."""
    text = re.sub(r"<@(\w+)>", lambda m: "@" + users.get(m.group(1), {}).get("name", m.group(1)), text or "")
    text = re.sub(r"<#\w+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    return re.sub(r"<(https?://[^>]+)>", r"\1", text)


def is_conversation(msg):
    return msg.get("subtype") not in SKIP_SUBTYPES and (msg.get("text") or msg.get("files"))


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%a %Y-%m-%d %H:%M UTC")


def fmt_message(msg, users, new, indent=""):
    who = users.get(msg.get("user"), {}).get("name") or msg.get("username") or (msg.get("bot_profile") or {}).get("name") or "unknown"
    text = readable(msg.get("text"), users)
    files = ", ".join(f.get("name") or f.get("title") or "file" for f in msg.get("files") or [])
    if files:
        text += f" [attached: {files}]"
    return f"{indent}[{'NEW' if new else 'context'}] ts={msg['ts']} {fmt_ts(msg['ts'])} {who}: {text}"


def channel_transcript(channel, users, checkpoint, now):
    """Transcript of one channel for Claude, plus how many [NEW] messages are in it."""
    history = [m for m in slack_paged("conversations.history", "messages", channel=channel["id"],
                                      oldest=f"{now - THREAD_LOOKBACK_DAYS * 86400:.6f}", latest=f"{now:.6f}")
               if is_conversation(m)]
    history.sort(key=lambda m: float(m["ts"]))
    context_ts = {m["ts"] for m in [m for m in history if float(m["ts"]) <= checkpoint][-CONTEXT_MESSAGES:]}

    lines, new_count = [], 0
    for m in history:
        is_new = float(m["ts"]) > checkpoint
        replies = []
        if m.get("reply_count") and float(m.get("latest_reply") or 0) > checkpoint:
            replies = [r for r in slack_paged("conversations.replies", "messages", channel=channel["id"], ts=m["ts"])
                       if r["ts"] != m["ts"] and is_conversation(r) and float(r["ts"]) <= now]
        has_new_reply = any(float(r["ts"]) > checkpoint for r in replies)
        if not (is_new or has_new_reply or m["ts"] in context_ts):
            continue
        lines.append(fmt_message(m, users, is_new))
        new_count += is_new
        for r in replies:
            r_new = float(r["ts"]) > checkpoint
            lines.append(fmt_message(r, users, r_new, indent="    reply "))
            new_count += r_new
    return lines, new_count


# ------------------------------------------------------------------------- monday

def monday_users():
    data = monday("query{users(kind:non_guests, limit:500){id name email enabled}}")
    return [{"id": int(u["id"]), "name": u["name"], "email": (u["email"] or "").lower()}
            for u in data["users"] if u.get("enabled", True)]


def board_layout(board_id):
    data = monday("query($id:[ID!]){boards(ids:$id){columns{id title type} groups{id title}}}", {"id": [board_id]})
    board = data["boards"][0]
    cols = {c["title"]: c["id"] for c in board["columns"]}
    groups = {g["title"]: g["id"] for g in board["groups"]}
    return cols, groups


def board_items(board_id, column_ids):
    query = """query($id:[ID!], $cols:[String!]){boards(ids:$id){items_page(limit:500){cursor
        items{id name group{title} column_values(ids:$cols){id text}}}}}"""
    next_query = """query($cursor:String!, $cols:[String!]){next_items_page(limit:500, cursor:$cursor){cursor
        items{id name group{title} column_values(ids:$cols){id text}}}}"""
    page = monday(query, {"id": [board_id], "cols": column_ids})["boards"][0]["items_page"]
    items = list(page["items"])
    while page.get("cursor"):
        page = monday(next_query, {"cursor": page["cursor"], "cols": column_ids})["next_items_page"]
        items.extend(page["items"])
    return items


def load_board(country):
    ws = WORKSPACES[country]
    cols, groups = board_layout(ws["tasks_board"])
    for title in ("Person", "Status", "Deadline", "Notes/Updates"):
        if title not in cols:
            raise RuntimeError(f"Daily Tasks board in {ws['name']} has no '{title}' column")
    items = board_items(ws["tasks_board"], [cols["Person"], cols["Status"], cols["Notes/Updates"]])
    keys, open_tasks = set(), []
    for it in items:
        vals = {c["id"]: c["text"] or "" for c in it["column_values"]}
        keys.update(SOURCE_KEY_RE.findall(vals.get(cols["Notes/Updates"], "")))
        if (it.get("group") or {}).get("title") != "Completed task" and vals.get(cols["Status"]) != "Done":
            open_tasks.append(f"- [{country}] {it['name']} (assigned: {vals.get(cols['Person']) or 'nobody'})")
    return {"country": country, "board_id": ws["tasks_board"], "cols": cols,
            "group_id": groups.get(TASK_GROUP), "keys": keys, "open_tasks": open_tasks}


def property_names(country):
    board_id = WORKSPACES[country]["properties_board"]
    data = monday("query($id:[ID!]){boards(ids:$id){items_page(limit:500){items{name}}}}", {"id": [board_id]})
    return [it["name"] for it in data["boards"][0]["items_page"]["items"]]


def column_values(task, cols, valid_user_ids, notes):
    vals = {
        cols["Status"]: {"label": NEW_STATUS},
        cols["Notes/Updates"]: notes,
    }
    people = [i for i in dict.fromkeys(task["assignee_ids"]) if i in valid_user_ids]
    if people:
        vals[cols["Person"]] = {"personsAndTeams": [{"id": i, "kind": "person"} for i in people]}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", task.get("deadline") or ""):
        vals[cols["Deadline"]] = {"date": task["deadline"]}
    return vals


def build_notes(task, permalink):
    key = f"[slack {task['source_channel']} {task['source_ts']}]"
    tail = f"\nSlack: {permalink}\n{key}" if permalink else f"\n{key}"
    return task["details"].strip()[: NOTES_LIMIT - len(tail)] + tail


def create_item(board, task, valid_user_ids, permalink):
    vals = column_values(task, board["cols"], valid_user_ids, build_notes(task, permalink))
    data = monday("""mutation($board:ID!, $group:String, $name:String!, $vals:JSON!){
        create_item(board_id:$board, group_id:$group, item_name:$name, column_values:$vals){id}}""",
                  {"board": board["board_id"], "group": board["group_id"], "name": task["title"][:255],
                   "vals": json.dumps(vals)})
    return data["create_item"]["id"]


# ------------------------------------------------------------------------- Claude

def team_list(m_users, s_users):
    by_email = {u["email"]: u for u in s_users.values() if u["email"]}
    lines = []
    for u in m_users:
        s = by_email.get(u["email"])
        slack_name = f", Slack name: {s['name']}" if s and s["name"] != u["name"] else ""
        lines.append(f"- monday id {u['id']}: {u['name']}{slack_name}")
    return "\n".join(lines)


def build_prompt(transcripts, team, properties, open_tasks, now):
    parts = [f"Current time: {fmt_ts(now)}", "", "Team (monday users you can assign):", team, ""]
    for country, names in properties.items():
        parts.append(f"{country} properties: " + ("; ".join(names) or "(none listed)"))
    parts += ["", "Open tasks already on the boards (don't add these again):",
              "\n".join(open_tasks[-300:]) or "(none)", "", "Slack conversations:"]
    for chan, lines in transcripts:
        parts += ["", f"#{chan['name']} (channel id {chan['id']})", *lines]
    return "\n".join(parts)


def extract_tasks(client, prompt):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        log("! Claude declined this batch")
        return []
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens); the checkpoint was not moved")
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)["tasks"]


# --------------------------------------------------------------------------- main

def permalink_for(team_url, channel_id, ts):
    return f"{team_url.rstrip('/')}/archives/{channel_id}/p{ts.replace('.', '')}" if team_url else ""


def main():
    for var in ("SLACK_BOT_TOKEN", "MONDAY_API_TOKEN", "ANTHROPIC_API_KEY"):
        if not os.environ.get(var):
            sys.exit(f"Missing required environment variable {var}")
    if DRY_RUN:
        log("DRY RUN: tasks are logged but not created, and the checkpoint is not moved")

    now = time.time()
    state = load_state(STATE_FILE)
    checkpoint = float(state.get("checkpoint") or now - FIRST_RUN_HOURS * 3600)
    log(f"Reading Slack messages since {fmt_ts(checkpoint)}")

    team_url = slack("auth.test").get("url", "")
    s_users = slack_users()
    only = {c.strip().lstrip("#") for c in (os.environ.get("SLACK_CHANNELS") or "").split(",") if c.strip()}
    channels = member_channels(only)
    if not channels:
        log("The Slack bot isn't in any channels yet: invite it with /invite @<bot name>")

    transcripts, new_total = [], 0
    for chan in channels:
        try:
            lines, new_count = channel_transcript(chan, s_users, checkpoint, now)
        except RuntimeError as e:
            log(f"! #{chan['name']}: {e}")
            continue
        if new_count:
            transcripts.append((chan, lines))
            new_total += new_count
            log(f"#{chan['name']}: {new_count} new message(s)")

    if not new_total:
        log("No new messages.")
        if not DRY_RUN:
            save_state(STATE_FILE, {"checkpoint": now})
        return

    boards = {c: load_board(c) for c in WORKSPACES}
    m_users = monday_users()
    valid_ids = {u["id"] for u in m_users}
    properties = {c: property_names(c) for c in WORKSPACES}
    open_tasks = boards["USA"]["open_tasks"] + boards["Canada"]["open_tasks"]

    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    client = anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
    )
    prompt = build_prompt(transcripts, team_list(m_users, s_users), properties, open_tasks, now)
    tasks = extract_tasks(client, prompt)
    log(f"Claude found {len(tasks)} task(s)")

    seen = boards["USA"]["keys"] | boards["Canada"]["keys"]
    names = {u["id"]: u["name"] for u in m_users}
    created = 0
    for task in tasks:
        key = (task["source_channel"], task["source_ts"])
        if key in seen:
            log(f"= already on monday: {task['title']}")
            continue
        seen.add(key)
        country = task["country"] if task["country"] in WORKSPACES else DEFAULT_COUNTRY
        who = ", ".join(names.get(i, str(i)) for i in task["assignee_ids"] if i in valid_ids) or "unassigned"
        label = f"[{country}] {task['title']} -> {who}" + (f" (due {task['deadline']})" if task["deadline"] else "")
        if DRY_RUN:
            log(f"~ {label}\n    {task['details']}")
            continue
        item_id = create_item(boards[country], task, valid_ids,
                              permalink_for(team_url, task["source_channel"], task["source_ts"]))
        created += 1
        log(f"+ {label} (item {item_id})")

    if not DRY_RUN:
        save_state(STATE_FILE, {"checkpoint": now})
    log(f"\nDone. Created {created} task(s).")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=Slack tasks failed::{e.code}")
        raise
    except Exception as e:
        if os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=Slack tasks failed::{type(e).__name__}: {str(e)[:900]}".replace("\n", " "))
        raise
