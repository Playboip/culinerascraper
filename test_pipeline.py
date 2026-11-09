"""Offline synthetic fixtures only. These names are never discovery inputs."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock
import imagehash
from PIL import Image
import alberta_culinary_scraper as s


class PipelineTests(unittest.TestCase):
    def candidate(self):
        return {
            "name": "Synthetic Test Kitchen",
            "city": "Canmore",
            "google_place_id": "fixture-only",
            "google_status": "OPERATIONAL",
            "google_rating": 4.4,
            "google_reviews": 25,
            "primary_type": "Restaurant",
            "types": ["restaurant"],
            "categories": [],
            "website": "https://official.example",
            "official_verified": True,
            "evidence": [
                {
                    "url": "https://news.example/article",
                    "source": "Synthetic News",
                    "quote": "Synthetic Test Kitchen offers a seasonal tasting menu in Canmore.",
                    "published": datetime.now(timezone.utc).isoformat(),
                    "opening_status": "unknown",
                }
            ],
        }

    def test_distinctive_candidate(self):
        self.assertTrue(s.eligible(self.candidate())[0])

    def test_small_town_alone_is_not_distinctive(self):
        c = self.candidate()
        c["evidence"] = []
        self.assertFalse(s.eligible(c)[0])

    def test_chain(self):
        c = self.candidate()
        c["name"] = "Tim Hortons Canmore"
        self.assertEqual(s.eligible(c), (False, "chain"))

    def test_no_substring_chain_false_positive(self):
        c = self.candidate()
        c["name"] = "Pearls Test Kitchen"
        self.assertTrue(s.eligible(c)[0])

    def test_diner(self):
        c = self.candidate()
        c["categories"] = ["Diners"]
        self.assertEqual(s.eligible(c), (False, "excluded format"))

    def test_diner_elsewhere_in_news_does_not_reject(self):
        c = self.candidate()
        c["evidence"][0]["quote"] += " A different diner is nearby."
        self.assertTrue(s.eligible(c)[0])

    def test_review_count(self):
        c = self.candidate()
        c["google_reviews"] = 9
        self.assertFalse(s.eligible(c)[0])

    def test_do_not_mix_provider_rating_and_review_count(self):
        c = self.candidate()
        c.update(google_rating=4.8, google_reviews=2, yelp_rating=3.1, yelp_reviews=300)
        self.assertFalse(s.rating_gate(c))

    def test_verified_new_exception(self):
        c = self.candidate()
        c.update(google_reviews=2, google_rating=None)
        c["evidence"][0].update(
            opening_status="open",
            quote="Synthetic Test Kitchen is now open in Canmore.",
        )
        self.assertTrue(s.new_exception(c))
        self.assertTrue(s.eligible(c)[0])

    def test_new_opening_requires_official_verification(self):
        c = self.candidate()
        c["google_reviews"] = 2
        c["official_verified"] = False
        c["evidence"][0]["opening_status"] = "open"
        self.assertFalse(s.new_exception(c))

    def test_new_opening_requires_independent_news(self):
        c = self.candidate()
        c["google_reviews"] = 2
        c["evidence"][0].update(
            opening_status="open", url="https://official.example/news"
        )
        self.assertFalse(s.new_exception(c))

    def test_old_news_not_new_exception(self):
        c = self.candidate()
        c["google_reviews"] = 2
        c["evidence"][0].update(
            opening_status="open",
            published=(datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),
        )
        self.assertFalse(s.new_exception(c))

    def test_undated_news_not_new_exception(self):
        c = self.candidate()
        c["google_reviews"] = 2
        c["evidence"][0].update(opening_status="open", published="")
        self.assertFalse(s.new_exception(c))

    def test_future_opening_not_current(self):
        c = self.candidate()
        c["google_status"] = "FUTURE_OPENING"
        self.assertFalse(s.eligible(c)[0])

    def test_closed(self):
        c = self.candidate()
        c["google_status"] = "CLOSED_TEMPORARILY"
        self.assertFalse(s.eligible(c)[0])

    def test_truck_requires_official(self):
        c = self.candidate()
        c.update(kind="food_truck", official_verified=False)
        self.assertFalse(s.eligible(c)[0])

    def test_truck_verified(self):
        c = self.candidate()
        c["kind"] = "food_truck"
        self.assertTrue(s.eligible(c)[0])

    def test_duplicate_city_name(self):
        c = self.candidate()
        self.assertTrue(s.duplicate(c, [{"name": c["name"], "city": "CANMORE"}]))

    def test_same_name_different_city(self):
        c = self.candidate()
        self.assertFalse(s.duplicate(c, [{"name": c["name"], "city": "Calgary"}]))

    def test_copy_sequence_rejected(self):
        text = "These exact words form a long sequence taken from a source article."
        self.assertTrue(s.shares_sequence(text, [text]))

    def test_original_sequence(self):
        self.assertFalse(
            s.shares_sequence(
                "A different sentence about a culinary stop.",
                ["The source text discusses another subject entirely."],
            )
        )

    def test_dry_run_never_posts(self):
        with patch.object(s, "DRY_RUN", True), patch.object(s.HTTP, "post") as post:
            self.assertTrue(s.push([{"name": "fixture"}])["dry_run"])
            post.assert_not_called()

    def test_live_lock(self):
        with patch.object(s, "DRY_RUN", False), patch.object(
            s, "DISCOVERY_ONLY", False
        ), patch.object(s, "LIVE_ENABLED", False), patch.object(s.HTTP, "post") as post:
            with self.assertRaises(s.PipelineError):
                s.push([{}])
            post.assert_not_called()

    def test_bad_provider_auth_is_not_green_empty_success(self):
        r = Mock(status_code=403)
        with patch.object(s.HTTP, "request", return_value=r), patch.dict(
            s.METRICS, provider_requests=0
        ):
            with self.assertRaises(s.PipelineError):
                s.provider_request("Google", "GET", "https://api.example")

    def test_corrupt_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s, "STATE_DIR", Path(tmp)
        ):
            Path(tmp, "history.json").write_text("broken")
            with self.assertRaises(s.PipelineError):
                s.load_state()

    def test_literal_extraction_only(self):
        d = {
            "source_name": "Fixture",
            "title": "Test",
            "text": "Synthetic Test Kitchen is now open in Canmore.",
            "url": "https://news.example/test",
            "published": datetime.now(timezone.utc).isoformat(),
            "links": [],
        }
        r = {
            "leads": [
                {
                    "item": 0,
                    "name": "Invented Name",
                    "quote": "Invented Name is open",
                    "city": "Canmore",
                    "opening_status": "open",
                }
            ]
        }
        with patch.object(s, "OPENAI_API_KEY", "fixture-not-a-key"), patch.object(
            s, "ai_json", return_value=r
        ):
            leads, held = s.extract_leads([d])
            self.assertEqual(leads, [])
            self.assertTrue(held)

    def test_literal_name_extraction(self):
        text = "Synthetic Test Kitchen is now open in Canmore."
        d = {
            "source_name": "Fixture",
            "title": "Test",
            "text": text,
            "url": "https://news.example/test",
            "published": datetime.now(timezone.utc).isoformat(),
            "links": [],
        }
        r = {
            "leads": [
                {
                    "item": 0,
                    "name": "Synthetic Test Kitchen",
                    "quote": text,
                    "city": "Canmore",
                    "opening_status": "open",
                }
            ]
        }
        with patch.object(s, "OPENAI_API_KEY", "fixture-not-a-key"), patch.object(
            s, "ai_json", return_value=r
        ):
            leads, _ = s.extract_leads([d])
            self.assertEqual(len(leads), 1)
            self.assertTrue(s.is_new(leads[0]))

    def test_image_phash_duplicate(self):
        h = imagehash.phash(Image.new("RGB", (800, 600), (120, 80, 40)))
        a = s.ImageFingerprint(
            "https://a.example/x.jpg", "https://a.example/x.jpg", h, 800, 600, b""
        )
        b = s.ImageFingerprint(
            "https://b.example/y.jpg", "https://b.example/y.jpg", h, 800, 600, b""
        )
        self.assertTrue(s.fingerprint_is_duplicate(b, [a], set()))

    def test_image_resize_url_duplicate(self):
        self.assertEqual(
            s.image_url_identity("https://a.example/x.jpg?w=800"),
            s.image_url_identity("https://a.example/x.jpg?w=1000"),
        )

    def test_unverified_official_image_held(self):
        c = self.candidate()
        c["official_verified"] = False
        with patch.object(s, "fetch_image_fingerprint") as fetch:
            self.assertIsNone(s.choose_image(c, [], set()))
            fetch.assert_not_called()


class MainFlowTests(unittest.TestCase):
    def test_full_preview_never_ingests_or_remembers_synthetic_submission(self):
        c = PipelineTests().candidate()
        image = {
            "url": "https://official.example/fixture.jpg",
            "provider": "official website",
            "source_page": "https://official.example",
            "attribution": "fixture only",
            "phash": "0000000000000000",
            "width": 800,
            "height": 600,
        }
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp, "report.json")
            state = Path(tmp, "state")
            response = Mock()
            response.json.return_value = []
            with patch.object(s, "REPORT_PATH", report), patch.object(
                s, "STATE_DIR", state
            ), patch.object(s, "DRY_RUN", True), patch.object(
                s, "DISCOVERY_ONLY", False
            ), patch.object(
                s, "OPENAI_API_KEY", "fixture"
            ), patch.object(
                s, "GOOGLE_MAPS_API_KEY", "fixture"
            ), patch.object(
                s, "collect_documents", return_value=([{"title": "fixture"}], {})
            ), patch.object(
                s, "extract_leads", return_value=([c], [])
            ), patch.object(
                s, "extra_discovery", return_value=[]
            ), patch.object(
                s.HTTP, "get", return_value=response
            ), patch.object(
                s, "verify", side_effect=lambda c: c
            ), patch.object(
                s, "existing_images", return_value=([], set(), [])
            ), patch.object(
                s, "choose_image", return_value=image
            ), patch.object(
                s,
                "descriptions",
                return_value={
                    s.key(
                        c
                    ): "A synthetic fixture description used only by an offline test and never submitted to a production service. This deliberately exercises the preview payload without making any claims about a real business."
                },
            ), patch.object(
                s.HTTP, "post"
            ) as post, patch.dict(
                s.METRICS, provider_requests=0, provider_successes=0
            ):
                self.assertEqual(s.main(), 0)
                post.assert_not_called()
            result = json.loads(report.read_text())
            self.assertEqual(result["status"], "preview_complete")
            self.assertEqual(len(result["candidates"]), 1)
            history = json.loads(Path(state, "history.json").read_text())
            self.assertEqual(history["submissions"], [])

    def test_unreadable_old_image_holds_even_verified_new_venue(self):
        c = PipelineTests().candidate()
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp, "report.json")
            response = Mock()
            response.json.return_value = []
            with patch.object(s, "REPORT_PATH", report), patch.object(
                s, "STATE_DIR", Path(tmp, "state")
            ), patch.object(s, "DRY_RUN", True), patch.object(
                s, "DISCOVERY_ONLY", False
            ), patch.object(
                s, "OPENAI_API_KEY", "fixture"
            ), patch.object(
                s, "GOOGLE_MAPS_API_KEY", "fixture"
            ), patch.object(
                s, "collect_documents", return_value=([{"title": "fixture"}], {})
            ), patch.object(
                s, "extract_leads", return_value=([c], [])
            ), patch.object(
                s, "extra_discovery", return_value=[]
            ), patch.object(
                s.HTTP, "get", return_value=response
            ), patch.object(
                s, "verify", side_effect=lambda c: c
            ), patch.object(
                s,
                "existing_images",
                return_value=([], set(), ["https://broken.example/old-photo"]),
            ), patch.object(
                s, "choose_image"
            ) as choose, patch.object(
                s.HTTP, "post"
            ) as post, patch.dict(
                s.METRICS, provider_requests=0, provider_successes=0
            ):
                self.assertEqual(s.main(), 0)
                choose.assert_not_called()
                post.assert_not_called()
            result = json.loads(report.read_text())
            self.assertEqual(result["status"], "no_ready_cards")
            self.assertFalse(result["image_uniqueness_complete"])
            self.assertIn("ZERO-image-duplicates", result["held"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
