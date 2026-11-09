# Culinera Scrapling Discovery 3.0

This is a real, bounded website crawler plus evidence/verification pipeline, not a fixed list of restaurants. Start with `START_HERE.md` in the delivery archive for click-by-click installation.

## Discovery and gates

`scrapling_discovery.py` uses Scrapling `Fetcher.get` and CSS/XPath parsing to crawl configured public Alberta food-news and regional tourism pages. Roots and RSS feeds seed links; full individual pages provide evidence. No login, proxy pool or anti-bot challenge bypass is used. Robots rules, crawl delays, per-source page caps, depth caps and a crawl time limit are respected. Adding roots to `sources.json` changes where it looks, not the list of businesses it submits.

The main script extracts literal business names and exact source quotations. Every extracted name/quote must occur in source text. It matches Google Places API (New) and/or Yelp records in Alberta with name/city checks. Both Edmonton and Calgary get a Yelp `hot_and_new` lookup on every full run. Optional region-wide Google searches supplement the crawl, alternating northern/central and southern/mountain location groups, or both groups in a manual full-province run. News/tourism crawling does not alternate by city.

Normal cards need at least 4.0 stars and 10 reviews from the SAME provider, plus specific culinary-experience evidence. A small-town address alone does not establish a hidden gem. A new-opening exception needs dated recent reporting that says it is already open, an independently corroborating provider listing and a fetched official presence with matching identity. A qualifying exception is not blocked by an arbitrary review-score cutoff. Future/closed businesses are held. Chains and ordinary diners/fast-food formats are excluded. Food trucks need a verified official presence and matching listing.

Descriptions are generated only from fact packs and quoted evidence. Long copied sequences and highly similar descriptions are rejected. A separate factual-support pass holds descriptions with unsupported claims. These safeguards reduce copying/hallucination risk; they are not a global plagiarism certificate or proof of every fact. Human approval remains required.

Images must come from a fetched, identity-verified official website. Actual downloadable image data, dimensions, visual subject, URL identity and perceptual hashes are checked. No Unsplash pool, generic stock, AI-generated venue photos or stored Google/Yelp photo fallback is used. One image is submitted because Culinera's current schema exposes one `image_url`. A visual check cannot identify the venue or grant usage rights by itself. The editorial reviewer must confirm permissions and attribution before publishing.

The pipeline holds all otherwise-ready cards if any existing directory image is unreadable and lacks a previously saved fingerprint. This strict policy avoids pretending it can compare against images it cannot see. The report still shows discovered leads. Repeated, heavily transformed/cropped images are not guaranteed detectable by a perceptual hash; server-side image review and fingerprinting across the full admin inventory remain important. See `EMERGENT_IMAGE_AND_HISTORY_REQUEST.md` in the archive.

## Files

Run `python alberta_culinary_scraper.py`. Keep `scrapling_discovery.py`, `image_guard.py`, `sources.json` and `requirements.txt` beside it. Tests live in `test_pipeline.py` and `test_discovery.py`. `smoke_test.py` performs only a small read-only crawl and published-directory GET.

