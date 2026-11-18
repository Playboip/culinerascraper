"""Offline branch fixtures only; no discovery or ingestion of synthetic venues."""

import copy
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch
import alberta_culinary_scraper as s


class BranchMatchingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.google = self.stack.enter_context(
            patch.object(s, "google_search", return_value=[])
        )
        self.yelp = self.stack.enter_context(
            patch.object(s, "yelp_search", return_value=[])
        )
        self.official = self.stack.enter_context(
            patch.object(s, "fetch_official_page", return_value=None)
        )
        self.stack.enter_context(
            patch.object(
                s.HTTP,
                "request",
                side_effect=AssertionError("Live HTTP forbidden in branch tests"),
            )
        )
        self.stack.enter_context(
            patch.object(
                s.HTTP,
                "post",
                side_effect=AssertionError("Ingestion forbidden in branch tests"),
            )
        )
        self.stack.enter_context(
            patch.dict(
                s.METRICS,
                {
                    "provider_requests": 0,
                    "provider_successes": 0,
                    "provider_errors": [],
                    "provider_successes_by_provider": {},
                    "warnings": [],
                    "ai_calls": 0,
                },
                clear=True,
            )
        )

    def lead(self, quote=None):
        return {
            "name": "Synthetic Branch Kitchen",
            "city": "Edmonton",
            "kind": "restaurant",
            "evidence": [
                {
                    "url": "https://news.example/fixture",
                    "source": "Offline fixture",
                    "published": datetime.now(timezone.utc).isoformat(),
                    "quote": quote
                    or "Synthetic Branch Kitchen opened its south side location at 9204 23 Avenue NW in Edmonton, with a tasting menu.",
                    "opening_status": "open",
                }
            ],
        }

    def listing(
        self, address="9204 23 Ave NW, Edmonton, AB", count=2, place="fixture-south"
    ):
        return {
            "name": "Synthetic Branch Kitchen",
            "city": "Edmonton",
            "address": address,
            "google_place_id": place,
            "google_rating": 4.6,
            "google_reviews": count,
            "google_status": "OPERATIONAL",
            "primary_type": "Restaurant",
            "types": ["restaurant"],
            "website": "https://official.example/branch",
            "provider_official_url": "https://official.example/branch",
        }

    def page(self, address="9204 23 Avenue NW"):
        return {
            "title": "Synthetic Branch Kitchen",
            "text": "Synthetic Branch Kitchen in Edmonton. Visit " + address,
            "url": "https://official.example/branch",
            "image_options": [],
        }

    def verify_good(self, count=2):
        self.google.return_value = [self.listing(count=count)]
        self.official.return_value = self.page()
        return s.verify(self.lead())

    def test_street_suffix_and_ordinal_normalize(self):
        self.assertTrue(
            s.address_matches("9204 23rd Avenue NW", "9204 23 Ave NW, Edmonton, AB")
        )

    def test_named_street_suffix_normalizes(self):
        self.assertTrue(
            s.address_matches(
                "1103 Mayor Magrath Drive South",
                "1103 Mayor Magrath Dr S, Lethbridge, AB",
            )
        )

    def test_street_number_mismatch_fails(self):
        self.assertFalse(s.address_matches("9204 23 Avenue NW", "9205 23 Avenue NW"))

    def test_route_mismatch_fails(self):
        self.assertFalse(s.address_matches("9204 23 Avenue NW", "9204 24 Avenue NW"))

    def test_direction_mismatch_fails(self):
        self.assertFalse(s.address_matches("9204 23 Avenue NW", "9204 23 Avenue SW"))

    def test_source_direction_missing_in_provider_fails(self):
        self.assertFalse(s.address_matches("9204 23 Avenue NW", "9204 23 Avenue"))

    def test_leading_unit_is_not_street_number(self):
        parsed = s.street_addresses("Unit 18, 9204 23 Avenue NW")
        self.assertEqual(parsed[0]["signature"][0], "9204")
        self.assertTrue(
            s.address_matches("Unit 18, 9204 23 Avenue NW", "9204 23 Ave NW #18")
        )

    def test_unit_conflict_fails(self):
        self.assertFalse(
            s.address_matches("Unit 18, 9204 23 Avenue NW", "9204 23 Ave NW #19")
        )

    def test_unit_hyphen_notation(self):
        self.assertTrue(
            s.address_matches("18-9204 23 Avenue NW", "9204 23 Ave NW Suite 18")
        )

    def test_unknown_address_does_not_guess(self):
        self.assertFalse(s.address_matches("Box 1, Site 13, RR 2", "9204 23 Avenue NW"))

    def test_two_source_addresses_are_ambiguous(self):
        lead = self.lead(
            "Synthetic Branch Kitchen moved from 10807 Castle Downs Road NW to 9204 23 Avenue NW in Edmonton."
        )
        result = s.verify(lead)
        self.assertIn("ambiguous", result["branch_review_reason"])
        self.google.assert_not_called()
        self.assertFalse(s.eligible(result)[0])

    def test_branch_opening_without_source_address_is_held(self):
        result = s.verify(
            self.lead("Synthetic Branch Kitchen opened a second location in Edmonton.")
        )
        self.assertIn(
            "without a verifiable street address", result["branch_review_reason"]
        )
        self.google.assert_not_called()

    def test_google_query_contains_source_street_address(self):
        self.verify_good()
        query = self.google.call_args.args[0].casefold()
        self.assertIn("9204 23 avenue nw", query)

    def test_wrong_branch_only_cannot_pass_or_borrow_old_reviews(self):
        self.google.return_value = [
            self.listing(
                "10807 Castle Downs Rd NW, Edmonton, AB",
                count=735,
                place="fixture-north",
            )
        ]
        result = s.verify(self.lead())
        self.assertIn("wrong-branch ratings prohibited", result["branch_review_reason"])
        self.assertNotIn("google_place_id", result)
        self.assertNotIn("google_reviews", result)
        self.assertFalse(s.eligible(result)[0])

    def test_existing_wrong_provider_id_is_requeried_and_removed(self):
        lead = self.lead()
        lead.update(
            self.listing(
                "10807 Castle Downs Rd NW, Edmonton, AB",
                count=735,
                place="fixture-north",
            )
        )
        self.google.return_value = [
            self.listing(
                "10807 Castle Downs Rd NW, Edmonton, AB",
                count=735,
                place="fixture-north",
            )
        ]
        result = s.verify(lead)
        self.google.assert_called_once()
        self.assertNotIn("google_place_id", result)
        self.assertFalse(s.rating_gate(result))

    def test_correct_low_review_branch_selected_instead_of_popular_old_branch(self):
        self.google.return_value = [
            self.listing(
                "10807 Castle Downs Rd NW, Edmonton, AB",
                count=735,
                place="fixture-north",
            ),
            self.listing(count=2),
        ]
        self.official.return_value = self.page()
        result = s.verify(self.lead())
        self.assertEqual(result["google_place_id"], "fixture-south")
        self.assertEqual(result["google_reviews"], 2)
        self.assertFalse(s.rating_gate(result))
        self.assertTrue(s.new_exception(result))
        self.assertTrue(s.eligible(result)[0])

    def test_existing_wrong_branch_ratings_not_inherited_when_correct_branch_found(
        self,
    ):
        lead = self.lead()
        lead.update(
            self.listing(
                "10807 Castle Downs Rd NW, Edmonton, AB",
                count=735,
                place="fixture-north",
            )
        )
        self.google.return_value = [self.listing(count=1)]
        self.official.return_value = self.page()
        result = s.verify(lead)
        self.assertEqual(result["google_reviews"], 1)
        self.assertEqual(result["google_place_id"], "fixture-south")

    def test_wrong_yelp_branch_reviews_do_not_repair_google_threshold(self):
        self.google.return_value = [self.listing(count=9)]
        self.yelp.return_value = [
            {
                "name": "Synthetic Branch Kitchen",
                "city": "Edmonton",
                "address": "10807 Castle Downs Rd NW, Edmonton, AB",
                "yelp_id": "fixture-yelp-north",
                "yelp_rating": 4.9,
                "yelp_reviews": 800,
            }
        ]
        self.official.return_value = self.page()
        lead = self.lead()
        lead["evidence"][0]["opening_status"] = "unknown"
        result = s.verify(lead)
        self.assertNotIn("yelp_reviews", result)
        self.assertFalse(s.rating_gate(result))
        self.assertFalse(s.eligible(result)[0])

    def test_same_yelp_branch_can_corroborate_rating(self):
        self.google.return_value = [self.listing(count=9)]
        self.yelp.return_value = [
            {
                "name": "Synthetic Branch Kitchen",
                "city": "Edmonton",
                "address": "9204 23 Avenue NW, Edmonton, AB",
                "yelp_id": "fixture-yelp-south",
                "yelp_rating": 4.5,
                "yelp_reviews": 12,
            }
        ]
        self.official.return_value = self.page()
        result = s.verify(self.lead())
        self.assertEqual(result["yelp_reviews"], 12)
        self.assertTrue(s.rating_gate(result))

    def test_cross_provider_mismatch_without_news_address_still_skips_yelp(self):
        self.google.return_value = [self.listing(count=9)]
        self.yelp.return_value = [
            {
                "name": "Synthetic Branch Kitchen",
                "city": "Edmonton",
                "address": "10807 Castle Downs Rd NW, Edmonton, AB",
                "yelp_id": "fixture-yelp-north",
                "yelp_rating": 4.9,
                "yelp_reviews": 800,
            }
        ]
        lead = self.lead("Synthetic Branch Kitchen offers a tasting menu in Edmonton.")
        lead["evidence"][0]["opening_status"] = "unknown"
        result = s.verify(lead)
        self.assertNotIn("yelp_reviews", result)
        self.assertTrue(s.METRICS["warnings"])

    def test_multiple_unlocated_google_branches_are_not_guessed(self):
        lead = self.lead("Synthetic Branch Kitchen is now open in Edmonton.")
        self.google.return_value = [
            self.listing(place="fixture-one"),
            self.listing("10807 Castle Downs Rd NW, Edmonton, AB", place="fixture-two"),
        ]
        result = s.verify(lead)
        self.assertIn("multiple same-name", result["branch_review_reason"])
        self.assertFalse(s.eligible(result)[0])
        self.yelp.assert_not_called()

    def test_official_address_missing_holds_new_opening(self):
        self.google.return_value = [self.listing(count=100)]
        self.official.return_value = self.page("Contact our team for directions.")
        result = s.verify(self.lead())
        self.assertFalse(result["branch_verified"])
        self.assertFalse(s.eligible(result)[0])

    def test_wrong_official_address_holds_new_opening(self):
        self.google.return_value = [self.listing(count=100)]
        self.official.return_value = self.page("10807 Castle Downs Road NW")
        result = s.verify(self.lead())
        self.assertFalse(s.new_exception(result))
        self.assertFalse(s.eligible(result)[0])

    def test_multibranch_official_page_holds_new_opening(self):
        self.google.return_value = [self.listing(count=100)]
        self.official.return_value = self.page(
            "9204 23 Avenue NW and 10807 Castle Downs Road NW"
        )
        result = s.verify(self.lead())
        self.assertFalse(s.eligible(result)[0])
        self.assertIn("single-branch official page", result["branch_review_reason"])

    def test_repeated_same_official_address_is_not_two_branches(self):
        self.google.return_value = [self.listing()]
        self.official.return_value = self.page(
            "9204 23 Avenue NW. Footer: 9204 23 Ave NW."
        )
        result = s.verify(self.lead())
        self.assertTrue(s.eligible(result)[0])

    def test_false_verified_flag_cannot_override_source_provider_conflict(self):
        c = self.lead()
        c.update(self.listing("10807 Castle Downs Rd NW, Edmonton, AB", count=100))
        c.update(branch_verified=True, official_verified=True)
        self.assertFalse(s.new_exception(c))
        self.assertFalse(s.eligible(c)[0])

    def test_internal_key_keeps_branches_separate(self):
        a = self.listing()
        b = self.listing(
            "10807 Castle Downs Rd NW, Edmonton, AB", place="fixture-north"
        )
        self.assertNotEqual(s.key(a), s.key(b))

    def test_equivalent_source_spellings_merge(self):
        a = self.lead()
        b = self.lead(
            "Synthetic Branch Kitchen opened at 9204 23rd Ave NW in Edmonton."
        )
        self.assertEqual(s.key(a), s.key(b))

    def test_backend_name_city_dedup_stays_conservative(self):
        self.assertTrue(
            s.duplicate(
                self.listing(),
                [
                    self.listing(
                        "10807 Castle Downs Rd NW, Edmonton, AB", place="fixture-north"
                    )
                ],
            )
        )

    def test_payload_preserves_branch_metadata(self):
        c = self.verify_good()
        c["selected_image"] = {"url": "https://official.example/fixture.jpg"}
        out = s.payload([c], {s.key(c): "Synthetic offline description only."})[0]
        self.assertEqual(out["address"], c["address"])
        self.assertEqual(out["google_place_id"], "fixture-south")
        self.assertEqual(out["source_address"], "9204 23 avenue nw")
        self.assertTrue(out["branch_verified"])
        self.assertEqual(out["status"], "pending")


if __name__ == "__main__":
    unittest.main()
