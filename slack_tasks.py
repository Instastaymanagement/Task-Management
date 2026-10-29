#!/usr/bin/env python3
"""Every 5 minutes: turn tasks found in Slack and guest conversations into monday.com items.

1. Reads new messages since the last run from
   - every Slack channel the bot has been added to (including thread replies), and
   - every Hospitable guest conversation with new activity, and
   - every Quo text conversation on the mapped Quo numbers (calls are not read).
2. Has Claude pick out the actionable tasks, who should do them, which property
   each is for, a deadline that fits its urgency, and whether each one belongs to
   the USA or the Canada portfolio (guest
   conversations go to the country of their property, Quo conversations to the
   country of the Quo number).
3. Creates each task on the "Task Hub" board (Instastay Automated → Task Hub),
   in the "Other task" group, with its Country set, the person assigned so
   monday notifies them, and the property linked to its row in that country's
   Properties Master.
4. Checks the open tasks in "Other task" against the same conversations: a task
   the conversation shows was handled is marked Done and moved to "Completed
   task"; progress on one updates its status or deadline. Every change is
   explained in a comment on the item.

Each item's Notes carry a "[slack <channel> <ts>]" or
"[hospitable <reservation> <message id>]" or "[quo <conversation> <message id>]" key for the message the task came from. That key is checked before creating anything, so overlapping or
re-run windows never create the same task twice.

Environment:
  SLACK_BOT_TOKEN       Slack bot token (xoxb-...) (required)
  MONDAY_API_TOKEN      monday.com API token (required)
  HOSPITABLE_API_TOKEN  Hospitable personal access token; guest conversations are skipped without it
  QUO_API_KEY           Quo (OpenPhone) API key; Quo texts are skipped without it
  QUO_NUMBERS           which Quo number belongs to which country, e.g. "+15195550100=Canada,+16175550100=USA"
  CANDIDATE_BOARD       monday board of job candidates (Vacancies); their Quo texts are left to HR-Interview
  ANTHROPIC_API_KEY     Claude API key (required)
  ANTHROPIC_WORKSPACE_ID  workspace for the key; needed only if the key isn't scoped to one
  DRY_RUN               "true" to log the tasks without creating them or moving the checkpoint
  STATE_FILE            where the last-run checkpoint is kept (default state/slack_state.json)
  FIRST_RUN_HOURS       how far back to read when there is no checkpoint yet (default 2)
  LOOKBACK_HOURS        read this many hours back instead of from the checkpoint (for manual re-scans)
  SLACK_CHANNELS        optional comma-separated channel names to limit the scan to
  DEFAULT_COUNTRY       country to use when a task can't be tied to one (default Canada)
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import zlib
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import anthropic

SLACK_API = "https://slack.com/api"
HOSPITABLE_API = "https://public.api.hospitable.com/v2"
QUO_API = "https://api.openphone.com/v1"
MONDAY_API = "https://api.monday.com/v2"
MODEL = "claude-opus-5-5"

# Task Hub: one board for USA and Canada tasks (its Country column says which), and its
# archive (finished tasks, moved nightly by archive_tasks.py)
HUB = {"name": "Task Hub", "tasks_board": 18436688444, "archive_board": 18436688447}
COUNTRY_COLUMN = "Country"
SHARED_COUNTRY = "Both"  # the Daily Checklist is the same for both countries
# Properties Master board of each country
WORKSPACES = {
    "USA": {"properties_board": 18435507823},
    "Canada": {"properties_board": 18435476437},
}
TASK_GROUP = "Other task"
# Groups that repeat; their items are never closed or updated automatically
RECURRING_GROUPS = {"Daily Checklist", "Weekly Tasks"}
DONE_GROUP = "Completed task"
# Early check-in / late checkout requests wait here until the day before the stay date,
# then move to the due group (see release_upcoming)
UPCOMING_GROUP = "Upcoming Check-in/out Requests"
DUE_GROUP = "Overdue & Due Today"
DONE_STATUS = "Done"
PROGRESS_STATUS = "Working on it"
NEW_STATUS = "Not done"

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
STATE_FILE = os.environ.get("STATE_FILE") or "state/slack_state.json"
FIRST_RUN_HOURS = float(os.environ.get("FIRST_RUN_HOURS") or 2)
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY") or "Canada"
LOCAL_TZ = ZoneInfo("America/Toronto")  # London ON and Boston share Eastern time
DEFAULT_DEADLINE_DAYS = 3                # used only if Claude's date can't be read
THREAD_LOOKBACK_DAYS = 3   # thread replies are picked up on parents up to this old
CONTEXT_MESSAGES = 15      # earlier messages per channel shown to Claude for context
GUEST_CONTEXT_MESSAGES = 20  # latest messages per guest conversation shown to Claude
NOTES_LIMIT = 1900
NOTE_PREVIEW = 150         # characters of each open task's notes shown to Claude

# Who covers for whom: on these weekdays (Mon=0) the first person is off and the second does all their work
DAYS_OFF = {"Anna": ({0, 1}, "Ivy"), "Ivy": ({5, 6}, "Anna")}
# Customer support tasks are split evenly between these people (the same guest stays with the same person)
SUPPORT_CATEGORY = "Guest"
SUPPORT_TEAM = ("Anna", "Ivy")

# Hospitable property country code -> workspace
COUNTRY_CODES = {"US": "USA", "CA": "Canada"}
# Templated host messages (check-in instructions, reminders) never hold new work
SKIP_GUEST_SOURCES = {"automated"}

QUO_CONTEXT_MESSAGES = 20   # latest texts per Quo conversation shown to Claude

# Sentences that carry no information on their own: thanks, greetings, "I'll get back to you".
# A host or team message made only of these is shown as [context], never [NEW], so a
# conversation whose only new messages are such replies doesn't cost a Claude call.
_NAME = r"(?:,? [A-Z][\w'-]*)?"
_SOON = r"(?: (?:soon|shortly|as soon as (?:i can|possible)|right away))?"
_BACK = rf"(?:(?:and )?(?:i'll |i will |will )?(?:get back to you|be in touch|follow up){_SOON})"
HOLDING_SENTENCES = [re.compile(rf"^{p}[.!]*$", re.I) for p in (
    rf"(?:hi|hello|hey|good (?:morning|afternoon|evening)){_NAME}",
    rf"(?:thanks|thank you)(?: so much| very much)?(?: for (?:asking|checking(?: in)?|letting (?:me|us) know|"
    rf"the (?:update|message|heads up)|reaching out|your (?:message|patience)|trying that|your understanding))?{_NAME}",
    rf"(?:you're|you are) (?:very |so |most )?welcome{_NAME}",
    rf"(?:i've|i have) received your message(?:,? and {_BACK})?",
    rf"(?:sorry|i'm sorry|apologies) for the (?:trouble|inconvenience|delay)(?: with this)?",
    rf"(?:let me|i'll|i will|i'm|i am) (?:take a look|look(?:ing)? into (?:this|it|that)|check(?:ing)? (?:on )?(?:this|that|it))"
    rf"(?: for you)?(?: now)?(?:,? {_BACK})?",
    _BACK,
)]


def is_holding_reply(text):
    """True if every sentence is a pleasantry or a promise to get back, with nothing else in it."""
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text or "").replace("\u2019", "'")
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if x.strip()]
    return bool(sentences) and all(any(p.match(x) for p in HOLDING_SENTENCES) for x in sentences)

SOURCE_KEY_RE = re.compile(r"\[(slack|hospitable|quo) ([\w-]+) ([\w.]+)\]")
SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                 "channel_archive", "channel_unarchive", "pinned_item", "unpinned_item", "bot_add",
                 "bot_remove", "message_deleted", "message_changed", "tombstone"}

SYSTEM_PROMPT = """You read conversations for Instastay, a short-term rental management company with properties in the USA and Canada, and pick out tasks that someone on the team needs to do.

