"""The pipeline: collect -> dedupe -> triage -> verify at source -> filter -> write.

Each stage moves items to a new status, so a crash or restart just resumes.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from .collectors import Candidate, collect_all
from .config import load_yaml, settings
from .db import DB
from .dedupe import canonical_url, domain, find_title_duplicate, normalize_title
from .fetch import fetch_page
from .llm import LLM
from .render import build_platform_payload, render_post

log = logging.getLogger(__name__)

# Sites that republish other people's opportunities. A link to one of these is never "official".
AGGREGATOR_DOMAINS = {
    "opportunitydesk.org", "opportunitiesforyouth.org", "opportunitiescorners.com", "fundsforngos.org",
    "www2.fundsforngos.org", "scholars4dev.com", "youthop.com", "opportunitiescircle.com",
    "scholarshipregion.com", "afterschoolafrica.com", "mladiinfo.eu", "scholarshipsads.com",
    "opportunitiesforafricans.com", "globalsouthopportunities.com", "grantgo.uz", "edugrants.uz",
    "grantlar.uz", "t.me", "oyaop.com", "oliygoh.uz", "profellow.com", "brightscholarship.com", "scholarships365.info", "opportunitiesforafricans.com",
}


def hard_rules_text() -> str:
    """The rules the AI must enforce, built from settings so .env is the single place to change them."""
    rules = ["open to applicants from Uzbekistan (open to all countries counts)",
             "no application fee (a fee just to apply)"]
    if settings.require_free_participation:
        rules.append("the participant pays nothing to take part (fully funded, or free)")
    if settings.check_ages:
        rules.append(f"ages overlap {settings.age_min}-{settings.age_max}")
    lines = ["HARD RULES (drop only if one of these is clearly broken):"] + [f"- {r}" for r in rules]
    lines.append("Also drop things that are not an opportunity for an individual young person: job vacancies, "
                 "grants only for organisations or companies, and programmes only for citizens of one other country.")
    if not settings.require_free_participation:
        lines.append("Cost of taking part, age range and study level are NOT reasons to drop. Use them only "
                     "for the fit score.")
    return "\n".join(lines)


# First word of reject reasons that describe the programme itself, not this year's round
PERMANENT_REJECTIONS = {"participant", "application", "ages", "Uzbekistan"}


def _domain(url: str | None) -> str:
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def post_link(data: dict, official_url: str | None, aggregators: set[str] = AGGREGATOR_DOMAINS) -> str | None:
    """The link the post will carry: the registration form, else the organiser's page. Never an aggregator."""
    for url in (data.get("registration_url"), official_url):
        if url and url.startswith("http") and _domain(url) not in aggregators:
            return url
    return None


def format_gaps(data: dict, official_url: str | None, aggregators: set[str] = AGGREGATOR_DOMAINS) -> list[str]:
    """What the @EduGrandsUz post layout needs but this find doesn't have. Every post has: title,
    Davlat, Moliyaviy ta'minot / Tanlov shakli / Dastur shakli, a description, Imtiyozlari,
    a 'Havola' to the organiser (never an aggregator) and a deadline. Empty list = it fits."""
    gaps = []
    if not (data.get("title") or "").strip():
        gaps.append("no title")
    if (data.get("host_country") or "").strip().lower() in ("", "unknown", "n/a", "none"):
        gaps.append("no country")
    if data.get("funding") not in ("full", "partial") and data.get("format") in (None, "", "unknown"):
        gaps.append("online/offline unknown")
    if len((data.get("summary") or "").strip()) < 20:
        gaps.append("no description")
    if not [b for b in data.get("benefits") or [] if len(str(b).strip()) > 3]:
        gaps.append("no benefits")
    if not post_link(data, official_url, aggregators):
        gaps.append("no official link")
    if data.get("deadline_type") != "rolling" and not data.get("deadline"):
        gaps.append("no deadline")
    return gaps


