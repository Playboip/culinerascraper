"""Report-32 regressions. Fixtures never enter production or real API calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
from scrapling import Selector
import alberta_culinary_scraper as s
import scrapling_discovery as d
import test_pipeline as fixtures


class FakePage:
    def __init__(self, html):
        self.body = html.encode()
        self.selector = Selector(html, url="https://official.example/")

    def css(self, query):
        return self.selector.css(query)


class OfficialExtractionTests(unittest.TestCase):
    def test_official_body_keeps_business_and_address_not_testimonials(self):
        html = """<html><head><meta name="description" content="Seasonal dining at Fixture Kitchen."></head><body><article class="reviews"><p>A reviewer claims fictional lobster and best service.</p></article><main><h1>Fixture Kitchen</h1><p>We offer a tasting menu.</p><div id="testimonial-carousel">Invented review claims.</div></main><footer>123 Fixture Street, Canmore, AB</footer><script>fake chef story</script></body></html>"""
        text = d.official_page_business_text(html)
        self.assertIn("We offer a tasting menu.", text)
        self.assertIn("123 Fixture Street", text)
        self.assertIn("Seasonal dining", text)
        self.assertNotIn("fictional lobster", text)
        self.assertNotIn("Invented review", text)
        self.assertNotIn("fake chef", text)

    def test_photo_srcset_preserves_cdn_commas_and_largest_size(self):
        raw = "https://cdn.example/media/fill/w_400,h_300,q_90/photo.jpg 400w, https://cdn.example/media/fill/w_1200,h_900,q_90/photo.jpg 1200w"
        urls = d._srcset_urls(raw)
        self.assertEqual(
            urls[0], "https://cdn.example/media/fill/w_1200,h_900,q_90/photo.jpg"
        )
        self.assertEqual(len(urls), 2)

    def test_photo_url_does_not_collapse_embedded_cdn_http_path(self):
        raw = "https://cdn.example/image/fetch/f_auto/http://static.example/real.jpg?size=large&quality=80"
        self.assertEqual(d._photo_url(raw, "https://official.example/"), raw)

    def test_lazy_picture_background_and_srcset_photos_are_retained(self):
        html = """<html><head><meta property="og:image" content="/logo.png"></head><body><picture><source srcset="/food-small.jpg 400w, /food-large.jpg 1200w"></picture><img data-lazy-src="/interior.jpg"><div style="background-image:url('/exterior.jpg')"></div><img src="/placeholder.svg"></body></html>"""
        options = d._image_options(FakePage(html), "https://official.example/")
        urls = [x["url"] for x in options]
        self.assertIn("https://official.example/food-large.jpg", urls)
        self.assertIn("https://official.example/interior.jpg", urls)
        self.assertIn("https://official.example/exterior.jpg", urls)
        self.assertFalse(any("logo" in x or "placeholder" in x for x in urls))
        self.assertTrue(all(x["provider"] == "official website" for x in options))

    def test_exact_photo_transform_query_is_preserved(self):
        url = "https://cdn.example/photo.jpg?rect=306%2C563%2C3905%2C3149&w=1400&q=85"
        self.assertEqual(d._photo_url(url, "https://official.example/"), url)

    def test_unsafe_photo_urls_rejected(self):
        for url in [
            "javascript:alert(1)",
            "data:image/png;base64,abc",
            "https://user:password@cdn.example/a.jpg",
            "http://127.0.0.1/a.jpg",
        ]:
            with self.subTest(url=url):
                self.assertIsNone(d._photo_url(url, "https://official.example/"))

    def test_obvious_logos_do_not_consume_photo_limit(self):
        html = (
            "<html><body>"
            + "".join(f'<img src="/logo-{i}.png">' for i in range(20))
            + '<img src="/actual-food.jpg"></body></html>'
        )
        options = d._image_options(FakePage(html), "https://official.example/")
        self.assertEqual(
            [x["url"] for x in options], ["https://official.example/actual-food.jpg"]
        )


class TargetedCoreTests(unittest.TestCase):
    def setUp(self):
        self.forbidden = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("No HTTP in offline regressions"),
        )
        self.forbidden.start()
        self.addCleanup(self.forbidden.stop)
        self.metrics = patch.dict(
            s.METRICS,
            {
                "provider_requests": 0,
                "provider_successes": 0,
                "warnings": [],
                "provider_errors": [],
                "provider_successes_by_provider": {"Google Places": 1},
                "ai_calls": 0,
            },
            clear=True,
        )
        self.metrics.start()
        self.addCleanup(self.metrics.stop)
        self.c = fixtures.PipelineTests().candidate()

    def test_disabled_expired_yelp_makes_no_requests(self):
        with patch.object(s, "YELP_ENABLED", False), patch.object(
            s, "YELP_API_KEY", "fixture"
        ), patch.object(s, "provider_request") as request:
            self.assertEqual(s.yelp_search(city="Edmonton"), [])
            request.assert_not_called()

    def test_expired_trial_is_not_retried(self):
        error = s.ProviderUnavailableError(
            "Yelp", 400, "TRIAL_EXPIRED", "Offline expired trial"
        )
        with patch.object(s, "YELP_ENABLED", True), patch.object(
            s, "YELP_API_KEY", "fixture"
        ), patch.object(s, "YELP_DISABLED_REASON", ""), patch.object(
            s, "GOOGLE_MAPS_API_KEY", "fixture"
        ), patch.object(
            s, "provider_request", side_effect=error
        ) as request:
            self.assertEqual(s.yelp_search(city="Edmonton"), [])
            self.assertEqual(request.call_count, 1)

    def test_trial_expired_without_working_google_still_fails_closed(self):
        error = s.ProviderUnavailableError(
            "Yelp", 400, "TRIAL_EXPIRED", "Offline expired trial"
        )
        with patch.object(s, "YELP_ENABLED", True), patch.object(
            s, "YELP_API_KEY", "fixture"
        ), patch.object(s, "YELP_DISABLED_REASON", ""), patch.object(
            s, "GOOGLE_MAPS_API_KEY", ""
        ), patch.object(
            s, "provider_request", side_effect=error
        ) as request:
            with self.assertRaises(s.PipelineError):
                s.yelp_search()
            self.assertEqual(request.call_count, 1)

    def test_dotted_compass_directions_match_but_opposites_do_not(self):
        self.assertTrue(
            s.address_matches("3022 23 St. N.E.", "3022 23 Street NE, Calgary, AB")
        )
        self.assertFalse(
            s.address_matches("3022 23 St. N.E.", "3022 23 Street NW, Calgary, AB")
        )

    def test_longer_provider_name_needs_exact_independent_address(self):
        c = {"name": "Fixture Tora", "city": "Edmonton"}
        p = {
            "name": "Fixture Tora Japanese Bistro",
            "city": "Edmonton",
            "address": "123 Fixture Road SW",
            "google_place_id": "offline-id",
        }
        source = s.street_addresses("123 Fixture Road SW")
        self.assertEqual(s.select_branch_match(c, [p], source)[0], p)
        self.assertIsNone(s.select_branch_match(c, [p], [])[0])
        p["address"] = "124 Fixture Road SW"
        self.assertIsNone(s.select_branch_match(c, [p], source)[0])

    def test_other_brand_or_generic_name_cannot_pass_prefix_exception(self):
        self.assertFalse(
            s.provider_name_matches(
                {"name": "Coffee"}, {"name": "Coffee House Destination"}, True
            )
        )
        self.assertFalse(
            s.provider_name_matches(
                {"name": "Fixture Tora"}, {"name": "Fixture Toro Japanese Bistro"}, True
            )
        )

    def test_multiple_provider_ids_at_same_address_still_held(self):
        c = {"name": "Fixture Tora", "city": "Edmonton"}
        candidates = [
            {
                "name": "Fixture Tora Japanese Bistro",
                "city": "Edmonton",
                "address": "123 Fixture Road SW",
                "google_place_id": i,
            }
            for i in ["offline-1", "offline-2"]
        ]
        self.assertIsNone(
            s.select_branch_match(
                c, candidates, s.street_addresses("123 Fixture Road SW")
            )[0]
        )

    def test_address_first_retry_is_bounded_and_preserves_same_branch_only(self):
        c = {
            "name": "Fixture Tora",
            "city": "Edmonton",
            "evidence": [
                {
                    "quote": "Fixture Tora is now open at 123 Fixture Road SW.",
                    "opening_status": "open",
                    "published": "2026-11-18",
                    "url": "https://news.example/fixture",
                }
            ],
        }
        wrong = {
            "name": "Fixture Tora",
            "city": "Edmonton",
            "address": "999 Other Road SW",
            "google_place_id": "wrong-branch",
            "website": "https://official.example/",
        }
        right = {
            "name": "Fixture Tora Japanese Bistro",
            "city": "Edmonton",
            "address": "123 Fixture Road SW",
            "google_place_id": "right-branch",
            "website": "https://official.example/",
            "provider_official_url": "https://official.example/",
            "google_rating": 4.4,
            "google_reviews": 22,
        }
        page = {
            "title": "Fixture Tora",
            "text": "Fixture Tora 123 Fixture Road SW Edmonton",
            "business_text": "Fixture Tora 123 Fixture Road SW Edmonton",
            "url": "https://official.example/",
            "html": "",
            "image_options": [],
        }
        with patch.object(s, "GOOGLE_MAPS_API_KEY", "fixture"), patch.object(
            s, "google_search", side_effect=[[wrong], [right]]
        ) as query, patch.object(s, "yelp_search", return_value=[]), patch.object(
            s, "fetch_official_page", return_value=page
        ):
            result = s.verify(c)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result["google_place_id"], "right-branch")
        self.assertTrue(result["branch_verified"])
        self.assertNotIn("branch_review_reason", result)

    def test_verified_official_fact_pack_excludes_raw_customer_review_text(self):
        self.c["evidence"] = []
        self.c["official_page_text"] = (
            "A customer says it is award-winning and serves invented lobster."
        )
        self.c["official_business_text"] = "We offer a seasonal tasting menu."
        pack = s.description_fact_pack(self.c)
        self.assertEqual(pack["source_passages"], ["We offer a seasonal tasting menu."])
        self.assertNotIn("invented lobster", str(pack))
        self.assertTrue(s.signals(self.c))
        self.c["official_verified"] = False
        self.assertEqual(s.description_fact_pack(self.c)["source_passages"], [])

    def test_writing_and_audit_receive_identical_official_fact_sources(self):
        self.c["evidence"] = []
        self.c["official_business_text"] = "We offer a seasonal tasting menu."
        k = s.key(self.c)
        text = "This culinary stop in Canmore offers a seasonal tasting menu. Consider it when planning a food-focused route through the area, and check the official website before your visit for practical information rather than relying on a fixed road-trip schedule."
        with patch.object(
            s,
            "ai_json",
            side_effect=[
                {"descriptions": [{"candidate_key": k, "description": text}]},
                {"checks": [{"candidate_key": k, "supported": True}]},
            ],
        ) as ai:
            self.assertEqual(s.descriptions([self.c])[k], text)
        generation = json.loads(ai.call_args_list[0].args[1])[0]
        audit = json.loads(ai.call_args_list[1].args[1])[0]
        self.assertEqual(generation["source_passages"], audit["source_passages"])
        self.assertEqual(generation["source_records"], audit["source_records"])
        self.assertTrue(s.METRICS["description_checks"][0]["accepted"])

    def test_failed_description_has_only_one_bounded_rewrite(self):
        k = s.key(self.c)
        good = "This restaurant in Canmore offers a seasonal tasting menu. It can be considered when arranging a culinary route through the area, with the official website providing a useful starting point for checking practical details before making a visit."
        with patch.object(
            s,
            "ai_json",
            side_effect=[
                {"descriptions": [{"candidate_key": k, "description": "Too short."}]},
                {"descriptions": [{"candidate_key": k, "description": good}]},
                {"checks": [{"candidate_key": k, "supported": True}]},
            ],
        ) as ai:
            result = s.descriptions([self.c])
        self.assertEqual(result[k], good)
        self.assertEqual(ai.call_count, 3)
        self.assertTrue(
            any("length" in x["reason"] for x in s.METRICS["description_checks"])
        )

    def test_unsupported_descriptions_remain_held_after_bounded_attempts(self):
        k = s.key(self.c)
        text = "This restaurant in Canmore offers an invented cooking experience for visitors travelling through the region. Use it as a starting point when planning a culinary route, and check practical details before deciding whether to add the venue to your itinerary."
        proposed = {"descriptions": [{"candidate_key": k, "description": text}]}
        reject = {
            "checks": [
                {
                    "candidate_key": k,
                    "supported": False,
                    "reason": "Invented experience unsupported",
                }
            ]
        }
        with patch.object(
            s, "ai_json", side_effect=[proposed, reject, proposed, reject]
        ) as ai:
            self.assertEqual(s.descriptions([self.c]), {})
        self.assertEqual(ai.call_count, 4)
        self.assertIn(
            "no description passed", s.METRICS["description_checks"][-1]["reason"]
        )

    def test_free_image_metric_reads_actual_request_counter(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            s, "REPORT_PATH", Path(tmp, "report.json")
        ), patch.object(s.free_dish_photos, "requests_count", 7), patch.dict(
            "os.environ", {"GITHUB_STEP_SUMMARY": ""}
        ):
            report = {"status": "preview", "metrics": {}}
            s.report_write(report)
            self.assertEqual(
                json.loads(Path(tmp, "report.json").read_text())["metrics"][
                    "free_image_requests"
                ],
                7,
            )

    def test_image_decode_failure_records_specific_diagnostic(self):
        with patch.object(
            s, "fetch_image_fingerprint", return_value=None
        ), patch.object(s, "ai_json") as ai:
            self.assertIsNone(
                s.review_photo(
                    self.c,
                    {
                        "url": "https://official.example/a.jpg",
                        "source_page": "https://official.example/",
                    },
                    [],
                    set(),
                )
            )
            ai.assert_not_called()
        self.assertIn("GET/decode", self.c["image_attempts"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
