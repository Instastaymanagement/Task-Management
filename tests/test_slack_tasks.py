import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import slack_tasks as s  # noqa: E402

USERS = {"U1": {"name": "Falis Ahmed", "email": "falis@x.com"}, "U2": {"name": "Terry", "email": ""}}
COLS = {"Person": "person", "Status": "status", "Deadline": "date_mm7ytd1z", "Notes/Updates": "text_mm7xv1hm"}


def task(**kw):
    base = {"title": "Fix sink", "details": "Kitchen sink leaks.", "assignee_ids": [11], "country": "Canada",
            "deadline": "", "source": "slack", "source_conversation": "C1", "source_message": "1760000000.000100"}
    base.update(kw)
    return base


class ReadableTests(unittest.TestCase):
    def test_mentions_channels_and_links(self):
        text = "<@U1> please check <#C9|maintenance> <https://a.com|form> <https://b.com> <!here>"
        self.assertEqual(s.readable(text, USERS),
                         "@Falis Ahmed please check #maintenance form (https://a.com) https://b.com @here")

    def test_unknown_user_keeps_id(self):
        self.assertEqual(s.readable("<@U404> hi", USERS), "@U404 hi")


class FilterTests(unittest.TestCase):
    def test_skips_joins_and_empty(self):
        self.assertFalse(s.is_conversation({"subtype": "channel_join", "text": "joined"}))
        self.assertFalse(s.is_conversation({"text": ""}))
        self.assertTrue(s.is_conversation({"text": "Can someone restock towels?"}))
        self.assertTrue(s.is_conversation({"subtype": "file_share", "text": "", "files": [{"name": "a.jpg"}]}))


class TranscriptTests(unittest.TestCase):
    def fake_paged(self, method, key, **params):
        if method == "conversations.history":
            return iter([
                {"ts": "100.0", "user": "U1", "text": "old message"},
                {"ts": "150.0", "user": "U1", "text": "thread parent", "reply_count": 1, "latest_reply": "250.0"},
                {"ts": "300.0", "user": "U2", "text": "new message"},
            ])
        return iter([{"ts": "150.0", "text": "thread parent"}, {"ts": "250.0", "user": "U2", "text": "on it"}])

    def test_marks_new_messages_and_new_replies(self):
        with mock.patch.object(s, "slack_paged", side_effect=self.fake_paged):
            lines, count = s.channel_transcript({"id": "C1"}, USERS, checkpoint=200.0, now=400.0)
        self.assertEqual(count, 2)
        self.assertTrue(lines[0].startswith("[context] id=100.0"))
        self.assertTrue(lines[1].startswith("[context] id=150.0"))
        self.assertIn("reply [NEW] id=250.0", lines[2])
        self.assertTrue(lines[3].startswith("[NEW] id=300.0"))


class GuestTranscriptTests(unittest.TestCase):
    res = {"id": "R1", "code": "HM1", "platform": "airbnb", "check_in": "2026-10-10T16:00:00-04:00",
           "check_out": "2026-10-20T10:00:00-04:00", "guest": {"first_name": "Brent", "last_name": "McKenzie"},
           "properties": [{"id": "P1"}]}
    messages = {"data": [  # Hospitable returns newest first
        {"id": 4, "created_at": "2026-10-15T12:13:21Z", "sender_type": "host", "source": "public_api",
         "author": None, "sender": {"full_name": "Mohamed A"}, "body": "Thanks, it's all set."},
        {"id": 3, "created_at": "2026-10-15T12:10:15Z", "sender_type": "guest", "source": "platform",
         "body": "The dishwasher\nis leaking.", "attachments": [{"url": "x"}]},
        {"id": 2, "created_at": "2026-10-14T09:00:00Z", "sender_type": "host", "source": "automated",
         "body": "Check-in reminder"},
        {"id": 1, "created_at": "2026-10-14T00:18:44Z", "sender_type": "host", "source": "AI",
         "author": {"name": "Ivy Monteagudo"}, "body": "Rescheduled for Thursday."},
    ]}

    def test_transcript(self):
        checkpoint = s.iso_ts("2026-10-15T12:00:00Z")
        with mock.patch.object(s, "hospitable", return_value=self.messages):
            lines, count = s.guest_transcript(self.res, {}, checkpoint, now=checkpoint + 3600)
        self.assertEqual(count, 2)
        self.assertEqual(len(lines), 3)  # automated reminder dropped
        self.assertIn("[context] id=1", lines[0])
        self.assertIn("Ivy Monteagudo (host): Rescheduled", lines[0])
        self.assertIn("[NEW] id=3", lines[1])
        self.assertIn("Brent McKenzie (guest): The dishwasher is leaking. [1 attachment(s)]", lines[1])
        self.assertIn("Mohamed A (host)", lines[2])

    def test_property_and_header(self):
        props = {"P1": {"name": "600 Sarnia Rd", "country": "Canada"}}
        prop = s.reservation_property(self.res, props)
        self.assertEqual(prop["country"], "Canada")
        header = s.guest_header(self.res, prop)
        self.assertIn("source hospitable, conversation id R1", header)
        self.assertIn("Brent McKenzie at 600 Sarnia Rd (Canada)", header)
        self.assertEqual(s.reservation_property({"properties": []}, props)["country"], "Unknown")