def participant_pays_nothing(data: dict) -> bool:
    """Fully funded, or free to take part where there is nothing else to pay for:
    online programmes, competitions entered remotely, and events inside Uzbekistan."""
    if data.get("funding") == "full":
        return True
    fee = data.get("program_fee")
    if fee == "paid_with_full_aid":
        return settings.allow_full_aid
    if fee != "free":
        return False
    if data.get("format") == "online":
        return True
    if data.get("opportunity_type") in ("competition", "olympiad") and data.get("format") != "offline":
        return True
    return (data.get("host_country_iso2") or "").upper() == "UZ"


class Pipeline:
    def __init__(self, db: DB, llm: LLM | None = None, config: dict | None = None, options: dict | None = None,
                 fetcher=fetch_page):
        self.db = db
        self.llm = llm or LLM(db)
        self.config = config if config is not None else load_yaml(settings.sources_file)
        self.options = options if options is not None else load_yaml(settings.options_file)
        self.fetch = fetcher
        self.aggregators = AGGREGATOR_DOMAINS | set(self.config.get("extra_aggregator_domains", []))

    # ------------------------------------------------------------------ 1
    def ingest(self, candidates: list[Candidate]) -> int:
        added = 0
        history = self.db.history_rows()
        recent_cut = (date.today() - timedelta(days=settings.recent_post_days)).isoformat()
        for c in candidates:
            norm = normalize_title(c.title)
            item_id = self.db.insert_item(
                source=c.source, url=c.url, canonical_url=c.key or canonical_url(c.url), title=c.title,
                norm_title=norm, summary=c.summary, published_at=c.published_at,
            )
            if not item_id:
                continue  # exact URL seen before
            added += 1
            if c.history_id:
                self.db.update(item_id, history_id=c.history_id)
                continue  # watched pages are deduped by official URL + deadline later

            # Has the channel posted this programme before?
            hid = find_title_duplicate(norm, history, settings.dedupe_threshold)
            if hid:
                h = next(r for r in history if r["id"] == hid)
                if h["last_posted"] >= recent_cut:
                    self.db.update(item_id, status="duplicate", history_id=hid,
                                   reason=f"kanalda {h['last_posted']} da joylangan")
                    continue
                self.db.update(item_id, history_id=hid)  # posted in an earlier year: likely a new round

            dup = find_title_duplicate(norm, self.db.recent_titles(settings.dedupe_days, exclude_id=item_id),
                                       settings.dedupe_threshold)
            if dup:
                self.db.update(item_id, status="duplicate", dup_of=dup, reason="same title as #%d" % dup)
        return added

    def collect(self, fast_only: bool = False) -> int:
        return self.ingest(collect_all(self.db, self.config, fast_only=fast_only))

    # ------------------------------------------------------------------ 2
    def triage(self, batch_size: int = 20) -> None:
        audience = self.config.get("audience", "") + "\n" + hard_rules_text()
        profile = settings.profile_file.read_text(encoding="utf-8") if settings.profile_file.exists() else ""
        feedback = self.db.feedback_examples()
        new = self.db.by_status("new", limit=settings.max_process_per_run * 3)
        # Programmes the channel already posted in earlier years are known to fit: no triage call needed
        known = [r for r in new if r["history_id"]]
        for r in known:
            h = self.db.history_get(r["history_id"])
            self.db.update(r["id"], status="triaged", fit_score=5,
                           fit_reason=f"oldin {h['times_posted']} marta joylangan, eng ko'p {h['reactions_max']} reaksiya")
        unknown = [r for r in new if not r["history_id"]]
        for i in range(0, len(unknown), batch_size):
            chunk = unknown[i:i + batch_size]
            payload = [{"id": r["id"], "title": r["title"], "summary": (r["summary"] or "")[:400]} for r in chunk]
            try:
                decisions = self.llm.triage(payload, audience, profile, feedback)
            except Exception as e:
                log.error("triage failed: %s", e)
                return
            for r in chunk:
                keep, fit, reason = decisions.get(r["id"], (True, 3, "no decision, kept"))
                if keep and fit < settings.min_fit_score:
                    keep, reason = False, f"low fit ({fit}): {reason}"
                self.db.update(r["id"], status="triaged" if keep else "rejected", fit_score=fit,
                               fit_reason=reason if keep else None, reason=None if keep else f"triage: {reason}")

    # ------------------------------------------------------------------ 3
    def research(self, item) -> None:
        """Find the organiser's page, read it, extract facts, apply filters."""
        item_id, url, title = item["id"], item["url"], item["title"]
        is_watch = "#watch-" in item["canonical_url"]
        is_tg = item["source"].startswith("telegram") or "t.me/" in item["canonical_url"]

        # Text of the listing itself
        agg_text, links = item["summary"] or "", []
        if not is_watch:
            try:
                page = self.fetch(url)
                agg_text, links = page.text, page.links
                if domain(page.final_url) not in self.aggregators:  # the link already IS the organiser
                    official_url, official_text = page.final_url, page.text
                    return self._extract_and_filter(item, title, official_text, official_url, agg_text, url)
            except Exception as e:
                if not is_tg:
                    self.db.update(item_id, status="error", reason=f"fetch listing: {e}"[:300])
                    return
        else:
            return self._extract_and_filter(item, title, None, None, agg_text, url, watch_url=url)

        # Ask which outbound link is the organiser's page, then read that page
        official_url = official_text = None
        try:
            found = self.llm.find_official(title, agg_text, links)
            cand = found.get("official_url") or found.get("registration_url")
            if cand and domain(cand) not in self.aggregators:
                official_page = self.fetch(cand)
                official_url, official_text = official_page.final_url, official_page.text
        except Exception as e:
            log.info("#%d official page not reachable: %s", item_id, e)
        self._extract_and_filter(item, title, official_text, official_url, agg_text, url)

    def _extract_and_filter(self, item, title, official_text, official_url, agg_text, agg_url, watch_url=None):
        item_id = item["id"]
        if watch_url:  # a watched page is itself the official source
            try:
                p = self.fetch(watch_url)
                official_text, official_url = p.text, p.final_url
            except Exception as e:
                self.db.update(item_id, status="error", reason=f"fetch watched page: {e}"[:300])
                return
        try:
            data = self.llm.extract(title, official_text, official_url, agg_text, agg_url)
        except Exception as e:
            self.db.update(item_id, status="error", reason=f"extract: {e}"[:300])
            return

        official_canon = canonical_url(official_url) if official_url else None
        fields = {"data_json": data, "official_url": official_url, "official_canonical": official_canon}

        reason = self.reject_reason(data)
        if not reason:
            gaps = format_gaps(data, official_url, self.aggregators)
            reason = f"post format: {', '.join(gaps)}" if gaps else None
        if reason:
            self.db.update(item_id, status="rejected", reason=reason, **fields)
            # A rule that won't change next year (tuition, fee, ages, eligibility): stop watching it
            if item["history_id"] and reason.split(" ")[0] in PERMANENT_REJECTIONS:
                self.db.history_exclude(item["history_id"], reason)
            return
        if official_canon:
            dup = self.db.find_by_official(official_canon, item_id)
            if dup and not self.is_new_intake(dup, data):
                self.db.update(item_id, status="duplicate", dup_of=dup["id"],
                               reason=f"same official page as #{dup['id']}", **fields)
                return
        else:
            dup = None
        # Re-check title dedupe with the clean official title (skipped for a genuine new intake)
        norm = normalize_title(data.get("title") or title)
        tdup = None if (dup or watch_url) else find_title_duplicate(
            norm, self.db.recent_titles(settings.dedupe_days, exclude_id=item_id), settings.dedupe_threshold)
        if tdup:
            self.db.update(item_id, status="duplicate", dup_of=tdup, reason=f"same programme as #{tdup}", **fields)
            return
        self.db.update(item_id, status="extracted", norm_title=norm, **fields)

    @staticmethod
    def is_new_intake(previous, data: dict) -> bool:
        """Same official page, but the earlier item's deadline has passed and this one has a
        new deadline: it's next year's round, not a duplicate."""
        try:
            prev = json.loads(previous["data_json"] or "{}")
        except ValueError:
            return False
        old_dl, new_dl = prev.get("deadline"), data.get("deadline")
        return bool(old_dl and new_dl and new_dl != old_dl and old_dl < date.today().isoformat())

    @staticmethod
    def reject_reason(data: dict) -> str | None:
        """Hard rules. In finder mode these are EduGrants' rules: the participant pays nothing,
        no application fee, open to Uzbek applicants, ages overlap AGE_MIN..AGE_MAX."""
        if not data.get("is_opportunity", True):
            return "not a specific opportunity"
        if data.get("status") == "closed":
            return "applications closed"
        uz = data.get("uzbekistan_eligible")
        if uz == "no" or (uz == "unclear" and settings.require_uz_yes and settings.mode == "finder"):
            return f"Uzbekistan eligibility {uz} ({data.get('eligible_countries')})"
        if data.get("deadline_type") != "rolling":
            dl = data.get("deadline")
            if not dl:
                return "no deadline found"
            try:
                days_left = (date.fromisoformat(dl[:10]) - date.today()).days
            except ValueError:
                return f"unreadable deadline {dl!r}"
            if days_left < settings.min_days_left:
                return f"deadline too close ({days_left} days left)"
        if data.get("confidence") == "low" and not data.get("verified_from_official"):
            return "low confidence and not verified on organiser page"
        if settings.mode == "finder":
            if data.get("application_fee") == "paid":
                return "application fee"
            lo, hi = data.get("age_min"), data.get("age_max")
            if settings.check_ages and ((lo is not None and lo > settings.age_max)
                                        or (hi is not None and hi < settings.age_min)):
                return f"ages {lo}-{hi} outside {settings.age_min}-{settings.age_max}"
            if settings.require_free_participation and not participant_pays_nothing(data):
                return (f"participant pays (funding={data.get('funding')}, "
                        f"program_fee={data.get('program_fee')}, format={data.get('format')})")
        return None

    # ------------------------------------------------------------------ 4
    def write(self, item) -> None:
        data = json.loads(item["data_json"])
        link = post_link(data, item["official_url"], self.aggregators) or item["official_url"] or item["url"]
        try:
            written = self.llm.write(data, self.options)
        except Exception as e:
            self.db.update(item["id"], status="error", reason=f"write: {e}"[:300])
            return
        post = render_post(data, written, link, settings.channel_handle)
        platform = build_platform_payload(data, written, item["official_url"], link)
        self.db.update(item["id"], status="drafted", post_text=post, platform_json=platform)

    # ------------------------------------------------------------------
    def process(self) -> dict:
        self.triage()
        # New external finds first, best fit and freshest first; old programmes after them
        triaged = sorted(self.db.by_status("triaged"), key=lambda r: r["published_at"] or "", reverse=True)
        triaged.sort(key=lambda r: (r["history_id"] is not None, -(r["fit_score"] or 0)))  # stable: keeps freshest first
        for item in triaged[: settings.max_process_per_run]:
            self.research(item)
        # Soonest deadline first, so urgent ones reach the editor first
        extracted = sorted(self.db.by_status("extracted"),
                           key=lambda r: json.loads(r["data_json"]).get("deadline") or "9999")
        if settings.mode == "full":  # in finder mode, posts are written only after an editor accepts
            for item in extracted[: settings.max_drafts_per_run]:
                self.write(item)
        return self.db.stats(days=1)["counts"]

    def run(self, fast_only: bool = False) -> dict:
        added = self.collect(fast_only=fast_only)
        counts = self.process()
        log.info("run finished: %d new candidates, today: %s", added, counts)
        return {"added": added, "today": counts}
