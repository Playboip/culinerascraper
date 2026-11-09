"""Offline regressions only; synthetic names must never reach live ingestion."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import alberta_culinary_scraper as s
from scrapling_discovery import _link_priority, _canonical_url


class PriorityAndEvidenceTests(unittest.TestCase):
    def test_actual_venue_detail_outranks_navigation(self):
        self.assertGreater(
            _link_priority(
                "https://visitreddeer.com/experience/fixture-venue/", "Fixture Venue"
            ),
            _link_priority(
                "https://visitreddeer.com/eat-drink/restaurants/", "Restaurants"
            ),
        )

    def test_credentialed_link_rejected(self):
        self.assertIsNone(_canonical_url("https://user:password@example.com"))

    def test_future_quotation_cannot_be_promoted_to_open_by_model(self):
        text = "Synthetic Test Kitchen has a grand opening tomorrow in Canmore."
        doc = {
            "source_name": "Fixture",
            "title": "Fixture",
            "text": text,
            "url": "https://news.example/fixture",
            "published": datetime.now(timezone.utc).isoformat(),
            "links": [],
        }
        result = {
            "leads": [
                {
                    "item": 0,
                    "name": "Synthetic Test Kitchen",
                    "city": "Canmore",
                    "quote": text,
                    "opening_status": "open",
                }
            ]
        }
        with patch.object(s, "OPENAI_API_KEY", "fixture-not-real"), patch.object(
            s, "ai_json", return_value=result
        ):
            leads, _ = s.extract_leads([doc])
        self.assertEqual(leads[0]["evidence"][0]["opening_status"], "announced")
        self.assertFalse(s.is_new(leads[0]))

    def test_missing_date_does_not_break_document_ordering(self):
        docs = [
            {
                "source_name": "Fixture",
                "title": "Test",
                "text": "No named venue in this synthetic article.",
                "url": "https://news.example/a",
                "published": None,
                "links": [],
            },
            {
                "source_name": "Fixture",
                "title": "Test2",
                "text": "No named venue in this second synthetic article.",
                "url": "https://news.example/b",
                "published": "2020-01-01",
                "links": [],
            },
        ]
        with patch.object(s, "OPENAI_API_KEY", "fixture-not-real"), patch.object(
            s, "ai_json", return_value={"leads": []}
        ):
            leads, _ = s.extract_leads(docs)
        self.assertEqual(leads, [])


if __name__ == "__main__":
    unittest.main()
