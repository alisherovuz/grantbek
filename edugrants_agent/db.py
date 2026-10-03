"""SQLite storage. One file, no server, easy to back up."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    summary TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    -- new -> duplicate | rejected | drafted -> in_review -> published | declined
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT,
    dup_of INTEGER,
    official_url TEXT,
    official_canonical TEXT,
    data_json TEXT,
    post_text TEXT,
    platform_json TEXT,
    review_message_id INTEGER,
    channel_message_id INTEGER,
    platform_ref TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_official ON items(official_canonical);

CREATE TABLE IF NOT EXISTS page_watch (
    url TEXT PRIMARY KEY,
    last_hash TEXT,
    last_checked TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_ok TEXT,
    last_error TEXT,
    last_error_at TEXT,
    items_last_run INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    norm_title TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    category TEXT,
    country TEXT,
    age TEXT,
    first_posted TEXT,
    last_posted TEXT,
    times_posted INTEGER DEFAULT 1,
    post_dates TEXT,
    official_url TEXT,
    edugrants_url TEXT,
    reactions_max INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS competitor_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    post_key TEXT NOT NULL UNIQUE,
    posted_at TEXT NOT NULL,
    title TEXT,
    norm_text TEXT,
    urls TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,            -- info | task | done | error
    text TEXT NOT NULL,
    task_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,            -- search | weekly_list | daily_report | write_post | order
    agent TEXT NOT NULL,           -- who does it: manager | finder | writer
    status TEXT NOT NULL,          -- new | working | waiting | done | failed | cancelled
    payload TEXT,
    result TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_posts (
    msg_id INTEGER PRIMARY KEY,    -- the post's number in our own channel
    posted_at TEXT,
    title TEXT NOT NULL,
    flag TEXT,
    deadline TEXT,                 -- YYYY-MM-DD read from "Ro'yxatdan o'tishning so'nggi muddati"
    link TEXT
);

CREATE TABLE IF NOT EXISTS community_qa (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    place TEXT NOT NULL,           -- dm | comment
    question TEXT NOT NULL,
    answer TEXT,
    topic TEXT
);

CREATE TABLE IF NOT EXISTS comment_threads (
    chat_id INTEGER NOT NULL,      -- the channel's comments group
    msg_id INTEGER NOT NULL,       -- the channel post as it appears in that group
    post_text TEXT,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
"""


MIGRATIONS = [("items", "history_id", "INTEGER"), ("items", "fit_score", "INTEGER"), ("items", "fit_reason", "TEXT"),
              ("history", "excluded", "TEXT"), ("history", "score", "REAL"), ("channel_posts", "text", "TEXT"),
              ("community_qa", "escalated", "INTEGER DEFAULT 0"), ("community_qa", "handled", "INTEGER DEFAULT 0"),
              ("community_qa", "link", "TEXT"), ("community_qa", "correction", "TEXT"), ("community_qa", "who", "TEXT")]


