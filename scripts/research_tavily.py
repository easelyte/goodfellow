#!/usr/bin/env python3
"""Batch-verify factual claims via Tavily Search API.

Requires GOODFELLOW_TAVILY_KEY. Each claim becomes one search query.
Output: markdown appendix with verified (✓) or unverifiable (?) labels.
Limitation: cannot detect refutation — word-overlap heuristic confirms relevance,
not agreement. A contradicting source scores the same as a confirming one.
Refutation detection requires LLM judgment (future enhancement).
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


def search_tavily(query, api_key, max_results=3):
    """Single Tavily search. Returns list of {title, url, content} dicts."""
    payload = json.dumps(
        {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                }
                for r in data.get("results", [])
            ]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return [{"title": "ERROR", "url": "", "content": str(e)}]


def verify_claims(claims, api_key, max_searches=5):
    results = []
    for claim in claims[:max_searches]:
        search_results = search_tavily(claim, api_key)
        if not search_results or search_results[0].get("title") == "ERROR":
            results.append(
                {
                    "claim": claim,
                    "status": "?",
                    "detail": "Search failed or no results",
                    "url": "",
                }
            )
            continue

        top = search_results[0]
        content_lower = top["content"].lower()
        claim_words = set(claim.lower().split())

        overlap = len(claim_words & set(content_lower.split())) / max(
            len(claim_words), 1
        )
        if overlap > 0.3:
            results.append(
                {
                    "claim": claim,
                    "status": "✓",
                    "detail": top["content"][:200],
                    "url": top["url"],
                }
            )
        else:
            results.append(
                {
                    "claim": claim,
                    "status": "?",
                    "detail": f"Low relevance (top result: {top['title']})",
                    "url": top["url"],
                }
            )

    return results


def format_markdown(results):
    lines = [f"## Verified Claims (Tavily research pass)", ""]
    for r in results:
        lines.append(f"{r['status']} **{r['claim']}**")
        if r["url"]:
            lines.append(f"  Source: {r['url']}")
        lines.append(f"  {r['detail']}")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--claims", required=True, help="JSON array of claim strings")
    parser.add_argument("--max", type=int, default=5)
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()

    claims = json.loads(args.claims)
    results = verify_claims(claims, args.api_key, args.max)
    print(format_markdown(results))


if __name__ == "__main__":
    main()