There are three kinds of conversation:
- Slack: the team talking to each other.
- Quo: text messages on the company's business phone lines, with owners, guests, cleaners, contractors and vendors. Tasks are the follow-ups the team owes: things promised or requested in the texts.
- Guest: a guest messaging the host about their reservation. The host replies come from the team (sometimes drafted by AI). Guest-side tasks are things the team must do for the stay: repairs, missing or broken items, supplies, cleaning, laundry pickup, early check-in or late checkout to arrange, refunds or charges to process, complaints to follow up. A promise the host made in a [NEW] message ("I'll send someone tomorrow") is a task too. A question the host already fully answered (wifi password, parking, directions) is not.

A task is a concrete piece of work someone has been asked to do, has agreed to do, or clearly needs to do: fix, buy, clean, inspect, call, send, book, pay, follow up, etc.

Not tasks: greetings, FYIs, status updates about work already finished, questions answered in the same conversation, general discussion, jokes, or automated notifications with nothing to act on.

Rules:
- Only take tasks from messages marked [NEW]. Messages marked [context] are earlier conversation shown so you understand the [NEW] ones; never create a task from a [context] message alone.
- Skip a task if a later message in the conversation shows it is already done or cancelled.
- Skip a task if it is already on the list of open tasks you are given.
- One task per piece of work. If several messages talk about the same job, return it once, citing the message where it was asked for.
- title: a short imperative line (under 80 characters) that names the property when one is mentioned, e.g. "Replace smoke detector battery at 600 Sarnia Rd".
- details: one to three sentences with what is needed and any specifics from the conversation (amounts, times, guest names, unit numbers). Don't invent anything.
- assignee_ids: the monday user ids of the people who should do it, chosen from the team list. Use whoever was asked or tagged (@name), or who said they would do it. If nobody can be identified, return an empty list. Never assign a guest.
  These areas always belong to the same person, whoever was asked:
  - Claims against Airbnb guests (damage claims, AirCover, security deposits, charging a guest for damage): Anna.
  - Warranty claims (appliances, fixtures, products under warranty): Ivy.
  - Accounting (bookkeeping, invoices, bills, reconciliations, payouts, reimbursements, expense records): Ivy.
  - Cleaning schedules (booking and scheduling cleaners and turnovers): Anna.
  Customer support (category "Guest") is shared between Anna and Ivy and days off are covered automatically after you answer, so don't try to balance or cover for days off yourself.
- country: "USA" or "Canada". For guest and Quo conversations use the country in the conversation header. For Slack, use the property, city, or channel; if you can't tell, return "Unknown".
- property_ids: the Properties Master ids (from the property lists) of the properties the task is about, in the same country as the task. Match street names, nicknames and unit numbers loosely ("Sarnia" = "600 Sarnia Rd"). For guest conversations it is the reservation's property. Empty list if no property is involved (e.g. office or admin work).
- category: the one that fits best:
  - "Maintenance": repairs, broken or missing fixtures and appliances, heat, plumbing, power, internet, pest control, contractors, garbage and bins.
  - "Cleaning": cleanings, turnovers, cleaners' schedules, laundry, restocking supplies and toiletries.
  - "Guest": replies to guests, check-in/check-out arrangements, early check-in or late checkout, lockouts and access, lost items, parking for guests, guest requests.
  - "Claims": damage claims, AirCover, security deposits, charging guests for damages.
  - "Payments & Bills": refunds, payment requests, invoices, utility bills, payouts, costs and reimbursements.
  - "Owners": owner communication, owner reports, management agreements.
  - "Listings & Licensing": listings on Airbnb/VRBO/Furnished Finder, pricing, calendar blocks, licences, permits, inspections.
  - "Admin & Hiring": internal admin, documents, reports, tools and accounts, hiring and staff.