def now_iso() -> str:
    # Same format as SQLite's datetime('now') so comparisons in SQL work
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:  # columns added after the first version
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- items -------------------------------------------------------
    def insert_item(self, *, source: str, url: str, canonical_url: str, title: str,
                    norm_title: str, summary: str | None, published_at: str | None) -> int | None:
        """Returns the new id, or None if this canonical URL was seen before."""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO items (source,url,canonical_url,title,norm_title,summary,published_at,discovered_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (source, url, canonical_url, title, norm_title, summary, published_at, now_iso(), now_iso()),
            )
            return cur.lastrowid if cur.rowcount else None

    def get(self, item_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def by_status(self, status: str, limit: int = 1000) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE status=? ORDER BY id LIMIT ?", (status, limit)
        ).fetchall()

    def update(self, item_id: int, **fields) -> None:
        if not fields:
            return
        for k in ("data_json", "platform_json"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))

    def recent_titles(self, days: int, exclude_id: int | None = None) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, norm_title, status FROM items WHERE discovered_at >= datetime('now', ?)"
            " AND status NOT IN ('duplicate','rejected') AND id != ?",
            (f"-{days} days", exclude_id or -1),
        ).fetchall()

    def find_by_official(self, official_canonical: str, exclude_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT id, status, data_json FROM items WHERE official_canonical=? AND id != ?"
            " AND status NOT IN ('duplicate','rejected') ORDER BY id DESC LIMIT 1",
            (official_canonical, exclude_id),
        ).fetchone()

    # ---- page watch --------------------------------------------------
    def page_hash(self, url: str) -> str | None:
        row = self.conn.execute("SELECT last_hash FROM page_watch WHERE url=?", (url,)).fetchone()
        return row["last_hash"] if row else None

    def page_hours_since_check(self, url: str) -> float | None:
        row = self.conn.execute(
            "SELECT (julianday('now') - julianday(last_checked)) * 24 AS h FROM page_watch WHERE url=?", (url,)
        ).fetchone()
        return row["h"] if row and row["h"] is not None else None

    def set_page_hash(self, url: str, h: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO page_watch(url,last_hash,last_checked) VALUES (?,?,?)"
                " ON CONFLICT(url) DO UPDATE SET last_hash=excluded.last_hash, last_checked=excluded.last_checked",
                (url, h, now_iso()),
            )

    # ---- health / usage ---------------------------------------------
    def source_ok(self, source: str, n: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO source_health(source,last_ok,items_last_run) VALUES (?,?,?)"
                " ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok, items_last_run=excluded.items_last_run",
                (source, now_iso(), n),
            )

    def source_error(self, source: str, err: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO source_health(source,last_error,last_error_at) VALUES (?,?,?)"
                " ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error, last_error_at=excluded.last_error_at",
                (source, err[:500], now_iso()),
            )

    def health(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()

    def reset_finds(self, costs: bool = False) -> int:
        """Forget every find (and what each source last read), so the next search starts fresh.
        Kept: the channel's history, including programmes taken with ✅ Olamiz, so they never come back."""
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            for table in ("items", "page_watch", "competitor_posts", "source_health"):
                c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM meta WHERE key IN ('last_search', 'search_started')")
            if costs:
                c.execute("DELETE FROM llm_usage")
        return n

    # ---- agents: event log and task board ---------------------------------
    def log_event(self, agent: str, kind: str, text: str, task_id: int | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events(at, agent, kind, text, task_id) VALUES (?,?,?,?,?)",
                      (now_iso(), agent, kind, text[:1000], task_id))

    def events(self, limit: int = 50, since: str | None = None) -> list[sqlite3.Row]:
        if since:
            return self.conn.execute("SELECT * FROM events WHERE at >= ? ORDER BY id DESC LIMIT ?", (since, limit)).fetchall()
        return self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def add_task(self, type_: str, agent: str, payload=None, created_by: str = "manager", status: str = "new") -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO tasks(type, agent, status, payload, created_by, created_at, updated_at)"
                            " VALUES (?,?,?,?,?,?,?)", (type_, agent, status, json.dumps(payload, ensure_ascii=False),
                                                        created_by, now_iso(), now_iso()))
            return cur.lastrowid

    def update_task(self, task_id: int, status: str, result=None) -> None:
        with self.tx() as c:
            c.execute("UPDATE tasks SET status=?, result=COALESCE(?, result), updated_at=? WHERE id=?",
                      (status, None if result is None else json.dumps(result, ensure_ascii=False), now_iso(), task_id))

    def task(self, task_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---- our own channel's posts (for the Monday deadline list) ------------
    def save_channel_post(self, msg_id: int, posted_at: str | None, title: str, flag: str | None,
                          deadline: str | None, link: str | None, text: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO channel_posts(msg_id, posted_at, title, flag, deadline, link, text) VALUES (?,?,?,?,?,?,?)"
                      " ON CONFLICT(msg_id) DO UPDATE SET title=excluded.title, flag=excluded.flag,"
                      " deadline=excluded.deadline, link=excluded.link, text=COALESCE(excluded.text, channel_posts.text)",
                      (msg_id, posted_at, title, flag, deadline, link, text))

    def open_channel_posts(self, today: str, limit: int = 200) -> list[sqlite3.Row]:
        """Our posts whose deadline hasn't passed (what GrantBek can tell people about)."""
        return self.conn.execute("SELECT * FROM channel_posts WHERE deadline >= ? ORDER BY deadline LIMIT ?",
                                 (today, limit)).fetchall()

    def add_qa(self, place: str, question: str, answer: str | None, topic: str | None, escalated: bool = False,
               link: str | None = None, who: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO community_qa(at, place, question, answer, topic, escalated, handled, link, who)"
                            " VALUES (?,?,?,?,?,?,0,?,?)", (now_iso(), place, question[:1000], (answer or "")[:1500],
                                                           topic, int(escalated), link, who))
            return cur.lastrowid

    def qa_update(self, qa_id: int, **fields) -> None:
        cols = [k for k in fields if k in ("handled", "correction")]
        if cols:
            with self.tx() as c:
                c.execute(f"UPDATE community_qa SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                          (*[fields[k] for k in cols], qa_id))

    def corrections(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT question, correction FROM community_qa WHERE correction IS NOT NULL "
                                 "AND correction != '' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def recent_qa(self, days: int = 7, limit: int = 150) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM community_qa WHERE at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
                                 (f"-{days} days", limit)).fetchall()

    def save_thread(self, chat_id: int, msg_id: int, post_text: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO comment_threads(chat_id, msg_id, post_text) VALUES (?,?,?)",
                      (chat_id, msg_id, post_text))

    def thread_text(self, chat_id: int, msg_id: int) -> str | None:
        r = self.conn.execute("SELECT post_text FROM comment_threads WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
        return r[0] if r else None

    def channel_posts_between(self, first: str, last: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM channel_posts WHERE deadline BETWEEN ? AND ? ORDER BY deadline, msg_id DESC",
                                 (first, last)).fetchall()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False)))

    def log_usage(self, purpose: str, model: str, tin: int, tout: int, cost: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_usage(at,purpose,model,input_tokens,output_tokens,cost_usd) VALUES (?,?,?,?,?,?)",
                (now_iso(), purpose, model, tin, tout, cost),
            )

    def stats(self, days: int = 30) -> dict:
        since = f"-{days} days"
        counts = {r["status"]: r["n"] for r in self.conn.execute(
            "SELECT status, COUNT(*) n FROM items WHERE discovered_at >= datetime('now', ?) GROUP BY status", (since,))}
        cost = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o"
            " FROM llm_usage WHERE at >= datetime('now', ?)", (since,)).fetchone()
        return {"counts": counts, "cost_usd": round(cost["c"], 2), "tokens_in": cost["i"], "tokens_out": cost["o"]}

    # ---- channel history ---------------------------------------------
    def replace_history(self, programmes: list[dict]) -> None:
        cols = ["norm_title", "title", "category", "country", "age", "first_posted", "last_posted",
                "times_posted", "post_dates", "official_url", "edugrants_url", "reactions_max", "score"]
        with self.tx() as c:
            for p in programmes:
                c.execute(
                    f"INSERT INTO history ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                    " ON CONFLICT(norm_title) DO UPDATE SET "
                    + ", ".join(f"{k}=excluded.{k}" for k in cols if k not in ("norm_title", "official_url"))
                    + ", official_url=COALESCE(excluded.official_url, history.official_url)",
                    [p.get(k) for k in cols],
                )

    def history_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM history").fetchall()

    def history_get(self, hid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM history WHERE id=?", (hid,)).fetchone()

    def history_exclude(self, hid: int, reason: str | None) -> None:
        """Stop watching a programme that breaks a permanent rule (tuition, fee, ages, eligibility)."""
        with self.tx() as c:
            c.execute("UPDATE history SET excluded=? WHERE id=?", (reason, hid))

    def history_set_official(self, hid: int, url: str | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE history SET official_url=? WHERE id=?", (url or "", hid))

    def history_mark_posted(self, norm_title: str, title: str, day: str, official_url: str | None) -> None:
        row = self.conn.execute("SELECT * FROM history WHERE norm_title=?", (norm_title,)).fetchone()
        with self.tx() as c:
            if row:
                dates = json.loads(row["post_dates"] or "[]") + [day]
                c.execute("UPDATE history SET last_posted=?, times_posted=times_posted+1, post_dates=?,"
                          " official_url=COALESCE(?, official_url) WHERE id=?",
                          (day, json.dumps(dates), official_url, row["id"]))
            else:
                c.execute("INSERT INTO history (norm_title,title,first_posted,last_posted,times_posted,post_dates,"
                          "official_url,reactions_max) VALUES (?,?,?,?,1,?,?,0)",
                          (norm_title, title, day, day, json.dumps([day]), official_url))

    def feedback_examples(self, limit: int = 20) -> dict[str, list[str]]:
        out = {}
        groups = {"accepted": ("accepted", "drafted", "in_review", "published", "declined"), "skipped": ("skipped",)}
        for status, statuses in groups.items():
            rows = self.conn.execute(
                f"SELECT title, reason FROM items WHERE status IN ({','.join('?' * len(statuses))})"
                " ORDER BY updated_at DESC LIMIT ?", (*statuses, limit)).fetchall()
            out[status] = [r["title"] + (f" ({r['reason']})" if status == "skipped" and r["reason"] else "") for r in rows]
        return out

    # ---- other Uzbek channels -----------------------------------------
    def add_competitor_post(self, channel: str, post_key: str, posted_at: str, title: str,
                            norm_text: str, urls: list[str]) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO competitor_posts(channel,post_key,posted_at,title,norm_text,urls)"
                      " VALUES (?,?,?,?,?,?)", (channel, post_key, posted_at, title, norm_text, json.dumps(urls)))

    def recent_competitor_posts(self, days: int = 45) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM competitor_posts WHERE posted_at >= datetime('now', ?)",
                                 (f"-{days} days",)).fetchall()

    def competitor_channels(self) -> int:
        return self.conn.execute("SELECT COUNT(DISTINCT channel) FROM competitor_posts").fetchone()[0]
