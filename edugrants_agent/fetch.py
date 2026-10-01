"""Fetching pages and turning them into clean text plus a list of outbound links."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup

from .config import settings
from .dedupe import domain

log = logging.getLogger(__name__)

MAX_TEXT = 14_000  # characters sent to the model per page

SKIP_LINK_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "t.me",
    "youtube.com", "whatsapp.com", "pinterest.com", "wa.me", "api.whatsapp.com",
    "reddit.com", "tiktok.com", "wordpress.org", "gravatar.com",
}


@dataclass
class Page:
    url: str
    final_url: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (anchor text, absolute url)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def _client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(25.0, connect=10.0),
        headers={"User-Agent": settings.user_agent, "Accept-Language": "en,uz;q=0.8,ru;q=0.6"},
    )


FEED_READER_UA = "Feedly/1.0 (+http://www.feedly.com/fetcher.html; like FeedFetcher-Google)"


def fetch_raw(url: str, accept: str | None = None, user_agent: str | None = None) -> tuple[str, str]:
    headers = {}
    if accept:
        headers["Accept"] = accept
    if user_agent:
        headers["User-Agent"] = user_agent
    with _client() as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        return r.text, str(r.url)


def html_to_page(url: str, final_url: str, html: str) -> Page:
    text = trafilatura.extract(html, include_links=False, include_tables=True, favor_recall=True) or ""
    if len(text) < 200:  # trafilatura sometimes misses form-like pages; fall back to all visible text
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
    links = extract_links(html, final_url)
    return Page(url=url, final_url=final_url, text=text[:MAX_TEXT], links=links)


def extract_links(html: str, base_url: str, limit: int = 60) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    own = domain(base_url)
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not href.startswith("http"):
            continue
        d = domain(href)
        if d == own or d in SKIP_LINK_DOMAINS or any(d.endswith("." + s) for s in SKIP_LINK_DOMAINS):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((" ".join(a.get_text(" ").split())[:80], href))
        if len(out) >= limit:
            break
    return out


def fetch_page(url: str) -> Page:
    html, final = fetch_raw(url)
    return html_to_page(url, final, html)