class QuoTests(unittest.TestCase):
    line = {"id": "PN1", "number": "+15195550100", "name": "Canada", "country": "Canada"}

    def test_parse_numbers(self):
        self.assertEqual(s.parse_quo_numbers("+1 (519) 555-0100=Canada, +16175550100=USA"),
                         {"+15195550100": "Canada", "+16175550100": "USA"})

    def test_lines_mapping_and_name_fallback(self):
        pages = [{"id": "PN1", "number": "+15195550100", "name": "Main"},
                 {"id": "PN2", "number": "+16175550100", "name": "Instastay USA"},
                 {"id": "PN3", "number": "+12125550100", "name": "Other"}]
        with mock.patch.object(s, "quo_pages", return_value=iter(pages)):
            lines = s.quo_lines({"+15195550100": "Canada"})
        self.assertEqual([l["country"] for l in lines], ["Canada", "USA", "Unknown"])

    def test_transcript_marks_new_texts(self):
        checkpoint = s.iso_ts("2026-10-15T12:00:00Z")
        msgs = [{"id": "AC2", "createdAt": "2026-10-15T12:05:00Z", "direction": "incoming",
                 "from": "+15195551234", "text": "The furnace\nis making noise"},
                {"id": "AC1", "createdAt": "2026-10-15T11:00:00Z", "direction": "outgoing", "text": "Hi!"},
                {"id": "AC0", "createdAt": "2026-10-15T10:00:00Z", "direction": "incoming", "text": "  "}]
        with mock.patch.object(s, "quo_pages", return_value=iter(msgs)):
            lines, count = s.quo_transcript(self.line, {"participants": ["+15195551234"]}, checkpoint, checkpoint + 600)
        self.assertEqual(count, 1)
        self.assertEqual(len(lines), 2)
        self.assertIn("[context] id=AC1", lines[0])
        self.assertIn("team: Hi!", lines[0])
        self.assertIn("[NEW] id=AC2", lines[1])
        self.assertIn("+15195551234: The furnace is making noise", lines[1])


class PropertyAndDeadlineTests(unittest.TestCase):
    catalog = {"Canada": [{"id": 501, "name": "600 Sarnia Rd", "address": "", "hospitable_id": "H-SARNIA"},
                          {"id": 502, "name": "1494 Commissioners Rd", "address": "", "hospitable_id": "H-COMM"}],
               "USA": [{"id": 601, "name": "22 Euston", "address": "", "hospitable_id": "H-EUSTON"}]}

    def test_guest_task_uses_reservation_property(self):
        ids = s.task_properties(task(property_ids=[501]), "Canada", self.catalog, "H-COMM")
        self.assertEqual(ids, [502])

    def test_claude_pick_is_checked_against_country(self):
        self.assertEqual(s.task_properties(task(property_ids=[501, 601, 501]), "Canada", self.catalog), [501])
        self.assertEqual(s.task_properties(task(property_ids=[]), "USA", self.catalog), [])

    def test_unknown_hospitable_property_falls_back_to_claude(self):
        self.assertEqual(s.task_properties(task(property_ids=[501]), "Canada", self.catalog, "H-NEW"), [501])

    def test_deadline(self):
        today = s.dt.date(2026, 10, 15)
        self.assertEqual(s.clean_deadline("2026-10-17", today), "2026-10-17")
        self.assertEqual(s.clean_deadline("2026-10-01", today), "2026-10-15")  # never in the past
        self.assertEqual(s.clean_deadline("", today), "2026-10-18")
        self.assertEqual(s.clean_deadline("soon", today), "2026-10-18")

    def test_property_link_value(self):
        cols = {**COLS, "Property Link": "board_relation_x"}
        vals = s.column_values(task(), cols, {11}, "n", [502])
        self.assertEqual(vals["board_relation_x"], {"item_ids": [502]})
        self.assertNotIn("board_relation_x", s.column_values(task(), cols, {11}, "n", []))