- deadline: always a date, YYYY-MM-DD, never empty. If the conversation gives a date or relative day ("tomorrow", "by Friday"), use it, resolved against the message date. Otherwise set it from how urgent the task is and what kind of task it is, counting from today:
  - Today: anything affecting a guest who is staying now (no heat, no hot water, leak, lockout, broken appliance, missing essentials), safety issues, and anything that must happen before a check-in later today.
  - Before the next check-in: cleaning, restocking, repairs or setup needed before an arriving guest; use the day before check-in (or today if check-in is tomorrow).
  - Within 1-2 days: replies owed to owners, guests or vendors, laundry, deliveries, refunds and payments, small repairs at occupied properties.
  - Within 3-7 days: routine maintenance at empty properties, purchases, scheduling contractors, follow-ups with no time pressure.
  - Within 2 weeks: admin, documents, licensing, listings, improvements.
  Never set a date before today.
- stay_date: for an early check-in or late checkout request, the date of that check-in or checkout (YYYY-MM-DD); the task is held back and shows up on the board the day before it. Empty string for every other task.
- source, source_conversation and source_message: copy the source and conversation id from the conversation header, and the id of the [NEW] message the task comes from, exactly as shown.

If there are no tasks, return an empty list.

Updating open tasks (task_updates):
You are also given the open tasks on the boards, each with its id, the conversation it came from, its notes and deadline. When a [NEW] message shows what happened to one of them, return an entry in task_updates:
- action "done": the [NEW] messages show the work is finished, the question was answered, the guest/owner/vendor got the reply they needed, or the task was cancelled or is no longer needed. A host or team reply that fully answers what the task asked for counts ("Reply to X about parking" is done once the parking rules were sent).
- action "in_progress": someone has started or taken it on but it isn't finished ("I'm on it", "booked the plumber for Monday").
- action "note": something relevant happened that doesn't change the status (new details, a changed plan, a blocker, a promise of a new date).
- comment: what a teammate would post on the task in monday: one or two short, plain sentences saying what happened and who did it, in a natural voice. No labels, prefixes, emojis, sign-offs, or mention of automation or AI. E.g. "Ivy sent Heather the street parking rules in Hospitable, so this one's done." or "Waleed's lining up the tech to check the stove; Jeannette is OK with them coming by."
- new_deadline: YYYY-MM-DD if the conversation sets or moves the date for the work, otherwise an empty string.
- item_id: the open task's id, exactly as listed. source_message: the id of the [NEW] message this is based on.
Only use evidence from [NEW] messages; never mark a task done because it is old, or from a guess. If it is unclear whether a task is finished, use "note" or leave it out. A message can both resolve an open task and create a new task. If nothing changed for any open task, return an empty list."""

# Labels of the Task Hub's Category column
CATEGORIES = ["Maintenance", "Cleaning", "Guest", "Claims", "Payments & Bills", "Owners", "Listings & Licensing",
              "Admin & Hiring"]
CATEGORY_COLUMN = "Category"

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
                    "property_ids": {"type": "array", "items": {"type": "integer"}},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "deadline": {"type": "string"},
                    "stay_date": {"type": "string"},
                    "source": {"type": "string", "enum": ["slack", "hospitable", "quo"]},
                    "source_conversation": {"type": "string"},
                    "source_message": {"type": "string"},
                },
                "required": ["title", "details", "assignee_ids", "country", "property_ids", "category", "deadline",
                             "stay_date", "source", "source_conversation", "source_message"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tasks", "task_updates"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA["properties"]["task_updates"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "action": {"type": "string", "enum": ["done", "in_progress", "note"]},
            "comment": {"type": "string"},
            "new_deadline": {"type": "string"},
            "source_message": {"type": "string"},
        },
        "required": ["item_id", "action", "comment", "new_deadline", "source_message"],
        "additionalProperties": False,
    },
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


def hospitable(path, params=None):
    url = f"{HOSPITABLE_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return http_json(url, headers={"Authorization": f"Bearer {os.environ['HOSPITABLE_API_TOKEN']}",
                                   "Accept": "application/json"})


def hospitable_pages(path, params):
    page = 1
    while True:
        resp = hospitable(path, {**params, "page": page, "per_page": 100})
        yield from resp.get("data", [])
        if page >= (resp.get("meta") or {}).get("last_page", page):
            return
        page += 1


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
    return f"{indent}[{'NEW' if new else 'context'}] id={msg['ts']} {fmt_ts(msg['ts'])} {who}: {text}"


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


# --------------------------------------------------------------------- Hospitable

def iso_ts(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def hospitable_properties():
    """Property id -> {"name", "country"}."""
    props = {}
    for p in hospitable_pages("/properties", {}):
        code = ((p.get("address") or {}).get("country_code") or (p.get("address") or {}).get("country") or "").upper()
        props[p["id"]] = {"name": p.get("name") or p["id"], "country": COUNTRY_CODES.get(code, "Unknown")}
    return props


def active_reservations(props, checkpoint, now):
    """Reservations whose guest conversation has had a message since the checkpoint."""
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    since = dt.datetime.fromtimestamp(checkpoint, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return list(hospitable_pages("/reservations", {
        "properties[]": list(props),
        "start_date": (today - dt.timedelta(days=180)).isoformat(),
        "end_date": (today + dt.timedelta(days=365)).isoformat(),
        "last_message_at": since,
        "include": "guest,properties",
    }))


def guest_name(res):
    g = res.get("guest") or {}
    return " ".join(x for x in (g.get("first_name"), g.get("last_name")) if x) or "Guest"


def reservation_property(res, props):
    for p in res.get("properties") or []:
        if p.get("id") in props:
            return {**props[p["id"]], "id": p["id"]}
    return {"name": "unknown property", "country": "Unknown", "id": None}


def guest_transcript(res, prop, checkpoint, now):
    """Transcript of one guest conversation for Claude, plus how many [NEW] messages are in it."""
    msgs = [m for m in hospitable(f"/reservations/{res['id']}/messages").get("data", [])
            if (m.get("body") or "").strip() and m.get("source") not in SKIP_GUEST_SOURCES
            and iso_ts(m["created_at"]) <= now]
    msgs.sort(key=lambda m: m["created_at"])
    msgs = msgs[-GUEST_CONTEXT_MESSAGES:]
    guest = guest_name(res)
    lines, new_count = [], 0
    for m in msgs:
        is_new = iso_ts(m["created_at"]) > checkpoint
        if m.get("sender_type") != "guest" and is_holding_reply(m["body"]):
            is_new = False
        if m.get("sender_type") == "guest":
            who = f"{guest} (guest)"
        else:
            who = f"{(m.get('author') or {}).get('name') or (m.get('sender') or {}).get('full_name') or 'Host'} (host)"
        text = " ".join(m["body"].split())
        if m.get("attachments"):
            text += f" [{len(m['attachments'])} attachment(s)]"
        stamp = fmt_ts(iso_ts(m["created_at"]))
        lines.append(f"[{'NEW' if is_new else 'context'}] id={m['id']} {stamp} {who}: {text}")
        new_count += is_new
    return lines, new_count


def guest_header(res, prop):
    return (f"Guest conversation (source hospitable, conversation id {res['id']}): {guest_name(res)} at "
            f"{prop['name']} ({prop['country']}), {res.get('platform') or ''} reservation {res.get('code') or ''}, "
            f"check-in {(res.get('check_in') or '')[:10]}, check-out {(res.get('check_out') or '')[:10]}"
            + (f". Hospitable issue alert: {res['issue_alert']}" if res.get("issue_alert") else ""))


def read_hospitable(checkpoint, now):
    """(transcripts, new message count, reservation id -> info) for guest conversations."""
    props = hospitable_properties()
    transcripts, new_total, info = [], 0, {}
    for res in active_reservations(props, checkpoint, now):
        prop = reservation_property(res, props)
        lines, new_count = guest_transcript(res, prop, checkpoint, now)
        if not new_count:
            continue
        transcripts.append((guest_header(res, prop), lines))
        new_total += new_count
        info[res["id"]] = {"country": prop["country"], "hospitable_property_id": prop["id"],
                           "label": f"{guest_name(res)} at {prop['name']}, reservation {res.get('code') or res['id']}"}
        log(f"Guest {guest_name(res)} at {prop['name']}: {new_count} new message(s)")
    return transcripts, new_total, info


# ---------------------------------------------------------------------------- Quo

def quo(path, params=None):
    url = f"{QUO_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    # Quo's firewall rejects Python's default User-Agent, so name the client.
    return http_json(url, headers={"Authorization": os.environ["QUO_API_KEY"], "Accept": "application/json",
                                   "User-Agent": "Instastay-Task-Management/1.0"})


def quo_pages(path, params, limit=None):
    token, count = None, 0
    while True:
        resp = quo(path, {**params, **({"pageToken": token} if token else {})})
        for row in resp.get("data") or []:
            yield row
            count += 1
            if limit and count >= limit:
                return
        token = resp.get("nextPageToken")
        if not token:
            return


def iso_z(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_quo_numbers(spec):
    """"+15195550100=Canada,+16175550100=USA" -> {"+15195550100": "Canada", ...}"""
    out = {}
    for part in (spec or "").split(","):
        if "=" in part:
            num, country = part.split("=", 1)
            out[re.sub(r"[^\d+]", "", num.strip())] = country.strip()
    return out


def quo_lines(mapping):
    """Quo phone lines with the board each one belongs to."""
    lines = []
    for pn in quo_pages("/phone-numbers", {}):
        number = pn.get("number") or pn.get("formattedNumber") or ""
        country = mapping.get(re.sub(r"[^\d+]", "", number))
        if not country:
            name = (pn.get("name") or "").lower()
            country = "Canada" if "canada" in name else "USA" if ("usa" in name or "us " in name + " ") else "Unknown"
        lines.append({"id": pn["id"], "number": number, "name": pn.get("name") or number, "country": country})
    return lines


def quo_transcript(line, convo, checkpoint, now):
    """Transcript of one Quo text conversation for Claude, plus how many [NEW] messages are in it."""
    base = {"phoneNumberId": line["id"], "participants": convo.get("participants") or []}
    entries = []
    for m in quo_pages("/messages", {**base, "maxResults": 50}, limit=QUO_CONTEXT_MESSAGES):
        if not (m.get("text") or "").strip():
            continue
        who = "team" if m.get("direction") == "outgoing" else (m.get("from") or "contact")
        if iso_ts(m["createdAt"]) <= now:
            is_new = iso_ts(m["createdAt"]) > checkpoint and not (who == "team" and is_holding_reply(m["text"]))
            entries.append((m["createdAt"], m["id"], who, " ".join(m["text"].split()), is_new))
    entries.sort(key=lambda e: e[0])
    lines, new_count = [], 0
    for created, eid, who, text, is_new in entries[-QUO_CONTEXT_MESSAGES:]:
        lines.append(f"[{'NEW' if is_new else 'context'}] id={eid} {fmt_ts(iso_ts(created))} {who}: {text}")
        new_count += is_new
    return lines, new_count


def candidate_phones(board_id):
    """Phone numbers on the job candidates board; the HR-Interview automation handles their texts."""
    if not board_id:
        return set()
    data = monday("query($id:[ID!]){boards(ids:$id){columns{id type}}}", {"id": [board_id]})
    phone_cols = [c["id"] for c in data["boards"][0]["columns"] if c["type"] == "phone"]
    if not phone_cols:
        return set()
    phones = set()
    for it in board_items(board_id, phone_cols):
        for c in it["column_values"]:
            num = re.sub(r"[^\d+]", "", c["text"] or "")
            if num:
                phones.add(num if num.startswith("+") else "+1" + num[-10:])
    return phones


def read_quo(checkpoint, now):
    """(transcripts, new message count, conversation id -> info) for Quo texts."""
    mapping = parse_quo_numbers(os.environ.get("QUO_NUMBERS"))
    candidates = candidate_phones(int(os.environ.get("CANDIDATE_BOARD") or 0))
    transcripts, new_total, info = [], 0, {}
    lines_found = quo_lines(mapping)
    if DRY_RUN and os.environ.get("GITHUB_ACTIONS"):
        found = "%0A".join(f"{ln['number']} ({ln['name']}) -> {ln['country']}" for ln in lines_found)
        log(f"::notice title=Quo numbers::{found or 'none found'}")
    for line in lines_found:
        if line["country"] not in WORKSPACES:
            log(f"! Quo number {line['number']} ({line['name']}) isn't mapped to USA or Canada in QUO_NUMBERS; skipped")
            continue
        convos = quo_pages("/conversations", {"phoneNumbers": [line["id"]], "maxResults": 100,
                                              "updatedAfter": iso_z(checkpoint)})
        for convo in convos:
            if candidates & set(convo.get("participants") or []):
                continue  # a job candidate: HR-Interview handles these texts
            lines, new_count = quo_transcript(line, convo, checkpoint, now)
            if not new_count:
                continue
            who = convo.get("name") or ", ".join(convo.get("participants") or []) or "unknown"
            transcripts.append((f"Quo conversation (source quo, conversation id {convo['id']}) on the "
                                f"{line['country']} line {line['number']} with {who} ({line['country']})", lines))
            new_total += new_count
            info[convo["id"]] = {"country": line["country"], "label": f"{who} on the {line['country']} Quo line"}
            log(f"Quo {line['country']} line, {who}: {new_count} new")
    return transcripts, new_total, info


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


def load_board():
    cols, groups = board_layout(HUB["tasks_board"])
    for title in ("Person", "Status", "Deadline", "Notes/Updates", COUNTRY_COLUMN):
        if title not in cols:
            raise RuntimeError(f"{HUB['name']} board has no '{title}' column")
    items = board_items(HUB["tasks_board"], [cols["Person"], cols["Status"], cols["Notes/Updates"], cols["Deadline"],
                                             cols[COUNTRY_COLUMN]])
    keys, open_tasks, updatable = set(), [], {}
    # Archived tasks still block their source message from creating a task again.
    for it in board_items(HUB["archive_board"], [cols["Notes/Updates"]]):
        keys.update(SOURCE_KEY_RE.findall(" ".join(c["text"] or "" for c in it["column_values"])))
    for it in items:
        vals = {c["id"]: c["text"] or "" for c in it["column_values"]}
        notes = vals.get(cols["Notes/Updates"], "")
        item_keys = SOURCE_KEY_RE.findall(notes)
        keys.update(item_keys)
        group = (it.get("group") or {}).get("title")
        country = vals.get(cols[COUNTRY_COLUMN]) or "Unknown"
        if group == DONE_GROUP or vals.get(cols["Status"]) == DONE_STATUS:
            continue
        # Only one-off tasks are closed automatically; the daily and weekly checklists recur.
        # One-off tasks (in "Other task" or any other non-recurring group, such as "Overdue &
        # Due Today") are listed once, with their ids, in the updatable list.
        if group in RECURRING_GROUPS:
            open_tasks.append(f"- [{country}] {it['name']} (assigned: {vals.get(cols['Person']) or 'nobody'})")
        else:
            updatable[int(it["id"])] = {
                "id": int(it["id"]), "country": country, "name": it["name"], "status": vals.get(cols["Status"]),
                "assigned": vals.get(cols["Person"]) or "nobody", "deadline": vals.get(cols["Deadline"]) or "none",
                "notes": " ".join(SOURCE_KEY_RE.sub("", notes).split())[:NOTE_PREVIEW],
                "from": ", ".join(f"{src} conversation {conv}" for src, conv, _ in item_keys) or "unknown",
            }
    return {"board_id": HUB["tasks_board"], "cols": cols,
            "group_id": groups.get(TASK_GROUP), "done_group_id": groups.get(DONE_GROUP),
            "upcoming_group_id": groups.get(UPCOMING_GROUP),
            "keys": keys, "open_tasks": open_tasks, "updatable": updatable}


def property_catalog(country):
    """Properties Master rows: [{"id", "name", "address", "hospitable_id"}]."""
    cols, _ = board_layout(WORKSPACES[country]["properties_board"])
    wanted = {t: cols[t] for t in ("Address", "Hospitable ID", "Hospitable Name") if t in cols}
    rows = []
    for it in board_items(WORKSPACES[country]["properties_board"], list(wanted.values())):
        if (it.get("group") or {}).get("title", "").lower().startswith("inactive"):
            continue
        vals = {c["id"]: (c["text"] or "").strip() for c in it["column_values"]}
        rows.append({"id": int(it["id"]), "name": it["name"],
                     "address": vals.get(wanted.get("Address"), "") or vals.get(wanted.get("Hospitable Name"), ""),
                     "hospitable_id": vals.get(wanted.get("Hospitable ID"), "")})
    return rows


def local_today(now):
    return dt.datetime.fromtimestamp(now, LOCAL_TZ).date()


def clean_deadline(value, today):
    """Claude's deadline as a date string, never in the past; a few days out if it can't be read."""
    try:
        day = dt.date.fromisoformat((value or "").strip())
    except ValueError:
        day = today + dt.timedelta(days=DEFAULT_DEADLINE_DAYS)
    return max(day, today).isoformat()


def stay_schedule(task, today):
    """(deadline, hold) for an early check-in / late checkout request: it's due the day before the
    stay date, and held in the upcoming group until then. (None, False) for other tasks."""
    try:
        stay = dt.date.fromisoformat((task.get("stay_date") or "").strip())
    except ValueError:
        return None, False
    show = stay - dt.timedelta(days=1)
    return max(show, today).isoformat(), show > today


def release_upcoming(today):
    """Move held check-in/out requests whose day has come (deadline today or earlier) to the due group."""
    cols, groups = board_layout(HUB["tasks_board"])
    held, due = groups.get(UPCOMING_GROUP), groups.get(DUE_GROUP) or groups.get(TASK_GROUP)
    if not held or "Deadline" not in cols:
        return []
    data = monday("""query($id:[ID!], $group:[String], $cols:[String!]){boards(ids:$id){groups(ids:$group){
        items_page(limit:500){items{id name column_values(ids:$cols){text}}}}}}""",
                  {"id": [HUB["tasks_board"]], "group": [held], "cols": [cols["Deadline"]]})
    released = []
    for it in data["boards"][0]["groups"][0]["items_page"]["items"]:
        deadline = (it["column_values"][0]["text"] if it["column_values"] else "") or ""
        if deadline[:10] > today.isoformat():
            continue
        if not DRY_RUN:
            monday("mutation($item:ID!, $group:String!){move_item_to_group(item_id:$item, group_id:$group){id}}",
                   {"item": it["id"], "group": due})
        released.append(it["name"])
    return released


def task_properties(task, country, catalog, hospitable_property_id=None):
    """Properties Master item ids for a task: the reservation's property for guest tasks, else Claude's pick."""
    rows = catalog.get(country) or []
    if hospitable_property_id:
        match = [r["id"] for r in rows if r["hospitable_id"] == hospitable_property_id]
        if match:
            return match[:1]
    valid = {r["id"] for r in rows}
    return [i for i in dict.fromkeys(task.get("property_ids") or []) if i in valid]


def column_values(task, cols, valid_user_ids, notes, property_ids=(), country=None):
    vals = {
        cols["Status"]: {"label": NEW_STATUS},
        cols["Notes/Updates"]: notes,
    }
    if country and COUNTRY_COLUMN in cols:
        vals[cols[COUNTRY_COLUMN]] = {"label": country}
    if task.get("category") in CATEGORIES and CATEGORY_COLUMN in cols:
        vals[cols[CATEGORY_COLUMN]] = {"label": task["category"]}
    if property_ids and "Property Link" in cols:
        vals[cols["Property Link"]] = {"item_ids": list(property_ids)}
    people = [i for i in dict.fromkeys(task["assignee_ids"]) if i in valid_user_ids]
    if people:
        vals[cols["Person"]] = {"personsAndTeams": [{"id": i, "kind": "person"} for i in people]}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", task.get("deadline") or ""):
        vals[cols["Deadline"]] = {"date": task["deadline"]}
    return vals


def user_ids_by_first_name(m_users):
    ids = {}
    for u in m_users:
        words = (u["name"] or "").split()
        if words:
            ids.setdefault(words[0].lower(), u["id"])
    return ids


def assign_coverage(task, m_users, today):
    """Split customer support between the support team, then hand work to whoever is on shift today."""
    ids = user_ids_by_first_name(m_users)
    people = list(task["assignee_ids"])
    support = [ids[n.lower()] for n in SUPPORT_TEAM if n.lower() in ids]
    if task.get("category") == SUPPORT_CATEGORY and support:
        # crc32, not hash(): it must give the same person for a conversation on every run
        people = [support[zlib.crc32(task["source_conversation"].encode()) % len(support)]]
    for name, (days, cover) in DAYS_OFF.items():
        off, on = ids.get(name.lower()), ids.get(cover.lower())
        if today.weekday() in days and off in people and on:
            people = [on if i == off else i for i in people]
    return list(dict.fromkeys(people))


def build_notes(task, source_line):
    key = f"[{task['source']} {task['source_conversation']} {task['source_message']}]"
    tail = f"\n{source_line}\n{key}" if source_line else f"\n{key}"
    return task["details"].strip()[: NOTES_LIMIT - len(tail)] + tail


def create_item(board, task, valid_user_ids, source_line, property_ids=(), country=None, group_id=None):
    vals = column_values(task, board["cols"], valid_user_ids, build_notes(task, source_line), property_ids, country)
    data = monday("""mutation($board:ID!, $group:String, $name:String!, $vals:JSON!){
        create_item(board_id:$board, group_id:$group, item_name:$name, column_values:$vals){id}}""",
                  {"board": board["board_id"], "group": group_id or board["group_id"], "name": task["title"][:255],
                   "vals": json.dumps(vals)})
    return data["create_item"]["id"]


def apply_task_update(board, item_id, action, comment, deadline):
    """Change a task's status/group/deadline and leave a comment saying why."""
    cols, vals = board["cols"], {}
    if action == "done":
        vals[cols["Status"]] = {"label": DONE_STATUS}
    elif action == "in_progress":
        vals[cols["Status"]] = {"label": PROGRESS_STATUS}
    if deadline:
        vals[cols["Deadline"]] = {"date": deadline}
    if vals:
        monday("""mutation($board:ID!, $item:ID!, $vals:JSON!){
            change_multiple_column_values(board_id:$board, item_id:$item, column_values:$vals){id}}""",
               {"board": board["board_id"], "item": item_id, "vals": json.dumps(vals)})
    if action == "done" and board.get("done_group_id"):
        monday("mutation($item:ID!, $group:String!){move_item_to_group(item_id:$item, group_id:$group){id}}",
               {"item": item_id, "group": board["done_group_id"]})
    monday("mutation($item:ID!, $body:String!){create_update(item_id:$item, body:$body){id}}",
           {"item": item_id, "body": comment.strip()})


