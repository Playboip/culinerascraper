"""Offline fallback regressions; these mocked fixtures never enter ingestion.

Run: python -B -m unittest -v test_free_dish_photos
No live requests, SQL, main scraper imports, or image downloads are permitted.
"""

import ast
import copy
import importlib
import inspect
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests
import free_dish_photos as photos


def fixture_page(number=1):
    """Synthetic API response, used only within this offline unit test module."""
    name = f"File:Butter chicken {number}.jpg"
    return {
        "pageid": number,
        "ns": 6,
        "title": name,
        "imageinfo": [
            {
                "url": f"https://upload.wikimedia.org/wikipedia/commons/a/ab/Butter_chicken_{number}.jpg",
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:Butter_chicken_{number}.jpg",
                "thumburl": f"https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Butter_chicken_{number}.jpg/1000px-Butter_chicken_{number}.jpg",
                "width": 1800,
                "height": 1200,
                "thumbwidth": 1000,
                "thumbheight": 667,
                "mime": "image/jpeg",
                "mediatype": "BITMAP",
                "extmetadata": {
                    "LicenseShortName": {
                        "value": "CC BY 4.0",
                        "source": "commons-desc-page",
                    },
                    "LicenseUrl": {
                        "value": "https://creativecommons.org/licenses/by/4.0/"
                    },
                    "License": {"value": "cc-by-4.0"},
                    "UsageTerms": {
                        "value": "Creative Commons Attribution 4.0 International"
                    },
                    "Artist": {
                        "value": '<a href="https://commons.wikimedia.org/wiki/User:Example">Example Photographer</a>'
                    },
                    "Credit": {"value": "Own work"},
                    "AttributionRequired": {"value": "true"},
                    "ImageDescription": {"value": "A plate of butter chicken."},
                    "ObjectName": {"value": "Butter chicken"},
                },
            }
        ],
    }