class TaskUpdateTests(unittest.TestCase):
    board = {"board_id": 1, "cols": {**COLS}, "done_group_id": "g_done"}

    def run_update(self, action, deadline=""):
        calls = []
        with mock.patch.object(s, "monday", side_effect=lambda q, v=None: calls.append((q, v)) or {}):
            s.apply_task_update(self.board, 42, action, "Ivy sent the parking rules.", deadline)
        return calls

    def test_done_sets_status_moves_and_comments(self):
        calls = self.run_update("done")
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(calls[0][1]["vals"]), {"status": {"label": "Done"}})
        self.assertIn("move_item_to_group", calls[1][0])
        self.assertEqual(calls[1][1], {"item": 42, "group": "g_done"})
        self.assertIn("create_update", calls[2][0])
        self.assertEqual(calls[2][1]["body"], "Ivy sent the parking rules.")

    def test_in_progress_with_new_deadline(self):
        calls = self.run_update("in_progress", "2026-10-20")
        self.assertEqual(json.loads(calls[0][1]["vals"]),
                         {"status": {"label": "Working on it"}, "date_mm7ytd1z": {"date": "2026-10-20"}})
        self.assertEqual(len(calls), 2)  # no move

    def test_note_only_comments(self):
        calls = self.run_update("note")
        self.assertEqual(len(calls), 1)
        self.assertIn("create_update", calls[0][0])

    def test_load_board_lists_only_open_one_off_tasks(self):
        items = [
            {"id": "1", "name": "Reply to Heather", "group": {"title": "Other task"},
             "column_values": [{"id": "status", "text": "Not done"}, {"id": "person", "text": "Anna"},
                               {"id": "date_mm7ytd1z", "text": "2026-10-16"},
                               {"id": "text_mm7xv1hm", "text": "Ask about parking\n[hospitable R1 55]"}]},
            {"id": "2", "name": "Daily check", "group": {"title": "Daily Checklist"},
             "column_values": [{"id": "status", "text": ""}, {"id": "text_mm7xv1hm", "text": ""}]},
            {"id": "3", "name": "Old", "group": {"title": "Completed task"},
             "column_values": [{"id": "status", "text": "Done"}, {"id": "text_mm7xv1hm", "text": "[slack C1 1.2]"}]},
        ]
        layout = ({"Person": "person", "Status": "status", "Deadline": "date_mm7ytd1z", "Notes/Updates": "text_mm7xv1hm"},
                  {"Other task": "g_other", "Completed task": "g_done"})
        with mock.patch.object(s, "board_layout", return_value=layout), mock.patch.object(s, "board_items", return_value=items):
            board = s.load_board("Canada")
        self.assertEqual(list(board["updatable"]), [1])
        t = board["updatable"][1]
        self.assertEqual(t["from"], "hospitable conversation R1")
        self.assertEqual(t["notes"], "Ask about parking")
        self.assertEqual(t["deadline"], "2026-10-16")
        self.assertEqual(len(board["open_tasks"]), 2)  # checklist still counts for duplicates
        self.assertEqual(board["keys"], {("hospitable", "R1", "55"), ("slack", "C1", "1.2")})
        self.assertEqual(board["done_group_id"], "g_done")


class MondayValueTests(unittest.TestCase):
    def test_column_values(self):
        vals = s.column_values(task(assignee_ids=[11, 11, 99], deadline="2026-10-20"), COLS, {11, 12}, "notes")
        self.assertEqual(vals["person"], {"personsAndTeams": [{"id": 11, "kind": "person"}]})
        self.assertEqual(vals["status"], {"label": "Not done"})
        self.assertEqual(vals["date_mm7ytd1z"], {"date": "2026-10-20"})
        self.assertEqual(vals["text_mm7xv1hm"], "notes")

    def test_no_person_or_bad_deadline(self):
        vals = s.column_values(task(assignee_ids=[], deadline="Friday"), COLS, {11}, "n")
        self.assertNotIn("person", vals)
        self.assertNotIn("date_mm7ytd1z", vals)

    def test_notes_carry_source_key(self):
        notes = s.build_notes(task(details="x" * 5000), "Slack: https://t.slack.com/archives/C1/p1760000000000100")
        self.assertLessEqual(len(notes), s.NOTES_LIMIT)
        self.assertEqual(s.SOURCE_KEY_RE.findall(notes), [("slack", "C1", "1760000000.000100")])

    def test_hospitable_source_key(self):
        notes = s.build_notes(task(source="hospitable", source_conversation="c83b8b9d-0992-4fa9", source_message="1303734654"),
                              "Hospitable: Brent McKenzie at 600 Sarnia Rd, reservation HMRX95F3M4")
        self.assertEqual(s.SOURCE_KEY_RE.findall(notes), [("hospitable", "c83b8b9d-0992-4fa9", "1303734654")])

    def test_permalink(self):
        self.assertEqual(s.permalink_for("https://insta.slack.com/", "C1", "1760000000.000100"),
                         "https://insta.slack.com/archives/C1/p1760000000000100")


class TeamListTests(unittest.TestCase):
    def test_matches_slack_names_by_email(self):
        team = s.team_list([{"id": 11, "name": "Falis", "email": "falis@x.com"},
                            {"id": 12, "name": "Anna", "email": "anna@x.com"}], USERS)
        self.assertEqual(team, "- monday id 11: Falis, Slack name: Falis Ahmed\n- monday id 12: Anna")


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict(self):
        item = s.OUTPUT_SCHEMA["properties"]["tasks"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), set(item["properties"]))
        json.dumps(s.OUTPUT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
