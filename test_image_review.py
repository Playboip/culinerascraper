"""Offline image-review fixtures; no real venue discovery or HTTP requests."""

import hashlib
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import imagehash
import alberta_culinary_scraper as s
import test_pipeline as pipeline_fixtures


class ImageReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(s.HTTP, "post", side_effect=AssertionError("No live writes"))
        )
        self.stack.enter_context(
            patch.object(
                s.HTTP,
                "request",
                side_effect=AssertionError("No live provider requests"),
            )
        )
        self.stack.enter_context(
            patch.dict(
                s.METRICS,
                {"warnings": [], "ai_calls": 0, "image_review_calls": 0},
                clear=False,
            )
        )
        self.fp = s.ImageFingerprint(
            "https://official.example/photo.jpg",
            "https://official.example/photo.jpg",
            imagehash.hex_to_hash("0123456789abcdef"),
            800,
            600,
            b"offline-not-an-image",
        )
        self.fetch = self.stack.enter_context(
            patch.object(s, "fetch_image_fingerprint", return_value=self.fp)
        )
        self.stack.enter_context(
            patch.object(
                s,
                "image_preview_data_url",
                return_value="data:image/jpeg;base64,offline",
            )
        )
        self.ai = self.stack.enter_context(
            patch.object(
                s,
                "ai_json",
                return_value={"accept": True, "subject": "food", "matches_dish": True},
            )
        )
        self.free = self.stack.enter_context(
            patch.object(
                s.free_dish_photos, "search_licensed_dish_images", return_value=[]
            )
        )
        self.menu = self.stack.enter_context(
            patch.object(s, "menu_dish_evidence", return_value=[])
        )
        self.c = pipeline_fixtures.PipelineTests().candidate()
        self.official = {
            "url": "https://official.example/photo.jpg",
            "provider": "official website",
            "source_page": "https://official.example",
            "rights_status": "claimed-owner",
            "licence_name": "invented fixture licence",
        }
        self.c["image_options"] = [self.official]
        self.evidence = {
            "dish": "Tomato soup",
            "source_url": "https://official.example/menu",
            "quote": "Tomato soup served daily.",
        }
        self.licensed = {
            "url": "https://upload.wikimedia.org/wikipedia/commons/fixture.jpg",
            "provider": "Wikimedia Commons",
            "source_page": "https://commons.wikimedia.org/wiki/File:Fixture.jpg",
            "rights_status": "source_licence_recorded",
            "licence_name": "CC BY 4.0",
            "licence_url": "https://creativecommons.org/licenses/by/4.0/",
            "licence_evidence": "Offline fixture licence statement",
            "attribution": "Fixture photographer — CC BY 4.0",
            "is_illustrative": True,
            "rights_holder": "Fixture photographer",
        }

    def test_official_photo_is_reviewable_not_claimed_licensed(self):
        used, ids = [], set()
        result = s.choose_image(self.c, used, ids)
        self.assertEqual(result["rights_status"], "unverified")
        self.assertEqual(result["licence_name"], "")
        self.assertEqual(result["rights_holder"], "")
        self.assertTrue(result["rights_review_required"])
        self.assertFalse(result["is_illustrative"])
        self.assertEqual(
            result["sha256"], hashlib.sha256(self.fp.bytes_data).hexdigest()
        )
        self.assertEqual(len(used), 1)
        self.free.assert_not_called()

    def test_wrong_official_source_domain_not_accepted(self):
        self.c["image_options"][0]["source_page"] = "https://another.example/"
        self.assertIsNone(s.choose_image(self.c, [], set()))
        self.fetch.assert_not_called()

    def test_exact_url_duplicate_rejected_before_download(self):
        result = s.review_photo(
            self.c, self.official, [], {"https://official.example/photo.jpg"}
        )
        self.assertIsNone(result)
        self.fetch.assert_not_called()
        self.ai.assert_not_called()

    def test_phash_duplicate_rejected_before_vision(self):
        self.assertIsNone(s.review_photo(self.c, self.official, [self.fp], set()))
        self.ai.assert_not_called()

    def test_accepted_photo_rejected_for_second_card_in_same_batch(self):
        used, ids = [], set()
        self.assertIsNotNone(s.review_photo(self.c, self.official, used, ids))
        self.assertIsNone(s.review_photo(self.c, self.official, used, ids))

    def test_failed_image_download_stays_held(self):
        self.fetch.return_value = None
        self.assertIsNone(s.choose_image(self.c, [], set()))
        self.ai.assert_not_called()

    def test_logo_or_uncertain_photo_rejected(self):
        self.ai.return_value = {"accept": False, "subject": "reject"}
        self.assertIsNone(s.review_photo(self.c, self.official, [], set()))

    def test_licensed_fallback_requires_literal_menu_evidence(self):
        self.assertIsNone(
            s.review_photo(self.c, self.licensed, [], set(), menu_evidence=None)
        )
        self.ai.assert_not_called()

    def test_licensed_fallback_requires_dish_visual_match(self):
        self.ai.return_value = {
            "accept": True,
            "subject": "food",
            "matches_dish": False,
        }
        self.assertIsNone(
            s.review_photo(
                self.c, self.licensed, [], set(), menu_evidence=self.evidence
            )
        )

    def test_fallback_is_labelled_illustrative_and_preserves_credit(self):
        self.c["image_options"] = []
        self.menu.return_value = [self.evidence]
        self.free.return_value = [self.licensed]
        result = s.choose_image(self.c, [], set())
        self.assertTrue(result["is_illustrative"])
        self.assertIn("not photographed at this venue", result["image_caption"])
        self.assertEqual(result["rights_status"], "source_licence_recorded")
        self.assertEqual(result["attribution"], self.licensed["attribution"])
        self.assertEqual(result["menu_evidence"], self.evidence)

    def test_unverified_generic_stock_fallback_rejected(self):
        self.licensed["rights_status"] = "unverified"
        self.assertIsNone(
            s.review_photo(
                self.c, self.licensed, [], set(), menu_evidence=self.evidence
            )
        )

    def test_image_review_budget_bounded(self):
        with patch.object(s, "MAX_IMAGE_REVIEWS", 0):
            self.assertIsNone(s.review_photo(self.c, self.official, [], set()))
            self.ai.assert_not_called()
            self.assertTrue(s.METRICS["warnings"])

    def test_no_menu_means_no_generic_food_search(self):
        self.c["image_options"] = []
        self.assertIsNone(s.choose_image(self.c, [], set()))
        self.free.assert_not_called()

    def test_payload_preserves_unknown_rights_and_incomplete_coverage(self):
        self.c["selected_image"] = s.review_photo(self.c, self.official, [], set())
        self.c["image_duplicate_coverage"] = {
            "scope": "published directory + current batch",
            "complete": False,
            "unreadable_existing_image_count": 20,
        }
        out = s.payload([self.c], {s.key(self.c): "Synthetic offline description."})[0]
        self.assertEqual(out["status"], "pending")
        self.assertTrue(out["manual_review_required"])
        self.assertEqual(out["image_rights_status"], "unverified")
        self.assertEqual(out["image_licence_name"], "")
        self.assertEqual(out["image_attribution"], "")
        self.assertFalse(out["image_review"]["client_duplicate_check"]["complete"])

    def test_payload_keeps_illustrative_label_and_menu_proof(self):
        self.c["selected_image"] = s.review_photo(
            self.c, self.licensed, [], set(), menu_evidence=self.evidence
        )
        out = s.payload([self.c], {s.key(self.c): "Synthetic offline description."})[0]
        self.assertTrue(out["image_is_illustrative"])
        self.assertEqual(
            out["image_caption"], "Illustrative image — not photographed at this venue"
        )
        self.assertEqual(out["image_licence_url"], self.licensed["licence_url"])
        self.assertEqual(out["image_review"]["menu_evidence"], self.evidence)

    def test_live_submission_waits_for_backend_metadata_support(self):
        with patch.object(s, "DRY_RUN", False), patch.object(
            s, "DISCOVERY_ONLY", False
        ), patch.object(s, "LIVE_ENABLED", True), patch.object(
            s, "IMAGE_REVIEW_FIELDS_ENABLED", False
        ):
            with self.assertRaisesRegex(
                s.PipelineError, "metadata support not confirmed"
            ):
                s.push([{"name": "offline"}])


