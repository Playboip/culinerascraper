"""Run this freely: no ingestion, Google, Yelp or OpenAI calls."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import requests
from scrapling_discovery import collect_documents


def main():
    config = {
        "crawl": {
            "max_pages": 8,
            "max_pages_per_source": 4,
            "max_depth": 2,
            "min_domain_delay_seconds": 2,
        },
        "sources_food_news": [
            {
                "name": "Only Here for the Food",
                "root": "https://onlyhereforthefood.ca/",
                "feed": "https://onlyhereforthefood.ca/feed/",
            },
            {
                "name": "Avenue Calgary Dining",
                "root": "https://www.avenuecalgary.com/restaurants-food/",
                "feed": "https://www.avenuecalgary.com/feed/",
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, "sources.json")
        p.write_text(json.dumps(config))
        docs, stats = collect_documents(p, max_pages=8)
    directory = {"url": "https://seo-phase.emergent.host/api/restaurants"}
    try:
        r = requests.get(directory["url"], timeout=18)
        directory["http_status"] = r.status_code
        directory["published_record_count"] = (
            len(r.json()) if r.ok and isinstance(r.json(), list) else None
        )
    except requests.RequestException as e:
        directory["error"] = type(e).__name__
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY",
        "ingestion_calls": 0,
        "paid_api_calls": 0,
        "crawl": stats,
        "directory": directory,
        "documents": docs,
    }
    Path("scrapling_smoke_report.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    print(
        f"Read-only test: {len(docs)} real source documents; directory={directory}; submitted=0"
    )
    for d in docs:
        print(d.get("published"), d["title"], d["url"])
    if not docs:
        raise SystemExit(
            "No documents: inspect source errors in scrapling_smoke_report.json"
        )


if __name__ == "__main__":
    main()
