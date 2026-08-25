# services/ai-service/skills/search_skill.py
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

log = logging.getLogger("ai.skills.search")


class SearchSkill:
    def __init__(self, max_results: int = 3, timeout_s: float = 3.5):
        self.max_results = max_results
        self.timeout_s = timeout_s

    def search(self, query: str) -> Dict[str, Any]:
        """Searches live facts, news, and world info via DuckDuckGo & Wikipedia."""
        clean_query = str(query or "").strip()
        if not clean_query:
            return {"error": "Empty search query"}

        results = []

        # 1. DuckDuckGo Instant Answer API
        try:
            ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(clean_query)}&format=json&no_html=1&skip_disambig=1"
            req = urllib.request.Request(ddg_url, headers={"User-Agent": "HexapodAI/2.0 (Smart Robot Assistant)"})
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            abstract = data.get("AbstractText", "").strip()
            answer = data.get("Answer", "").strip()
            heading = data.get("Heading", "").strip()

            if answer:
                results.append({"title": heading or "Direct Answer", "snippet": answer})
            elif abstract:
                results.append({"title": heading or "Summary", "snippet": abstract})

            # Check Related Topics
            for topic in data.get("RelatedTopics", [])[:2]:
                if isinstance(topic, dict) and "Text" in topic:
                    results.append({"title": "Related Fact", "snippet": topic["Text"]})
        except Exception as e:
            log.debug("DuckDuckGo Instant Answer lookup failed: %s", e)

        # 2. Wikipedia Search API Fallback
        if len(results) < self.max_results:
            try:
                wiki_url = (
                    f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(clean_query)}"
                    f"&utf8=&format=json&srlimit={self.max_results}"
                )
                req = urllib.request.Request(wiki_url, headers={"User-Agent": "HexapodAI/2.0 (Smart Robot Assistant)"})
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    wiki_data = json.loads(resp.read().decode("utf-8"))

                search_hits = wiki_data.get("query", {}).get("search", [])
                for hit in search_hits:
                    title = hit.get("title", "")
                    raw_snippet = hit.get("snippet", "")
                    clean_snippet = re.sub(r"<[^>]+>", "", raw_snippet).strip()
                    if clean_snippet and not any(r["title"] == title for r in results):
                        results.append({"title": title, "snippet": clean_snippet})
                    if len(results) >= self.max_results:
                        break
            except Exception as e:
                log.debug("Wikipedia search lookup failed: %s", e)

        if not results:
            return {
                "query": clean_query,
                "status": "no_results",
                "summary": "No live web results found for this query.",
            }

        return {
            "query": clean_query,
            "status": "success",
            "results": results[:self.max_results],
        }