class MenuEvidenceTests(unittest.TestCase):
    def test_invented_or_nonliteral_dish_is_rejected(self):
        c = pipeline_fixtures.PipelineTests().candidate()
        source = {
            "url": "https://official.example/menu",
            "text": "Tomato soup served daily.",
        }
        with patch.object(
            s, "official_menu_sources", return_value=[source]
        ), patch.object(
            s,
            "ai_json",
            return_value={
                "dishes": [
                    {
                        "dish": "Invented Lobster",
                        "source_url": source["url"],
                        "quote": "Invented Lobster is served.",
                    }
                ]
            },
        ):
            self.assertEqual(s.menu_dish_evidence(c), [])

    def test_literal_dish_is_accepted_with_source(self):
        c = pipeline_fixtures.PipelineTests().candidate()
        source = {
            "url": "https://official.example/menu",
            "text": "Tomato soup served daily.",
        }
        evidence = {
            "dish": "Tomato soup",
            "source_url": source["url"],
            "quote": source["text"],
        }
        with patch.object(
            s, "official_menu_sources", return_value=[source]
        ), patch.object(s, "ai_json", return_value={"dishes": [evidence]}):
            self.assertEqual(s.menu_dish_evidence(c), [evidence])

    def test_unrelated_source_url_rejected(self):
        c = pipeline_fixtures.PipelineTests().candidate()
        source = {
            "url": "https://official.example/menu",
            "text": "Tomato soup served daily.",
        }
        evidence = {
            "dish": "Tomato soup",
            "source_url": "https://unrelated.example/menu",
            "quote": source["text"],
        }
        with patch.object(
            s, "official_menu_sources", return_value=[source]
        ), patch.object(s, "ai_json", return_value={"dishes": [evidence]}):
            self.assertEqual(s.menu_dish_evidence(c), [])


if __name__ == "__main__":
    unittest.main()
