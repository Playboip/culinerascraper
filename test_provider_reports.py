"""Offline tests: no real discovery, paid APIs, or ingestion."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
import requests
import alberta_culinary_scraper as s


class ProviderReportTests(unittest.TestCase):
    def fixture(self, stack, folder):
        p = Path(folder, "report.json")
        stack.enter_context(patch.object(s, "REPORT_PATH", p))
        stack.enter_context(patch.object(s, "STATE_DIR", Path(folder, "state")))
        stack.enter_context(patch.object(s, "DRY_RUN", True))
        stack.enter_context(patch.object(s, "DISCOVERY_ONLY", False))
        stack.enter_context(
            patch.object(s, "GOOGLE_MAPS_API_KEY", "synthetic-google-key")
        )
        stack.enter_context(patch.object(s, "OPENAI_API_KEY", "synthetic-ai-key"))
        stack.enter_context(patch.object(s, "YELP_API_KEY", "synthetic-yelp-key"))
        stack.enter_context(patch.object(s, "YELP_DISABLED_REASON", ""))
        stack.enter_context(
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
        doc = {
            "source_name": "Fixture only",
            "title": "Offline fixture",
            "url": "https://fixture.example/source",
            "text": "No real business discovery in this test.",
        }
        stack.enter_context(
            patch.object(
                s, "collect_documents", return_value=([doc], {"document_count": 1})
            )
        )
        return p

    def test_failed_google_preflight_happens_before_ai_and_saves_safe_report(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            p = self.fixture(stack, tmp)
            r = requests.Response()
            r.status_code = 403
            r._content = (
                b'{"error":{"status":"PERMISSION_DENIED","message":"API not enabled"}}'
            )
            stack.enter_context(patch.object(s.HTTP, "request", return_value=r))
            extraction = stack.enter_context(patch.object(s, "extract_leads"))
            post = stack.enter_context(patch.object(s.HTTP, "post"))
            self.assertEqual(s.main(), 2)
            extraction.assert_not_called()
            post.assert_not_called()
            d = json.loads(p.read_text())
            self.assertEqual(d["status"], "failed")
            self.assertIn("Google Places HTTP 403", d["error"])
            self.assertEqual(len(d["source_documents"]), 1)
            self.assertEqual(d["metrics"]["ai_calls"], 0)

    def test_later_provider_error_does_not_erase_extracted_leads(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            p = self.fixture(stack, tmp)
            stack.enter_context(patch.object(s, "preflight_providers"))
            lead = {
                "name": "Synthetic test record only",
                "city": "Canmore",
                "evidence": [],
            }
            stack.enter_context(
                patch.object(s, "extract_leads", return_value=([lead], []))
            )
            stack.enter_context(
                patch.object(
                    s,
                    "extra_discovery",
                    side_effect=s.PipelineError("Synthetic request budget failure"),
                )
            )
            post = stack.enter_context(patch.object(s.HTTP, "post"))
            self.assertEqual(s.main(), 2)
            post.assert_not_called()
            d = json.loads(p.read_text())
            self.assertEqual(d["lead_count"], 1)
            self.assertEqual(d["discovered_leads"][0]["name"], lead["name"])
            self.assertEqual(d["candidates"], [])


if __name__ == "__main__":
    unittest.main()
