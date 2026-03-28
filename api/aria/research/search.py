"""
ARIA - Research Search Providers

Purpose: Search provider abstraction for research tasks.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from urllib.parse import quote_plus

import httpx

from aria.config import settings


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Search for results relevant to a query."""


class BraveSearchProvider(SearchProvider):
    """Brave Search API provider."""

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        if not settings.brave_search_api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY is not configured")

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                settings.brave_search_url,
                params={"q": query, "count": max_results},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": settings.brave_search_api_key,
                },
            )
            response.raise_for_status()
            payload = response.json()

        results = []
        for item in payload.get("web", {}).get("results", [])[:max_results]:
            results.append(
                SearchResult(
                    title=item.get("title", item.get("url", "")),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                )
            )
        return results


class DuckDuckGoSearchProvider(SearchProvider):
    """Lightweight HTML fallback provider."""

    _result_pattern = re.compile(
        r'<a rel="nofollow" class="result__a" href="(?P<url>[^"]+)">(?P<title>.*?)</a>.*?'
        r'<a class="result__snippet".*?>(?P<snippet>.*?)</a>',
        re.DOTALL,
    )
    _tag_pattern = re.compile(r"<.*?>")

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "ARIA/0.2.0"})
            response.raise_for_status()
            html = response.text

        results = []
        for match in self._result_pattern.finditer(html):
            results.append(
                SearchResult(
                    title=self._clean_html(match.group("title")),
                    url=match.group("url"),
                    snippet=self._clean_html(match.group("snippet")),
                )
            )
            if len(results) >= max_results:
                break
        return results

    def _clean_html(self, value: str) -> str:
        return re.sub(r"\s+", " ", self._tag_pattern.sub("", value)).strip()


def get_search_provider() -> SearchProvider:
    """Resolve the configured search provider."""
    if settings.brave_search_api_key:
        return BraveSearchProvider()
    return DuckDuckGoSearchProvider()
