"""Settings you change on the dashboard (Boshqaruv tab), stored in the database.

They win over Railway Variables / .env, so day-to-day changes never need Railway again. Each saved
value is applied to the running settings at once, and the schedules are re-planned.
Also: pausing single agents and their daily AI budgets.
"""
from __future__ import annotations

import logging
import re

from .config import settings

log = logging.getLogger(__name__)

# key: (type, group, label, help)
EDITABLE = {
    "run_at": ("time", "finder", "Kunlik qidiruv vaqti", "Ergash har kuni shu vaqtda qidiradi"),
    "check_ages": ("bool", "finder", "Yoshni tekshirish", "O'chirilsa yosh qoidasi ishlamaydi"),
    "age_min": ("int", "finder", "Eng kichik yosh", "Dastur shu yoshni ham qabul qilishi kerak"),
    "age_max": ("int", "finder", "Eng katta boshlang'ich yosh", "Masalan 20: 18–35 o'tadi, 21–30 o'tmaydi"),
    "min_days_left": ("int", "finder", "Kamida necha kun qolgan bo'lsin", "Muddati yaqinlar tashlanadi"),
    "min_show_fit": ("int", "finder", "Navbatga chiqish uchun AI bahosi (1–5)", "Pastroqlari Zaxiraga tushadi"),
    "min_fit_score": ("int", "finder", "Tekshirishga arziydigan baho (1–5)", "Undan pasti sarlavhadan tashlanadi"),
    "research_min_per_run": ("int", "finder", "Har qidiruvda kamida tekshiriladi", "Sokin kunlarda ham nomzod bo'lsin"),
    "auto_write_per_day": ("int", "writer", "Kuniga avto-yoziladigan postlar", "Toshmat aka eng kuchlilarini Mirzoga beradi; 0 = o'chiq"),
    "auto_write_min_fit": ("int", "writer", "Avto-yozish uchun AI bahosi", "Odatda 5"),
    "weekly_at": ("text", "writer", "Haftalik ro'yxat vaqti", "Masalan: mon 08:30"),
    "community_refresh_at": ("time", "community", "GrantBek bilimini yangilash vaqti", "Kanalni qayta o'qiydi, FAQ tuzadi"),
    "report_at": ("time", "manager", "Kunlik hisobot vaqti", "Toshmat aka guruhga yozadi"),
    "budget_finder": ("money", "finder", "Ergash: kunlik AI byudjeti, $", "0 = cheklovsiz"),
    "budget_writer": ("money", "writer", "Mirzo: kunlik AI byudjeti, $", "0 = cheklovsiz"),
    "budget_community": ("money", "community", "GrantBek: kunlik AI byudjeti, $", "0 = cheklovsiz"),
    "budget_manager": ("money", "manager", "Toshmat aka: kunlik AI byudjeti, $", "0 = cheklovsiz"),
}

# which Claude calls belong to which agent (llm_usage.purpose)
PURPOSES = {
    "finder": ("triage", "find_official", "extract", "check"),
    "writer": ("write",),
    "community": ("community", "community_brief"),
    "manager": ("report", "order"),
}


def _convert(kind: str, value):
    if kind == "bool":
        return value in (True, "true", "1", 1, "on", "yes")
    if kind == "int":
        return int(value)
    if kind == "money":
        return round(float(value), 2)
    value = str(value or "").strip()
    if kind == "time" and value and not re.fullmatch(r"\d{1,2}:\d{2}", value):
        raise ValueError("vaqt HH:MM ko'rinishida bo'lsin, masalan 09:00")
    if kind == "text" and value and not re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun) \d{1,2}:\d{2}", value):
        raise ValueError("masalan: mon 08:30")
    return value or None


def current() -> dict:
    return {k: getattr(settings, k, None) for k in EDITABLE}


def apply(db) -> None:
    """Puts the saved values into the running settings (call at startup and after each save)."""
    for k, v in (db.get_meta("settings") or {}).items():
        if k in EDITABLE:
            try:
                setattr(settings, k, _convert(EDITABLE[k][0], v))
            except (TypeError, ValueError):
                log.warning("ignoring bad saved setting %s=%r", k, v)


def save(db, changes: dict) -> dict:
    """Validates, stores and applies. Returns the new values; raises ValueError with an Uzbek message."""
    saved = dict(db.get_meta("settings") or {})
    for k, v in changes.items():
        if k not in EDITABLE:
            continue
        try:
            value = _convert(EDITABLE[k][0], v)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{EDITABLE[k][2]}: {e}") from None
        saved[k] = value
    db.set_meta("settings", saved)
    apply(db)
    return current()


# --------------------------------------------------------------------------- pausing single agents
def paused_agents(db) -> set[str]:
    paused = set(db.get_meta("paused_agents") or [])
    if db.get_meta("paused"):            # the older "pause the daily search" order
        paused.add("finder")
    return paused


def set_paused(db, agent: str, on: bool) -> None:
    paused = set(db.get_meta("paused_agents") or [])
    (paused.add if on else paused.discard)(agent)
    db.set_meta("paused_agents", sorted(paused))
    if agent == "finder" and not on:
        db.set_meta("paused", False)


def is_paused(db, agent: str) -> bool:
    return agent in paused_agents(db)


# --------------------------------------------------------------------------- daily budgets
def spent_today(db, agent: str) -> float:
    from .dashboard import _tz_offset
    tz = _tz_offset()
    purposes = PURPOSES.get(agent, ())
    if not purposes:
        return 0.0
    q = (f"SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE purpose IN ({','.join('?' * len(purposes))})"
         " AND date(at, ?) = date('now', ?)")
    return float(db.conn.execute(q, (*purposes, tz, tz)).fetchone()[0])


def over_budget(db, agent: str) -> bool:
    budget = float(getattr(settings, f"budget_{agent}", 0) or 0)
    return budget > 0 and spent_today(db, agent) >= budget


def may_work(db, agent: str) -> str | None:
    """None if the agent may work now, else why not (in Uzbek)."""
    if is_paused(db, agent):
        return "to'xtatilgan"
    if over_budget(db, agent):
        return "bugungi byudjet tugadi"
    return None
