"""Offline tests only: these synthetic fixtures never perform discovery ingestion."""

from __future__ import annotations

import unittest

from scrapling import Selector

import scrapling_discovery as discovery

SYNTHETIC_ARTICLE_HTML = """<!doctype html>
<html><head>
  <title>Synthetic title fallback</title>
  <meta property="article:published_time" content="2026-10-15T12:30:00-06:00">
  <meta property="og:title" content="Synthetic Alberta food story">
  <meta property="og:image" content="/images/synthetic-hero.jpg">
  <script type="application/ld+json">
  {"@context":"https://schema.org","@graph":[
    {"@type":"Restaurant","name":"Synthetic Supper Club","url":"https://synthetic-supper.example/","address":{"@type":"PostalAddress","addressLocality":"Calgary"}},
    {"@type":"Organization","name":"Synthetic Newsroom"}
  ]}
  </script>
</head><body>
  <nav>Home Restaurants Subscribe</nav>
  <article>
    <h1>Synthetic article heading</h1>
    <p>A synthetic paragraph about a public Alberta food event.</p>
    <p>It contains enough article body content to select the article instead of the page navigation.</p>
    <a href="https://synthetic-supper.example/">Synthetic Supper Club</a>
    <a href="https://directory.example/restaurants">Restaurants</a>
    <script>this text must not appear</script>
  </article>
  <footer>Privacy newsletter links</footer>
</body></html>"""


class SyntheticResponse:
    """Small Scrapling-selector backed response fixture; no HTTP is ever made."""

    def __init__(self, html: str, url: str = "https://source.synthetic/story") -> None:
        self.selector = Selector(html, url=url)
        self.body = html.encode("utf-8")
        self.url = url
        self.status = 200

    def css(self, query: str):
        return self.selector.css(query)


class DiscoveryParsingTests(unittest.TestCase):
    def test_synthetic_article_preserves_evidence_and_excludes_layout(self) -> None:
        response = SyntheticResponse(SYNTHETIC_ARTICLE_HTML)
        document = discovery._document_from_page(
            response, "Synthetic Newsroom", "https://source.synthetic/"
        )

        self.assertEqual(document["title"], "Synthetic Alberta food story")
        self.assertEqual(document["published"], "2026-10-15T12:30:00-06:00")
        self.assertIn("synthetic paragraph", document["text"])
        self.assertNotIn("Home Restaurants Subscribe", document["text"])
        self.assertNotIn("this text must not appear", document["text"])
        self.assertEqual(
            document["links"],
            [
                {
                    "name": "Synthetic Supper Club",
                    "url": "https://synthetic-supper.example/",
                }
            ],
        )
        self.assertEqual(
            document["structured_candidates"],
            [
                {
                    "name": "Synthetic Supper Club",
                    "city": "Calgary",
                    "website": "https://synthetic-supper.example/",
                    "provenance": "json-ld:restaurant; https://source.synthetic/story",
                }
            ],
        )

    def test_synthetic_json_ld_source_name_is_not_a_candidate(self) -> None:
        html = """<html><head><script type='application/ld+json'>
        {"@type":"LocalBusiness","name":"Synthetic Tourism","url":"https://source.synthetic/"}
        </script></head><body><article><p>Long enough synthetic content for selection.</p></article></body></html>"""
        response = SyntheticResponse(html)
        document = discovery._document_from_page(
            response, "Synthetic Tourism", "https://source.synthetic/"
        )
        self.assertEqual(document["structured_candidates"], [])

    def test_robots_rules_and_same_domain_interest_are_offline(self) -> None:
        rules = discovery._parse_robots("""
            User-agent: *
            Crawl-delay: 4
            Disallow: /private/
            Allow: /private/public-story
        """)
        self.assertEqual(rules.crawl_delay, 4.0)
        self.assertFalse(
            rules.can_fetch(
                discovery.USER_AGENT, "https://source.synthetic/private/hidden"
            )
        )
        self.assertTrue(
            rules.can_fetch(
                discovery.USER_AGENT, "https://source.synthetic/private/public-story"
            )
        )
        self.assertTrue(
            discovery._is_interesting_same_domain_link(
                "/food/stories?page=2",
                "Older food stories",
                "https://source.synthetic/food/",
                "https://source.synthetic/",
            )
        )
        self.assertFalse(
            discovery._is_interesting_same_domain_link(
                "https://other.synthetic/food",
                "Food",
                "https://source.synthetic/",
                "https://source.synthetic/",
            )
        )
        self.assertFalse(
            discovery._is_interesting_same_domain_link(
                "/login",
                "Login",
                "https://source.synthetic/",
                "https://source.synthetic/",
            )
        )

    def test_current_dated_feed_urls_outrank_old_homepage_sidebar_urls(self) -> None:
        ordered = discovery._recent_first_urls(
            [
                "https://source.synthetic/2024/05/27/old-popular-story/",
                "https://source.synthetic/2016/09/05/very-old-sidebar-story/",
                "https://source.synthetic/2026/10/26/current-feed-story/",
                "https://source.synthetic/2026/10/19/last-weeks-feed-story/",
                "https://source.synthetic/category/food/",
            ]
        )
        self.assertEqual(
            ordered[:2],
            [
                "https://source.synthetic/2026/10/26/current-feed-story/",
                "https://source.synthetic/2026/10/19/last-weeks-feed-story/",
            ],
        )
        self.assertLess(
            ordered.index("https://source.synthetic/2024/05/27/old-popular-story/"),
            ordered.index(
                "https://source.synthetic/2016/09/05/very-old-sidebar-story/"
            ),
        )


if __name__ == "__main__":
    unittest.main()