# ------------------------------------------------------------------------- Claude

def team_list(m_users, s_users):
    by_email = {u["email"]: u for u in s_users.values() if u["email"]}
    lines = []
    for u in m_users:
        s = by_email.get(u["email"])
        slack_name = f", Slack name: {s['name']}" if s and s["name"] != u["name"] else ""
        lines.append(f"- monday id {u['id']}: {u['name']}{slack_name}")
    return "\n".join(lines)


def build_reference(team, catalog):
    """Team and property lists. They rarely change, so they're cached with the system prompt."""
    parts = ["Team (monday users you can assign):", team, ""]
    for country, rows in catalog.items():
        parts.append(f"{country} properties (Properties Master id: name, address):")
        parts += [f"- {r['id']}: {r['name']}" + (f", {r['address']}" if r["address"] else "") for r in rows] or ["(none listed)"]
        parts.append("")
    return "\n".join(parts)


def build_prompt(transcripts, open_tasks, now, updatable=()):
    """(open tasks, conversations) user blocks. The open tasks change only when a task does, so
    they get their own cache breakpoint; the conversations and the time come last, uncached."""
    parts = ["Open tasks already on the boards (don't add these again):",
             "\n".join(open_tasks[-300:]) or "(none)", "",
             "Open one-off tasks, also already on the boards (don't add these again), that you can update (task_updates):"]
    parts += [f"- id {t['id']} [{t['country']}] {t['name']} | status {t['status'] or 'none'} | assigned {t['assigned']} | "
              f"deadline {t['deadline']} | from {t['from']} | notes: {t['notes']}" for t in updatable] or ["(none)"]
    local = dt.datetime.fromtimestamp(now, LOCAL_TZ)
    convo = [f"Current time: {fmt_ts(now)} ({local.strftime('%A %Y-%m-%d %H:%M')} Eastern). "
             f"Today is {local.date().isoformat()}.", "", "Conversations:"]
    for header, lines in transcripts:
        convo += ["", header, *lines]
    return "\n".join(parts), "\n".join(convo)