class CommonsFallbackTests(unittest.TestCase):
    def setUp(self):
        # Forbid every real requests/Session HTTP operation, even if the module
        # accidentally stops using its injectable HTTP.get seam.
        self.forbidden = patch.object(
            requests.sessions.Session,
            "request",
            side_effect=AssertionError("Live HTTP is forbidden in offline tests"),
        )
        self.forbidden.start()
        self.addCleanup(self.forbidden.stop)
        self.env = patch.dict(os.environ, {"MAX_FREE_IMAGE_REQUESTS": "30"})
        self.env.start()
        self.addCleanup(self.env.stop)
        importlib.reload(photos)
        self.session = Mock()
        self.http_patch = patch.object(photos, "HTTP", self.session)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.response = Mock(status_code=200)
        self.session.get.return_value = self.response
        self.set_pages(fixture_page())

    def set_pages(self, *pages):
        self.response.json.return_value = {
            "batchcomplete": "",
            "query": {"pages": {str(i): page for i, page in enumerate(pages)}},
        }

    def metadata(self, page):
        return page["imageinfo"][0]["extmetadata"]

    def rejected(self, page):
        self.set_pages(page)
        self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])

    def test_single_fixed_get_exact_schema_and_honest_credit(self):
        result = photos.search_licensed_dish_images("Butter chicken")
        self.assertEqual(len(result), 1)
        item = result[0]
        self.assertEqual(
            set(item),
            {
                "provider",
                "url",
                "source_page",
                "attribution",
                "licence_name",
                "licence_url",
                "rights_status",
                "is_illustrative",
                "image_caption",
                "dish",
                "source_metadata",
                "licence_evidence",
            },
        )
        self.assertEqual(item["provider"], "Wikimedia Commons")
        self.assertEqual(item["rights_status"], "source_licence_recorded")
        self.assertIs(item["is_illustrative"], True)
        self.assertEqual(
            item["image_caption"], "Illustrative image — not photographed at this venue"
        )
        self.assertIn("Artist (as recorded): Example Photographer", item["attribution"])
        self.assertIn("Source credit: Own work", item["attribution"])
        self.assertNotIn("verifiedownership", item["rights_status"])
        self.assertEqual(item["source_metadata"]["attribution_required"], "true")
        self.assertEqual(item["source_metadata"]["Artist"], "Example Photographer")
        evidence = json.loads(item["licence_evidence"])
        self.assertEqual(
            evidence["extmetadata"]["LicenseShortName"]["source"], "commons-desc-page"
        )
        self.assertEqual(photos.requests_count, 1)
        self.session.get.assert_called_once()
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, ("https://commons.wikimedia.org/w/api.php",))
        self.assertEqual(kwargs["params"]["generator"], "search")
        self.assertEqual(kwargs["params"]["gsrsearch"], "Butter chicken")
        self.assertEqual(kwargs["params"]["gsrnamespace"], 6)
        self.assertEqual(kwargs["params"]["gsrlimit"], 3)
        self.assertEqual(
            kwargs["params"]["iiprop"], "url|mime|size|mediatype|extmetadata"
        )
        self.assertEqual(kwargs["params"]["iiurlwidth"], 1000)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIn(
            "https://seo-phase.emergent.host", kwargs["headers"]["User-Agent"]
        )
        self.assertNotIn("Authorization", kwargs["headers"])
        self.response.close.assert_called_once()
        self.session.cookies.clear.assert_called_once()

    def test_each_allowlisted_by_version_and_cc0(self):
        for version in ("2.0", "2.5", "3.0", "4.0"):
            with self.subTest(version=version):
                page = fixture_page()
                meta = self.metadata(page)
                meta["LicenseShortName"]["value"] = f"CC BY {version}"
                meta["LicenseUrl"][
                    "value"
                ] = f"http://creativecommons.org/licenses/by/{version}/"
                meta["License"]["value"] = f"cc-by-{version}"
                meta["UsageTerms"]["value"] = f"CC BY {version}"
                self.set_pages(page)
                item = photos.search_licensed_dish_images("Butter chicken")[0]
                self.assertEqual(item["licence_name"], f"CC BY {version}")
                self.assertEqual(
                    item["licence_url"],
                    f"https://creativecommons.org/licenses/by/{version}/",
                )
        for name in ("CC0", "CC0 1.0"):
            with self.subTest(name=name):
                page = fixture_page()
                meta = self.metadata(page)
                meta["LicenseShortName"]["value"] = name
                meta["LicenseUrl"][
                    "value"
                ] = "https://creativecommons.org/publicdomain/zero/1.0/"
                meta["License"]["value"] = "cc-zero"
                meta["UsageTerms"]["value"] = "Creative Commons CC0 Waiver"
                meta["AttributionRequired"]["value"] = "false"
                meta.pop("Artist")
                self.set_pages(page)
                item = photos.search_licensed_dish_images("Butter chicken")[0]
                self.assertEqual(item["licence_name"], name)
                self.assertIn("Author not identified", item["attribution"])
                self.assertEqual(
                    item["source_metadata"]["attribution_required"], "false"
                )

    def test_reject_unsupported_and_ambiguous_licences(self):
        cases = [
            ("LicenseShortName", "CC BY-SA 4.0"),
            ("LicenseShortName", "CC BY-NC 4.0"),
            ("LicenseShortName", "CC BY-ND 4.0"),
            ("LicenseShortName", "GFDL"),
            ("LicenseShortName", "Public domain"),
            ("LicenseShortName", "CC BY 1.0"),
            ("LicenseShortName", "CC BY 4.0; GFDL"),
            ("LicenseShortName", "CC BY 4.0 / CC0"),
            ("LicenseShortName", "CC BY 4.0 or CC BY 3.0"),
            ("License", "cc-by-sa-4.0"),
            ("License", "cc-by-4.0;gfdl"),
            ("UsageTerms", "CC BY 4.0 and GFDL"),
            ("Permission", "CC BY 4.0 or GFDL"),
            ("Permission", "CC BY 3.0"),
            ("Permission", "CC BY 4.0 or CC0"),
            ("Permission", "All rights reserved"),
            ("LicenseShortName", "CC BY 4.0<script>; GFDL</script>"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                page = fixture_page()
                self.metadata(page)[field] = {"value": value}
                self.rejected(page)

    def test_licence_name_url_must_agree_and_host_path_are_exact(self):
        urls = [
            "https://creativecommons.org/licenses/by/3.0/",
            "https://creativecommons.org/licenses/by-sa/4.0/",
            "https://creativecommons.org/publicdomain/zero/1.0/",
            "https://creativecommons.org/licenses/by/4.0/deed.en",
            "https://creativecommons.org/licenses/by/4.0/us/",
            "https://creativecommons.org.evil.example/licenses/by/4.0/",
            "https://www.creativecommons.org/licenses/by/4.0/",
            "https://u:p@creativecommons.org/licenses/by/4.0/",
            "https://creativecommons.org:443/licenses/by/4.0/",
            "https://creativecommons.org/licenses/by/4.0/?x=1",
            "https://creativecommons.org/licenses/by/4.0/#fragment",
            "https://127.0.0.1/licenses/by/4.0/",
            "javascript:alert(1)",
            "",
            None,
        ]
        for url in urls:
            with self.subTest(url=url):
                page = fixture_page()
                self.metadata(page)["LicenseUrl"]["value"] = url
                self.rejected(page)

    def test_required_artist_may_not_be_own_work_or_missing(self):
        for artist in (
            "",
            "Own work",
            "unknown",
            "Anonymous",
            None,
            "<script>Example</script>",
        ):
            with self.subTest(artist=artist):
                page = fixture_page()
                self.metadata(page)["Artist"]["value"] = artist
                self.rejected(page)
        for required in ("false", "0", "no", "maybe"):
            page = fixture_page()
            self.metadata(page)["AttributionRequired"]["value"] = required
            self.rejected(page)

    def test_custom_attribution_and_full_plain_metadata_evidence(self):
        page = fixture_page()
        meta = self.metadata(page)
        meta["Artist"][
            "value"
        ] = '<script>alert(1)</script><b onclick="evil()">Example Photographer</b>'
        meta["Attribution"] = {
            "value": '<a href="javascript:evil()">Credit the photographer and project</a>'
        }
        meta["UnusualField"] = {
            "value": "&lt;script&gt;bad()&lt;/script&gt;<div>Retain this evidence &amp; text</div>",
            "source": "api",
        }
        self.set_pages(page)
        item = photos.search_licensed_dish_images("Butter chicken")[0]
        serialized = json.dumps(item, ensure_ascii=False)
        for forbidden in ("<", ">", "alert(1)", "bad()", "onclick", "javascript:"):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("Credit the photographer and project", item["attribution"])
        self.assertEqual(
            item["source_metadata"]["UnusualField"], "Retain this evidence & text"
        )
        self.assertEqual(
            json.loads(item["licence_evidence"])["extmetadata"]["UnusualField"][
                "source"
            ],
            "api",
        )
        self.assertTrue(
            all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in item["source_metadata"].items()
            )
        )

    def test_nonempty_rights_warning_flags_rejected(self):
        for field in ("NonFree", "DeletionReason", "Restrictions"):
            for value in (
                "true",
                "false",
                "disputed",
                ["trademark"],
                False,
                "<script>disputed</script>",
            ):
                with self.subTest(field=field, value=value):
                    page = fixture_page()
                    self.metadata(page)[field] = {"value": value}
                    self.rejected(page)
        page = fixture_page()
        self.metadata(page)["Restrictions"] = {"value": []}
        self.set_pages(page)
        self.assertEqual(len(photos.search_licensed_dish_images("Butter chicken")), 1)

    def test_only_photo_mimes_bitmap_and_sufficient_dimensions(self):
        cases = [
            ("mime", "image/svg+xml"),
            ("mime", "image/gif"),
            ("mime", "application/pdf"),
            ("mediatype", "DRAWING"),
            ("mediatype", None),
            ("width", 599),
            ("height", 349),
            ("width", "1800"),
            ("height", True),
            ("thumbwidth", 599),
            ("thumbheight", 349),
            ("thumbmime", "text/html"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                page = fixture_page()
                page["imageinfo"][0][field] = value
                self.rejected(page)
        page = fixture_page()
        info = page["imageinfo"][0]
        info.update(width=600, height=350, thumbwidth=600, thumbheight=350)
        self.set_pages(page)
        self.assertEqual(len(photos.search_licensed_dish_images("Butter chicken")), 1)
        for mime in ("image/png", "image/webp"):
            page = fixture_page()
            page["imageinfo"][0]["mime"] = mime
            self.set_pages(page)
            self.assertEqual(
                len(photos.search_licensed_dish_images("Butter chicken")), 1
            )

    def test_fixed_upload_and_valid_matching_file_page_required(self):
        cases = [
            ("url", "http://upload.wikimedia.org/wikipedia/commons/a/ab/a.jpg"),
            (
                "thumburl",
                "https://u:p@upload.wikimedia.org/wikipedia/commons/a/ab/a.jpg",
            ),
            ("thumburl", "https://upload.wikimedia.org.evil.example/a.jpg"),
            ("thumburl", "https://localhost/a.jpg"),
            ("thumburl", "https://upload.wikimedia.org:443/a.jpg"),
            ("thumburl", "https://upload.wikimedia.org/wikipedia/commons/../a.jpg"),
            (
                "thumburl",
                "https://upload.wikimedia.org/wikipedia/commons/%252e%252e/a.jpg",
            ),
            (
                "thumburl",
                "https://upload.wikimedia.org/wikipedia/commons/a.jpg?token=1",
            ),
            (
                "descriptionurl",
                "https://commons.wikimedia.org/wiki/Special:FilePath/Butter_chicken_1.jpg",
            ),
            (
                "descriptionurl",
                "https://commons.wikimedia.org/wiki/File:Other_dish.jpg",
            ),
            (
                "descriptionurl",
                "https://u:p@commons.wikimedia.org/wiki/File:Butter_chicken_1.jpg",
            ),
            ("descriptionurl", "https://commons.wikimedia.org/wiki/File:"),
            (
                "descriptionurl",
                "https://commons.wikimedia.org/wiki/File:Butter_chicken_1.jpg#license",
            ),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                page = fixture_page()
                page["imageinfo"][0][field] = value
                self.rejected(page)
        page = fixture_page()
        page["imageinfo"][0].pop("thumburl")
        self.set_pages(page)
        self.assertEqual(
            photos.search_licensed_dish_images("Butter chicken")[0]["url"],
            page["imageinfo"][0]["url"],
        )

    def test_literal_dish_in_one_metadata_field_not_fuzzy_or_partial(self):
        page = fixture_page()
        page["title"] = "File:Other food.jpg"
        page["imageinfo"][0][
            "descriptionurl"
        ] = "https://commons.wikimedia.org/wiki/File:Other_food.jpg"
        meta = self.metadata(page)
        meta["ImageDescription"]["value"] = "Butter chicken served with rice"
        meta["ObjectName"]["value"] = "Other food"
        self.set_pages(page)
        self.assertEqual(len(photos.search_licensed_dish_images("BUTTER-CHICKEN")), 1)
        for text in (
            "Butter chickenish",
            "Chicken with butter",
            "A curry dish",
            "Butter",
        ):
            meta["ImageDescription"]["value"] = text
            self.rejected(page)
        meta["ImageDescription"]["value"] = "Butter"
        meta["ObjectName"]["value"] = "Chicken"
        self.rejected(page)
        meta["ObjectName"]["value"] = "Butter_chicken"
        self.set_pages(page)
        self.assertEqual(len(photos.search_licensed_dish_images("Butter chicken")), 1)
        self.assertEqual(photos._literal("Crème brûlée"), "creme brulee")

    def test_invalid_input_requests_nothing(self):
        for dish, limit in (
            (None, 3),
            ("", 3),
            ("<script>bad</script>", 3),
            ("x" * 201, 3),
            ("Butter chicken", 0),
            ("Butter chicken", -1),
            ("Butter chicken", "3"),
            ("Butter chicken", True),
        ):
            self.assertEqual(photos.search_licensed_dish_images(dish, limit), [])
        self.assertEqual(photos.requests_count, 0)
        self.session.get.assert_not_called()

    def test_five_result_limit_and_no_continuation(self):
        self.set_pages(*(fixture_page(number) for number in range(1, 9)))
        self.response.json.return_value["continue"] = {"gsroffset": 5}
        self.assertEqual(
            len(photos.search_licensed_dish_images("Butter chicken", 99)), 5
        )
        self.assertEqual(self.session.get.call_args.kwargs["params"]["gsrlimit"], 5)
        self.session.get.assert_called_once()

    def test_timeout_network_failure_json_failure_and_api_error_are_empty(self):
        for exception in (
            requests.Timeout("untrusted details"),
            requests.ConnectionError("untrusted details"),
        ):
            with self.subTest(exception=type(exception).__name__):
                self.session.get.side_effect = exception
                self.assertEqual(
                    photos.search_licensed_dish_images("Butter chicken"), []
                )
                self.assertNotIn("untrusted details", " ".join(photos.MODULE_WARNINGS))
        self.session.get.side_effect = None
        self.response.json.side_effect = ValueError("<script>bad</script>")
        self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])
        self.response.json.side_effect = None
        for payload in (
            None,
            [],
            {},
            {"error": {"info": "<script>bad</script>"}},
            {"query": {"pages": []}},
        ):
            self.response.json.return_value = payload
            self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])
        self.assertTrue(photos.MODULE_WARNINGS)
        self.assertNotIn("<script>", " ".join(photos.MODULE_WARNINGS))
        self.assertEqual(self.session.get.call_count, 8)

    def test_403_and_429_disable_later_calls_no_retries(self):
        for status in (403, 429):
            with self.subTest(status=status):
                photos._BLOCKED = False
                self.session.get.reset_mock()
                self.response.status_code = status
                self.assertEqual(
                    photos.search_licensed_dish_images("Butter chicken"), []
                )
                self.assertEqual(
                    photos.search_licensed_dish_images("Other actual dish"), []
                )
                self.session.get.assert_called_once()
                self.assertIn(str(status), " ".join(photos.MODULE_WARNINGS))

    def test_redirect_is_not_followed(self):
        self.response.status_code = 302
        self.response.headers = {"Location": "http://127.0.0.1/private"}
        self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])
        self.session.get.assert_called_once()
        self.response.json.assert_not_called()

    def test_malformed_candidate_structures_fail_closed(self):
        for page in (
            None,
            [],
            {},
            {"ns": 6, "title": "File:Butter chicken.jpg", "imageinfo": [None]},
        ):
            self.rejected(page)
        for field, value in (
            ("LicenseShortName", []),
            ("Artist", {"wrong": "shape"}),
            ("LicenseUrl", {"value": ["https://creativecommons.org/licenses/by/4.0/"]}),
        ):
            page = fixture_page()
            self.metadata(page)[field] = value
            self.rejected(page)

    def test_budget_counts_attempts_default_hard_cap_and_env_lowering(self):
        self.response.json.return_value = {"batchcomplete": ""}
        for _ in range(35):
            self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])
        self.assertEqual(photos.requests_count, 30)
        self.assertEqual(self.session.get.call_count, 30)
        self.assertIn("budget exhausted", " ".join(photos.MODULE_WARNINGS))
        for configured, expected in (("2", 2), ("999", 30), ("-5", 0), ("invalid", 30)):
            with patch.dict(os.environ, {"MAX_FREE_IMAGE_REQUESTS": configured}):
                self.assertEqual(photos._request_budget(), expected)
        with patch.object(photos, "MAX_FREE_IMAGE_REQUESTS", 1):
            photos.requests_count = 0
            self.session.get.reset_mock()
            self.session.get.side_effect = requests.Timeout()
            self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])
            self.assertEqual(photos.search_licensed_dish_images("Butter chicken"), [])
            self.assertEqual(photos.requests_count, 1)
            self.session.get.assert_called_once()

    def test_duplicate_pages_not_returned_twice(self):
        self.set_pages(fixture_page(), copy.deepcopy(fixture_page()))
        self.assertEqual(len(photos.search_licensed_dish_images("Butter chicken")), 1)

    def test_module_has_no_database_ingestion_or_file_write_code(self):
        tree = ast.parse(inspect.getsource(photos))
        forbidden_imports = {
            "sqlite3",
            "psycopg",
            "psycopg2",
            "supabase",
            "sqlalchemy",
            "alberta_culinary_scraper",
            "openai",
            "subprocess",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(
                    {alias.name.split(".")[0] for alias in node.names}
                    & forbidden_imports
                )
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], forbidden_imports)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "exec", "eval"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(
                    node.func.attr,
                    {
                        "execute",
                        "executemany",
                        "insert",
                        "upsert",
                        "write_text",
                        "write_bytes",
                        "post",
                        "put",
                        "delete",
                    },
                )


if __name__ == "__main__":
    unittest.main()
