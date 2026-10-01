"""Duplicate detection.

The same grant shows up on many aggregator sites with slightly different titles
and different URLs. Three layers catch it:
1. canonical URL (strip tracking params, www, trailing slash)
2. fuzzy title match against everything seen recently
3. after extraction: same official page URL (the strongest signal)
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz import fuzz

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|source$|_gl$|_ga$)", re.I)

# Words aggregators add to titles that don't identify the programme
NOISE = re.compile(
    r"\b(applications?|apply|now|open|opens?|call for|calls?|announced?|fully[- ]funded|funded|"
    r"partially[- ]funded|scholarships? program(me)?|deadline|extended|new|free|online|"
    r"20[2-3]\d(?:[-/–]20?[2-3]?\d)?|\d{4}/\d{2,4})\b",
    re.I,
)


def canonical_url(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", parts.path) or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)))
    return urlunsplit(("https", host, path, query, ""))


def domain(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


PREFIX = re.compile(
    r"^\s*(applications? (are )?(now )?open( for)?|call for (applications|proposals|entries)|apply now|"
    r"now open|open call|deadline extended|new)\s*[:\-–|]\s*",
    re.I,
)


def normalize_title(title: str) -> str:
    t = PREFIX.sub("", title.lower())
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t)          # drop "(Fully Funded)", "[$10,000]"
    # drop SEO subtitles ("X | Apply to build...", "X: $40,000 per year for...") when the head is meaningful
    head = re.split(r"\s\|\s|:\s", t, maxsplit=1)[0]
    if len(head.strip()) >= 12:
        t = head
    t = NOISE.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


DISCRIMINATING = {
    "master", "masters", "doctoral", "phd", "doctorate", "bachelor", "bachelors", "undergraduate",
    "postgraduate", "postdoctoral", "postdoc", "junior", "senior", "school", "high", "graduate",
    "summer", "winter", "spring", "autumn", "fall", "women", "girls",
}


def _differs_meaningfully(a: str, b: str) -> bool:
    """'X Master's' vs 'X Doctoral', or 'Cohort 2' vs 'Cohort 3', are different programmes."""
    diff = set(a.split()) ^ set(b.split())
    return any(tok in DISCRIMINATING or tok.isdigit() for tok in diff)


def find_title_duplicate(norm_title: str, candidates, threshold: int) -> int | None:
    """candidates: rows with id and norm_title. Returns the matching id or None."""
    if len(norm_title) < 8:
        return None
    best_id, best = None, 0
    for row in candidates:
        other = row["norm_title"]
        if not other or _differs_meaningfully(norm_title, other):
            continue
        score = fuzz.token_set_ratio(norm_title, other)
        # token_set_ratio loves short strings contained in long ones; guard with a plain ratio too
        if score >= threshold and fuzz.ratio(norm_title, other) >= threshold - 25 and score > best:
            best_id, best = row["id"], score
    return best_id


def normalize_text(text: str, limit: int = 800) -> str:
    """Lower-case letters and digits only, for fuzzy 'is this programme mentioned in that post'."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower()))[:limit].strip()


def competitor_match(rows, official_canonical: str | None, norm_title: str):
    """Has another channel already posted this? Same official link, or the programme's name
    appears in their post (names usually stay in English in Uzbek and Russian posts)."""
    for r in rows:
        urls = json.loads(r["urls"] or "[]")
        if official_canonical and any(canonical_url(u) == official_canonical for u in urls):
            return r
        if len(norm_title) >= 12 and len(norm_title.split()) >= 2 and \
                fuzz.partial_ratio(norm_title, r["norm_text"] or "") >= 92:
            return r
    return None
