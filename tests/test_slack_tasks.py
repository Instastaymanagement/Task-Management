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
            "deadline": "", "source_channel": "C1", "source_ts": "1760000000.000100"}
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
        self.assertTrue(lines[0].startswith("[context] ts=100.0"))
        self.assertTrue(lines[1].startswith("[context] ts=150.0"))
        self.assertIn("reply [NEW] ts=250.0", lines[2])
        self.assertTrue(lines[3].startswith("[NEW] ts=300.0"))


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
        notes = s.build_notes(task(details="x" * 5000), "https://t.slack.com/archives/C1/p1760000000000100")
        self.assertLessEqual(len(notes), s.NOTES_LIMIT)
        self.assertEqual(s.SOURCE_KEY_RE.findall(notes), [("C1", "1760000000.000100")])

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