Install with Python 3.11:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -v
python smoke_test.py
```

Scrapling's `fetchers` extra is required. Plain `pip install scrapling` only installs its parser, not the fetchers. Chromium/browser installation is not needed: this package uses static fetchers only. JS-only/protected pages are recorded as unavailable or held instead of bypassing access restrictions.

## Environment

- `GOOGLE_MAPS_API_KEY`: Google Places API (New), with billing and proper API/key restrictions. At least one Google/Yelp verifier is needed.
- `YELP_API_KEY`: optional second verifier and hot-and-new discovery.
- `OPENAI_API_KEY`: required for a full preview/production run; not required for a discovery-only test.
- `OPENAI_MODEL`: default `gpt-4o-mini` for the user's own OpenAI provider account, configurable via a GitHub repository variable. Must support text, image input and JSON object output. A model/key/billing error stops the run with a report rather than becoming an empty result.
- `INGEST_API_KEY`: rotated production ingestion key. Never commit it.
- `DRY_RUN`: default `true`. Never POST in preview.
- `DISCOVERY_ONLY`: default `false`. When true, crawl/documents only; no AI, Google, Yelp or ingestion calls.
- `LIVE_SUBMISSIONS_ENABLED`: default `false`. A separate live-write lock. In GitHub it is a repository variable; set it to `true` only after reviewing the preview.
- `ENABLE_REGIONAL_API_DISCOVERY`: default `false` locally; enabled in the supplied workflow. Additional per-town Google discovery. Raises cost.
- `SCAN_ALL_ALBERTA`: default `false`; only affects supplemental regional API group coverage, not the province-wide source crawl.
- `MAX_PROVIDER_REQUESTS`: 180 by default. Explicit combined Google/Yelp request cap; credential/request/quota errors fail visibly.
- `MAX_LEADS`: 50; `MAX_AI_DOCUMENTS`: 48; `MAX_CANDIDATES_PER_RUN`: 24.
- `NEWS_LOOKBACK_DAYS`: 90. Undated articles do not qualify for the new-opening review-count exception.
- `STATE_DIR`: `.culinera-state`; `REPORT_PATH`: `culinera_candidates.json` by default.
- `CULINERA_OPENAI_BASE_URL`: optional explicit OpenAI-compatible provider base URL. Do not assume any platform's internal key works with OpenAI directly. GitHub users normally leave the default `https://api.openai.com/v1`.

Paid API limits are request bounds, not dollar budgets. Use provider billing alerts/caps. A full preview uses paid extraction, Places/Yelp checks, photo classification and original-copy validation. Discovery-only uses no paid APIs.

## Outputs and interpretation

`culinera_candidates.json` contains crawl/source health, all discovered leads, eligibility holds, duplicate rejections, unreadable old images, ready payloads and the actual ingestion response. A prepared-card count is NOT a submitted/created count. Read `ingestion_result.created` for new queue records. A preview never adds admin cards.

API configuration errors and unavailable verifiers produce a failed workflow with a report. Zero ready cards with valid source/provider access is a distinct `no_ready_cards` outcome. It can mean duplicate venues, quality filters, future openings, missing official photos or old unhashable images—not necessarily no openings in Alberta.

`confidence` is a conservative rule-based review score, not a calibrated probability that the restaurant is special or that every fact is true.

## Schedule and state

The workflow runs on the 1st and 15th at 15:00 UTC. Until the live variable is enabled, scheduled runs stay previews. GitHub schedules may run late; a public repository's scheduled workflows may be disabled after a period of inactivity. If nothing runs, inspect the Actions schedule status as well as discovery reports. Manual dry runs are unlimited but still have API costs.

An Actions cache restores prior submitted names/cities and image hashes. Cache eviction or branch changes can lose history. Backend name+city deduplication still prevents duplicate venue creation, but the public API cannot provide a full pending/rejected inventory. An authenticated complete-inventory endpoint and server-side perceptual image checks are the recommended durable solution. Do not treat an Actions cache as a permanent database.

The external photo URL is tested with the Culinera origin as Referer. This is a fetch-time check, not a guarantee the browser card will always work. Emergent should rehost permission-cleared official images into Culinera storage and test the admin/public render. Google Maps photo-name caching/expiry and attribution requirements make blind permanent fallback unsuitable for the current schema.

No cards have been ingested or auto-published merely by creating or testing this package. Install it in the repository, preview it, repair backend/image blockers, then unlock live submissions to the existing manual queue.

## References

- Scrapling: https://github.com/d4vinci/Scrapling
- Static fetchers: https://scrapling.readthedocs.io/en/latest/fetching/static.html
- Scrapling crawling/robots guidance: https://scrapling.readthedocs.io/en/latest/spiders/getting-started.html
- Google Places photo requirements: https://developers.google.com/maps/documentation/places/web-service/place-photos
- Google Places policies: https://developers.google.com/maps/documentation/places/web-service/policies
