import os
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests
from dateutil import parser as dtparser

RSS_URL = os.getenv("RSS_URL", "https://blogs.mathworks.com/feedmlc")

TZ = os.getenv("TZ", "Europe/Rome")
TIMEZONE = ZoneInfo(TZ)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "@matlab_simulink_blog")

MAX_POSTS = int(os.getenv("MAX_POSTS", "200"))
DB_PATH = os.getenv("DB_PATH", "state.sqlite")


def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS posted (
            guid TEXT PRIMARY KEY,
            posted_at TEXT NOT NULL
        )
        """
    )
    con.commit()
    return con


def was_posted(con, guid: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT 1 FROM posted WHERE guid = ?", (guid,))
    return cur.fetchone() is not None


def mark_posted(con, guid: str):
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO posted(guid, posted_at) VALUES(?, ?)",
        (guid, datetime.now(tz=TIMEZONE).isoformat()),
    )
    con.commit()


def parse_entry_datetime(entry) -> datetime | None:
    published = getattr(entry, "published", None) or getattr(entry, "updated", None)
    if not published:
        return None
    try:
        dt = dtparser.parse(published)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return dt.astimezone(TIMEZONE)
    except Exception:
        return None


def send_to_telegram(text: str):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


def format_message(title: str, link: str) -> str:
    title = (title or "").strip()
    if title:
        return f"{title}\n{link}"
    return link


def main():
    con = db_init()

    now = datetime.now(tz=TIMEZONE)
    target_date = (now - timedelta(days=1)).date()

    feed = feedparser.parse(RSS_URL)

    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        print(f"Failed to parse feed: {getattr(feed, 'bozo_exception', None)}", file=sys.stderr)
        sys.exit(1)

    items = []
    for e in feed.entries:
        title = getattr(e, "title", "") or ""
        link = getattr(e, "link", "") or ""
        guid = getattr(e, "id", None) or link

        published_dt = parse_entry_datetime(e)
        if not published_dt:
            continue

        if published_dt.date() != target_date:
            continue

        if was_posted(con, guid):
            continue

        items.append((published_dt, guid, title, link))

    items.sort(key=lambda x: x[0])
    items = items[:MAX_POSTS]

    if not items:
        print(f"No new posts for {target_date.isoformat()} in TZ={TZ}.")
        return

    for published_dt, guid, title, link in items:
        msg = format_message(title, link)
        send_to_telegram(msg)
        mark_posted(con, guid)
        print(f"Posted: {published_dt.isoformat()} | {title}")

    print(f"Done. Posted {len(items)} item(s) for {target_date.isoformat()}.")


if __name__ == "__main__":
    main()