# Calls are usually more than 5 minutes apart, so the default cache would expire between them.
CACHE = {"type": "ephemeral", "ttl": "1h"}


def extract_tasks(client, reference, board_text, convo_text):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT},
                {"type": "text", "text": reference, "cache_control": CACHE}],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": board_text, "cache_control": CACHE},
            {"type": "text", "text": convo_text},
        ]}],
    )
    u = response.usage
    usage = (f"Claude tokens: {u.input_tokens} uncached + {u.cache_read_input_tokens or 0} cache read + "
             f"{u.cache_creation_input_tokens or 0} cache write in, {u.output_tokens} out")
    log(usage)
    if response.stop_reason == "refusal":
        log("! Claude declined this batch")
        return [], [], usage
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens); the checkpoint was not moved")
    text = "".join(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return data["tasks"], data.get("task_updates") or [], usage


# --------------------------------------------------------------------------- main

def annotate(title, lines, chunk_chars=3500, max_chunks=9):
    """Post the report as run annotations (GitHub cuts each one at ~4 KB, so split it)."""
    if not os.environ.get("GITHUB_ACTIONS") or not lines:
        return
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > chunk_chars:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks[:max_chunks], 1):
        body = chunk.rstrip().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        suffix = f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""
        log(f"::notice title={title}{suffix}::{body}")


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
    h_checkpoint = float(state.get("hospitable_checkpoint") or checkpoint)
    q_checkpoint = float(state.get("quo_checkpoint") or checkpoint)
    if os.environ.get("LOOKBACK_HOURS"):
        checkpoint = h_checkpoint = q_checkpoint = now - float(os.environ["LOOKBACK_HOURS"]) * 3600
    new_state = {"checkpoint": now, "hospitable_checkpoint": h_checkpoint, "quo_checkpoint": q_checkpoint}
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
            transcripts.append((f"Slack #{chan['name']} (source slack, conversation id {chan['id']})", lines))
            new_total += new_count
            log(f"#{chan['name']}: {new_count} new message(s)")

    guests = {}
    if os.environ.get("HOSPITABLE_API_TOKEN"):
        log(f"Reading guest conversations since {fmt_ts(h_checkpoint)}")
        try:
            g_transcripts, g_new, guests = read_hospitable(h_checkpoint, now)
            transcripts += g_transcripts
            new_total += g_new
            new_state["hospitable_checkpoint"] = now
        except (RuntimeError, OSError) as e:
            # Keep the Hospitable checkpoint so these conversations are read again next run.
            log(f"! Hospitable: {e}")
            if os.environ.get("GITHUB_ACTIONS"):
                log(f"::warning title=Guest conversations skipped::{str(e)[:900]}".replace("\n", " "))
    else:
        log("HOSPITABLE_API_TOKEN not set: guest conversations skipped")

    quo_convos = {}
    if os.environ.get("QUO_API_KEY"):
        log(f"Reading Quo texts since {fmt_ts(q_checkpoint)}")
        try:
            q_transcripts, q_new, quo_convos = read_quo(q_checkpoint, now)
            transcripts += q_transcripts
            new_total += q_new
            new_state["quo_checkpoint"] = now
        except (RuntimeError, OSError) as e:
            log(f"! Quo: {e}")
            if os.environ.get("GITHUB_ACTIONS"):
                log(f"::warning title=Quo skipped::{str(e)[:900]}".replace("\n", " "))
    else:
        log("QUO_API_KEY not set: Quo texts skipped")

    try:
        for name in release_upcoming(local_today(now)):
            log(f"{'~ would show' if DRY_RUN else '> showing'} check-in/out request: {name}")
    except (RuntimeError, OSError) as e:
        log(f"! Couldn't release upcoming check-in/out requests: {e}")

    if not new_total:
        log("No new messages.")
        if not DRY_RUN:
            save_state(STATE_FILE, new_state)
        return

    board = load_board()
    m_users = monday_users()
    valid_ids = {u["id"] for u in m_users}
    catalog = {c: property_catalog(c) for c in WORKSPACES}
    today = local_today(now)
    open_tasks, updatable = board["open_tasks"], board["updatable"]

    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    client = anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
    )
    board_text, convo_text = build_prompt(transcripts, open_tasks, now,
                                          sorted(updatable.values(), key=lambda t: t["id"]))
    tasks, updates, usage = extract_tasks(client, build_reference(team_list(m_users, s_users), catalog),
                                   board_text, convo_text)
    log(f"Claude found {len(tasks)} task(s) and {len(updates)} update(s) to open tasks")

    seen = board["keys"]
    names = {u["id"]: u["name"] for u in m_users}
    created, report, skipped = 0, [], 0
    for task in tasks:
        key = (task["source"], task["source_conversation"], task["source_message"])
        if key in seen:
            log(f"= already on monday: {task['title']}")
            skipped += 1
            continue
        seen.add(key)
        guest = (guests.get(task["source_conversation"]) if task["source"] == "hospitable"
                 else quo_convos.get(task["source_conversation"]) if task["source"] == "quo" else None)
        country = guest["country"] if guest and guest["country"] in WORKSPACES else task["country"]
        country = country if country in WORKSPACES else DEFAULT_COUNTRY
        task["deadline"] = clean_deadline(task.get("deadline"), today)
        stay_deadline, hold = stay_schedule(task, today)
        task["deadline"] = stay_deadline or task["deadline"]
        group_id = board["upcoming_group_id"] if hold else None
        task["assignee_ids"] = assign_coverage(task, m_users, today)
        prop_ids = task_properties(task, country, catalog, (guest or {}).get("hospitable_property_id"))
        prop_names = {r["id"]: r["name"] for r in catalog.get(country) or []}
        where = ", ".join(prop_names[i] for i in prop_ids) or "no property"
        who = ", ".join(names.get(i, str(i)) for i in task["assignee_ids"] if i in valid_ids) or "unassigned"
        label = (f"[{country}] [{task.get('category', '?')}] {task['title']} @ {where} -> {who} (due {task['deadline']})"
                 + (" [held until the day before]" if group_id else ""))
        if DRY_RUN:
            log(f"~ {label}\n    {task['details']}")
            report.append(f"WOULD ADD {label}")
            continue
        if task["source"] == "hospitable":
            source_line = f"Hospitable: {guest['label']}" if guest else "Hospitable guest conversation"
        elif task["source"] == "quo":
            source_line = f"Quo: {guest['label']}" if guest else "Quo conversation"
        else:
            link = permalink_for(team_url, task["source_conversation"], task["source_message"])
            source_line = f"Slack: {link}" if link else ""
        item_id = create_item(board, task, valid_ids, source_line, prop_ids, country, group_id)
        created += 1
        log(f"+ {label} (item {item_id})")
        report.append(f"ADDED {label}")

    changed, handled = 0, set()
    verbs = {"done": "DONE", "in_progress": "IN PROGRESS", "note": "NOTE"}
    for up in updates:
        target = updatable.get(up["item_id"])
        if not target or up["item_id"] in handled:
            log(f"! update for unknown or repeated task {up['item_id']} ignored")
            continue
        handled.add(up["item_id"])
        deadline = ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (up.get("new_deadline") or "").strip()):
            deadline = clean_deadline(up["new_deadline"], today)
        label = (f"{'WOULD MARK ' if DRY_RUN else ''}{verbs[up['action']]} [{target['country']}] {target['name']}"
                 + (f" (due {deadline})" if deadline else "") + f": {up['comment']}")
        report.append(label)
        if DRY_RUN:
            log(f"~ {label}")
            continue
        apply_task_update(board, up["item_id"], up["action"], up["comment"], deadline)
        changed += 1
        log(f"* {label}")

    if not DRY_RUN:
        save_state(STATE_FILE, new_state)
    summary = (f"{new_total} new message(s), {len(tasks)} task(s) found, {skipped} already on monday, "
               f"{created} created, {len(handled)} open task(s) updated" + ("" if DRY_RUN else f" ({changed} applied)"))
    log(f"\nDone. {summary}")
    annotate(f"Tasks{' (dry run)' if DRY_RUN else ''}", [summary, usage, *report])


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=Task scan failed::{e.code}")
        raise
    except Exception as e:
        if os.environ.get("GITHUB_ACTIONS"):
            log(f"::error title=Task scan failed::{type(e).__name__}: {str(e)[:900]}".replace("\n", " "))
        raise
