import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import purchase_requests as p  # noqa: E402

CANADA = [{"id": "1", "name": "1456 Evans Blvd"}, {"id": "2", "name": "306 Spruce Main"},
          {"id": "3", "name": "306 Spruce Upstairs"}, {"id": "4", "name": "600 Sarnia Rd"}]


def sub(**kw):
    names = {"supplies": p.S_SUPPLIES, "bedding": p.S_BEDDING, "form": p.S_FORM_TYPE, "urgent": p.S_URGENT,
             "purchased": p.S_PURCHASED, "bought": p.S_ITEMS_PURCHASED, "amount": p.S_AMOUNT,
             "notes": p.S_NOTES, "receipts": p.S_RECEIPTS}
    return {names[k]: v for k, v in kw.items()}


CLEANING = "Cleaning Form (Cleaning Completion Report)"
URGENT = "Urgent Maintenance / Supplies Requests Form (Anything that requires immediate attention)"


class ParseRequestTests(unittest.TestCase):
    def test_nothing_needed_is_skipped(self):
        self.assertIsNone(p.parse_request(sub(form=CLEANING, supplies="NO RESTOCK NEEDED", bedding="No")))
        self.assertIsNone(p.parse_request(sub(form=CLEANING, supplies=None, bedding="n/a", notes="All done")))
        self.assertEqual(p.parse_request(sub(form=CLEANING, supplies="Toilet paper", bedding="Nk"))["items"],
                         ["Toilet paper"])

    def test_supplies_needed(self):
        req = p.parse_request(sub(form=CLEANING, supplies="Toilet paper, Shampoo", bedding="No"))
        self.assertEqual(req["type"], p.TYPE_SUPPLIES)
        self.assertEqual(req["summary"], "Toilet paper, Shampoo")
        self.assertEqual(req["items"], ["Toilet paper", "Shampoo"])
        self.assertNotIn("Toilet paper", req["details"])

    def test_bedding_only(self):
        req = p.parse_request(sub(form=CLEANING, supplies="NO RESTOCK NEEDED", bedding="King size bedsheets"))
        self.assertEqual(req["type"], p.TYPE_SUPPLIES)
        self.assertEqual(req["summary"], "bedding (King size bedsheets)")

    def test_purchase_made(self):
        req = p.parse_request(sub(form="Service Completion Form", purchased="Yes", bought="Coffee pods",
                                  amount="12.79", receipts="https://x/IMG.jpeg"))
        self.assertEqual(req["type"], p.TYPE_PURCHASE)
        self.assertEqual(req["amount"], 12.79)
        self.assertIn("Receipts: https://x/IMG.jpeg", req["details"])
        self.assertEqual(req["details"], "Receipts: https://x/IMG.jpeg")
        self.assertEqual(p.item_name("Spruce Main 306", "Falis"), "Spruce Main 306")

    def test_purchase_no_is_skipped(self):
        self.assertIsNone(p.parse_request(sub(form="Service Completion Form", purchased="No")))

    def test_urgent_form(self):
        req = p.parse_request(sub(form=URGENT, urgent="Dish pods for dishwasher"))
        self.assertEqual(req["type"], p.TYPE_URGENT)
        self.assertEqual(p.item_name("Evans 1456"), "Evans 1456")

    def test_name_is_only_the_property(self):
        self.assertEqual(p.item_name("Others (Please specify on the description)", "Falis"), "Other property (Falis)")
        self.assertEqual(p.item_name("", ""), "Other property")

    def test_purchase_without_items_listed(self):
        req = p.parse_request(sub(form="Service", purchased="Yes", amount="20"))
        self.assertEqual(req["items"], ["Items not listed (see receipts)"])


class SplitItemsTests(unittest.TestCase):
    def test_split(self):
        self.assertEqual(p.split_items("Toilet paper, paper towel, and coffee"), ["Toilet paper", "Paper towel", "Coffee"])
        self.assertEqual(p.split_items("Fabreeze and door mat"), ["Fabreeze", "Door mat"])
        self.assertEqual(p.split_items("Toilet papers \nBody wash"), ["Toilet papers", "Body wash"])
        self.assertEqual(p.split_items("$20 cash to wash the laundry"), ["$20 cash to wash the laundry"])
        self.assertEqual(p.split_items(""), [])

    def test_request_items(self):
        req = p.parse_request(sub(form=CLEANING, supplies="Toilet paper, Shampoo, NO RESTOCK NEEDED",
                                  bedding="Queen duvet cover"))
        self.assertEqual(req["items"], ["Toilet paper", "Shampoo", "Bedding: Queen duvet cover"])
        req = p.parse_request(sub(form="Service", purchased="Yes", bought="Dish soap, dish rack base, laundry soap"))
        self.assertEqual(req["items"], ["Dish soap", "Dish rack base", "Laundry soap"])
        req = p.parse_request(sub(form=URGENT, urgent="Dish pods for dishwasher"))
        self.assertEqual(req["items"], ["Dish pods for dishwasher"])


class PropertyMatchTests(unittest.TestCase):
    def test_matches(self):
        self.assertEqual(p.property_match("Evans 1456", CANADA), 1)
        self.assertEqual(p.property_match("Spruce Upstairs 306", CANADA), 3)
        self.assertEqual(p.property_match("Sarnia 600 (Unit 44)", CANADA), 4)

    def test_no_match(self):
        self.assertIsNone(p.property_match("Others (Please specify on the description)", CANADA))
        self.assertIsNone(p.property_match("King 380 (Penthouse)", CANADA))
        self.assertIsNone(p.property_match("", CANADA))


if __name__ == "__main__":
    unittest.main()
