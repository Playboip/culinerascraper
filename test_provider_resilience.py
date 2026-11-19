"""Offline provider-resilience fixtures; never call live APIs or ingest fixtures."""

import copy
import json
import unittest
from unittest.mock import patch

import requests

import alberta_culinary_scraper as s


def response(status=200, payload=None, *, text=None, url="https://api.example/test"):
    """Use real requests decoding and HTTP errors, not permissive Mock JSON."""
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.encoding = "utf-8"
    result.headers["Content-Type"] = "application/json"
    result._content = (
        text if text is not None else json.dumps({} if payload is None else payload)
    ).encode("utf-8")
    result.request = requests.Request("GET", url).prepare()
    return result


def yelp_error(status, code="FIXTURE_ERROR", description="Offline provider failure"):
    return response(status, {"error": {"code": code, "description": description}})


class ProviderResilienceTests(unittest.TestCase):
    def setUp(self):
        self.yelp_enabled_patch = patch.object(s, "YELP_ENABLED", True)
        self.yelp_enabled_patch.start()
        self.addCleanup(self.yelp_enabled_patch.stop)
        # Replace and restore runtime state, including mutable nested metrics.
        metrics = copy.deepcopy(s.METRICS)
        for name, value in metrics.items():
            if isinstance(value, dict):
                metrics[name] = {}
            elif isinstance(value, list):
                metrics[name] = []
            elif isinstance(value, (int, float)):
                metrics[name] = 0
        metrics.update(
            provider_requests=0,
            provider_successes=0,
            provider_successes_by_provider={},
            provider_errors=[],
            ai_calls=0,
        )
        for name, value in {
            "METRICS": metrics,
            "YELP_DISABLED_REASON": "",
            "GOOGLE_MAPS_API_KEY": "fixture-google-secret-not-a-key",
            "YELP_API_KEY": "fixture-yelp-secret-not-a-key",
            "OPENAI_API_KEY": "fixture-openai-secret-not-a-key",
            "INGEST_API_KEY": "fixture-ingest-secret-not-a-key",
            "MAX_PROVIDER_REQUESTS": 100,
            "DRY_RUN": True,
            "DISCOVERY_ONLY": False,
            "LIVE_ENABLED": False,
        }.items():
            self.enterContext(patch.object(s, name, value, create=True))
        self.responses = []
        self.calls = []
        self.http = self.enterContext(
            patch.object(s.HTTP, "request", side_effect=self.offline_request)
        )
        self.enterContext(
            patch.object(
                s.HTTP, "get", side_effect=AssertionError("No live GET in tests")
            )
        )
        self.enterContext(
            patch.object(
                s.HTTP, "post", side_effect=AssertionError("No ingestion in tests")
            )
        )

    def offline_request(self, method, url, **kwargs):
        # Snapshot params now: the retry may mutate the original dict in place.
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request; live traffic is forbidden")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def healthy_google(self):
        self.responses = [response(payload={"places": []})]
        self.assertEqual(
            s.google_search("restaurant in Edmonton, Alberta", limit=1), []
        )
        self.assertEqual(
            s.METRICS["provider_successes_by_provider"]["Google Places"], 1
        )
        self.http.reset_mock()
        self.calls.clear()

    def assert_no_google_fallback(self, operation):
        with self.assertRaises(s.PipelineError):
            operation()
        self.assertEqual(s.YELP_DISABLED_REASON, "")
        self.assertEqual(s.METRICS["provider_successes_by_provider"].get("Yelp", 0), 0)

    def test_provider_unavailable_error_is_a_pipeline_error(self):
        self.assertTrue(issubclass(s.ProviderUnavailableError, s.PipelineError))

    def test_success_counts_are_by_provider_and_only_after_valid_json(self):
        self.responses = [
            response(payload={"places": []}),
            response(payload={"businesses": []}),
        ]
        self.assertEqual(
            s.google_search("restaurant in Edmonton, Alberta", limit=1), []
        )
        self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
        self.assertEqual(s.METRICS["provider_requests"], 2)
        self.assertEqual(s.METRICS["provider_successes"], 2)
        self.assertEqual(
            s.METRICS["provider_successes_by_provider"], {"Google Places": 1, "Yelp": 1}
        )
        self.assertEqual(s.METRICS["provider_errors"], [])

    def test_yelp_400_recovers_once_without_attributes_or_categories(self):
        self.responses = [
            yelp_error(400, "VALIDATION_ERROR", "Unsupported filter"),
            response(
                payload={
                    "businesses": [
                        {
                            "id": "fixture-only",
                            "name": "Synthetic Resilience Kitchen",
                            "location": {"state": "AB", "city": "Edmonton"},
                            "categories": [],
                            "is_closed": False,
                        }
                    ]
                }
            ),
        ]
        result = s.yelp_search(city="Edmonton", hot=True)
        self.assertEqual(
            [item["name"] for item in result], ["Synthetic Resilience Kitchen"]
        )
        self.assertEqual(self.http.call_count, 2)
        first, retry = [call[2]["params"] for call in self.calls]
        self.assertIn("attributes", first)
        self.assertIn("categories", first)
        self.assertNotIn("attributes", retry)
        self.assertNotIn("categories", retry)
        self.assertEqual(
            retry,
            {
                key: value
                for key, value in first.items()
                if key not in {"attributes", "categories"}
            },
        )
        self.assertEqual(s.YELP_DISABLED_REASON, "")
        self.assertEqual(s.METRICS["provider_successes_by_provider"]["Yelp"], 1)
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)

    def test_yelp_400_twice_disables_with_healthy_google_and_stops_http(self):
        self.healthy_google()
        self.responses = [yelp_error(400), yelp_error(400)]
        with self.assertLogs(s.LOG, level="WARNING") as logged:
            self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
        self.assertTrue(s.YELP_DISABLED_REASON)
        self.assertTrue(any("yelp" in line.lower() for line in logged.output))
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(len(s.METRICS["provider_errors"]), 2)
        reason = s.YELP_DISABLED_REASON
        self.assertEqual(s.yelp_search(city="Calgary", hot=True), [])
        self.assertEqual(s.yelp_search("Synthetic Resilience Kitchen", "Edmonton"), [])
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(s.YELP_DISABLED_REASON, reason)

    def test_yelp_400_twice_without_google_is_fatal(self):
        s.GOOGLE_MAPS_API_KEY = ""
        self.responses = [yelp_error(400), yelp_error(400)]
        self.assert_no_google_fallback(lambda: s.yelp_search(city="Edmonton", hot=True))
        self.assertEqual(self.http.call_count, 2)

    def test_yelp_401_without_google_is_fatal_and_not_retried(self):
        s.GOOGLE_MAPS_API_KEY = ""
        self.responses = [yelp_error(401, "TOKEN_INVALID", "Invalid authorization")]
        self.assert_no_google_fallback(lambda: s.yelp_search(city="Edmonton", hot=True))
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)

    def test_configured_but_unverified_google_does_not_allow_fallback(self):
        self.responses = [yelp_error(401)]
        self.assert_no_google_fallback(lambda: s.yelp_search(city="Edmonton", hot=True))
        self.assertEqual(self.http.call_count, 1)

    def test_yelp_success_does_not_substitute_for_google_health(self):
        self.responses = [response(payload={"businesses": []}), yelp_error(401)]
        self.assertEqual(s.yelp_search(city="Edmonton"), [])
        with self.assertRaises(s.PipelineError):
            s.yelp_search(city="Edmonton", hot=True)
        self.assertEqual(s.YELP_DISABLED_REASON, "")
        self.assertEqual(self.http.call_count, 2)

    def test_google_success_count_without_configured_key_does_not_allow_fallback(self):
        self.healthy_google()
        s.GOOGLE_MAPS_API_KEY = ""
        self.responses = [yelp_error(429)]
        self.assert_no_google_fallback(lambda: s.yelp_search(city="Edmonton", hot=True))
        self.assertEqual(self.http.call_count, 1)

    def test_google_403_is_fatal_even_after_prior_google_success(self):
        self.healthy_google()
        self.responses = [
            response(
                403,
                {
                    "error": {
                        "code": 403,
                        "status": "PERMISSION_DENIED",
                        "message": "API not enabled",
                    }
                },
            )
        ]
        with self.assertRaises(s.ProviderUnavailableError) as raised:
            s.google_search("restaurant in Edmonton, Alberta", limit=1)
        self.assertEqual(raised.exception.provider, "Google Places")
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("API not enabled", raised.exception.description)
        self.assertEqual(s.YELP_DISABLED_REASON, "")
        self.assertEqual(self.http.call_count, 1)

    def test_yelp_429_with_healthy_google_disables_without_retry(self):
        self.healthy_google()
        self.responses = [yelp_error(429, "ACCESS_LIMIT_REACHED", "Quota exhausted")]
        with self.assertLogs(s.LOG, level="WARNING"):
            self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(s.YELP_DISABLED_REASON)
        self.assertEqual(s.yelp_search(city="Calgary", hot=True), [])
        self.assertEqual(self.http.call_count, 1)

    def test_yelp_auth_and_server_outages_with_google_disable_without_retry(self):
        self.healthy_google()
        for status in (401, 403, 500, 502, 503):
            with self.subTest(status=status):
                s.YELP_DISABLED_REASON = ""
                self.responses = [yelp_error(status)]
                before = self.http.call_count
                with self.assertLogs(s.LOG, level="WARNING"):
                    self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
                self.assertEqual(self.http.call_count, before + 1)
                self.assertTrue(s.YELP_DISABLED_REASON)

    def test_yelp_network_failure_without_google_fails_closed(self):
        s.GOOGLE_MAPS_API_KEY = ""
        self.responses = [requests.ConnectionError("Offline connection failed")]
        self.assert_no_google_fallback(lambda: s.yelp_search(city="Edmonton", hot=True))
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)

    def test_yelp_network_failure_with_google_disables_without_retry(self):
        self.healthy_google()
        self.responses = [requests.Timeout("Offline request timed out")]
        with self.assertLogs(s.LOG, level="WARNING"):
            self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(s.YELP_DISABLED_REASON)

    def test_budget_exhaustion_is_plain_pipeline_error_without_http_or_fallback(self):
        self.healthy_google()
        s.METRICS["provider_requests"] = s.MAX_PROVIDER_REQUESTS
        before = copy.deepcopy(s.METRICS)
        with self.assertRaises(s.PipelineError) as raised:
            s.yelp_search(city="Edmonton", hot=True)
        self.assertIs(type(raised.exception), s.PipelineError)
        self.http.assert_not_called()
        self.assertEqual(s.YELP_DISABLED_REASON, "")
        self.assertEqual(s.METRICS, before)

    def test_budget_exhaustion_during_400_retry_is_not_swallowed(self):
        self.healthy_google()
        s.MAX_PROVIDER_REQUESTS = s.METRICS["provider_requests"] + 1
        self.responses = [yelp_error(400)]
        with self.assertRaises(s.PipelineError) as raised:
            s.yelp_search(city="Edmonton", hot=True)
        self.assertIs(type(raised.exception), s.PipelineError)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(s.YELP_DISABLED_REASON, "")
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)

    def test_plain_pipeline_error_is_not_retried_or_used_as_fallback(self):
        self.healthy_google()
        error = s.PipelineError("Offline non-provider invariant failure")
        with patch.object(s, "provider_request", side_effect=error) as request:
            with self.assertRaises(s.PipelineError) as raised:
                s.yelp_search(city="Edmonton", hot=True)
            self.assertIs(raised.exception, error)
            request.assert_called_once()
        self.http.assert_not_called()
        self.assertEqual(s.YELP_DISABLED_REASON, "")

    def test_error_details_preserve_code_and_description_but_redact_all_keys(self):
        secrets = [
            s.GOOGLE_MAPS_API_KEY,
            s.YELP_API_KEY,
            s.OPENAI_API_KEY,
            s.INGEST_API_KEY,
        ]
        description = "Offline diagnostic echoed " + " ".join(secrets)
        self.responses = [yelp_error(401, "TOKEN_INVALID", description)]
        with self.assertRaises(s.ProviderUnavailableError) as raised:
            s.provider_request("Yelp", "GET", "https://api.example/test")
        error = raised.exception
        self.assertEqual(error.provider, "Yelp")
        self.assertEqual(error.status, 401)
        self.assertEqual(error.code, "TOKEN_INVALID")
        self.assertIn("Offline diagnostic echoed", error.description)
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)
        detail = s.METRICS["provider_errors"][0]
        self.assertEqual(detail["provider"], "Yelp")
        self.assertEqual(detail["http_status"], 401)
        self.assertEqual(detail["code"], "TOKEN_INVALID")
        self.assertIn("Offline diagnostic echoed", detail["description"])
        visible = str(error) + repr(vars(error)) + json.dumps(s.METRICS)
        for secret in secrets:
            self.assertNotIn(secret, visible)
        self.assertEqual(s.METRICS["provider_successes"], 0)
        self.assertEqual(s.METRICS["provider_successes_by_provider"], {})

    def test_network_error_details_redact_configured_keys(self):
        self.responses = [
            requests.ConnectionError(
                "Offline failure " + s.YELP_API_KEY + " " + s.GOOGLE_MAPS_API_KEY
            )
        ]
        with self.assertRaises(s.ProviderUnavailableError) as raised:
            s.provider_request("Yelp", "GET", "https://api.example/test")
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)
        visible = (
            str(raised.exception) + repr(vars(raised.exception)) + json.dumps(s.METRICS)
        )
        self.assertNotIn(s.YELP_API_KEY, visible)
        self.assertNotIn(s.GOOGLE_MAPS_API_KEY, visible)
        self.assertEqual(s.METRICS["provider_successes"], 0)

    def test_invalid_json_is_provider_unavailable_not_empty_success(self):
        self.responses = [response(text="{not valid json")]
        with self.assertRaises(s.ProviderUnavailableError) as raised:
            s.provider_request("Google Places", "POST", "https://api.example/test")
        self.assertEqual(raised.exception.provider, "Google Places")
        self.assertEqual(len(s.METRICS["provider_errors"]), 1)
        self.assertEqual(s.METRICS["provider_successes"], 0)
        self.assertEqual(s.METRICS["provider_successes_by_provider"], {})

    def test_yelp_invalid_json_without_google_fails_closed_and_does_not_retry(self):
        s.GOOGLE_MAPS_API_KEY = ""
        self.responses = [
            response(text="<html>offline invalid provider response</html>")
        ]
        self.assert_no_google_fallback(lambda: s.yelp_search(city="Edmonton", hot=True))
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(s.METRICS["provider_successes"], 0)

    def test_missing_yelp_key_makes_no_http_request(self):
        s.YELP_API_KEY = ""
        self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
        self.http.assert_not_called()
        self.assertEqual(s.METRICS["provider_requests"], 0)

    def test_disabled_yelp_makes_no_http_request(self):
        s.YELP_DISABLED_REASON = "Offline prior provider outage"
        self.assertEqual(s.yelp_search(city="Edmonton", hot=True), [])
        self.http.assert_not_called()
        self.assertEqual(s.METRICS["provider_requests"], 0)

    def test_preflight_calls_google_first_then_yelp_with_exact_probe_arguments(self):
        order = []
        with patch.object(
            s,
            "google_search",
            side_effect=lambda *args, **kwargs: order.append(("Google", args, kwargs))
            or [],
        ) as google, patch.object(
            s,
            "yelp_search",
            side_effect=lambda *args, **kwargs: order.append(("Yelp", args, kwargs))
            or [],
        ) as yelp:
            s.preflight_providers()
        google.assert_called_once_with("restaurant in Edmonton, Alberta", limit=1)
        yelp.assert_called_once_with(city="Edmonton", hot=True)
        self.assertEqual([item[0] for item in order], ["Google", "Yelp"])
        self.http.assert_not_called()

    def test_preflight_google_403_stops_before_yelp(self):
        self.responses = [
            response(
                403, {"error": {"code": 403, "message": "Offline permission denied"}}
            )
        ]
        with self.assertRaises(s.PipelineError):
            s.preflight_providers()
        self.assertEqual(self.http.call_count, 1)
        self.assertIn("places.googleapis.com", self.calls[0][1])
        self.assertEqual(s.YELP_DISABLED_REASON, "")

    def test_preflight_google_health_allows_yelp_outage(self):
        self.responses = [response(payload={"places": []}), yelp_error(401)]
        with self.assertLogs(s.LOG, level="WARNING"):
            s.preflight_providers()
        self.assertEqual(self.http.call_count, 2)
        self.assertIn("places.googleapis.com", self.calls[0][1])
        self.assertIn("api.yelp.com", self.calls[1][1])
        self.assertEqual(
            s.METRICS["provider_successes_by_provider"], {"Google Places": 1}
        )
        self.assertTrue(s.YELP_DISABLED_REASON)

    def test_preflight_yelp_outage_without_google_is_fatal(self):
        s.GOOGLE_MAPS_API_KEY = ""
        self.responses = [yelp_error(503)]
        self.assert_no_google_fallback(s.preflight_providers)
        self.assertEqual(self.http.call_count, 1)
        self.assertIn("api.yelp.com", self.calls[0][1])

    def test_preflight_only_probes_configured_providers(self):
        for google_key, yelp_key in (
            ("", ""),
            ("fixture-google", ""),
            ("", "fixture-yelp"),
        ):
            with self.subTest(
                google=bool(google_key), yelp=bool(yelp_key)
            ), patch.object(s, "GOOGLE_MAPS_API_KEY", google_key), patch.object(
                s, "YELP_API_KEY", yelp_key
            ), patch.object(
                s, "google_search", return_value=[]
            ) as google, patch.object(
                s, "yelp_search", return_value=[]
            ) as yelp:
                if google_key or yelp_key:
                    s.preflight_providers()
                else:
                    with self.assertRaises(s.PipelineError):
                        s.preflight_providers()
                self.assertEqual(google.call_count, int(bool(google_key)))
                self.assertEqual(yelp.call_count, int(bool(yelp_key)))
        self.http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
