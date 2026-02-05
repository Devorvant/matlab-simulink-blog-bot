import os
import time
import sqlite3
import html
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


DEFAULT_RSS_FEED_URL = "https://it.mathworks.com/matlabcentral/profile/rss/v1/content/feed?source=blogs"
DB_PATH = "state.sqlite"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; matlab-simulink-blog-bot/1.1)",
    "Accept": "application/rss+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Connection": "close",
}


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


def env_any(*names: str) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    raise SystemExit(f"Missing env var: one of {', '.join(names)}")


def normalize_channel(value: str) -> str:
    v = value.strip()
    if v.lstrip("-").isdigit():  # numeric chat id
        return v
    if not v.startswith("@"):
        v = "@" + v
    return v


def get_tz():
    tz_name = os.getenv("TZ", "Europe/Rome").strip()
    if ZoneInfo is None:
        return None, tz_name
    try:
        return ZoneInfo(tz_name), tz_name
    except Exception:
        return None, tz_name


def target_yesterday_date():
    """
    Вчера по TZ (по умолчанию Europe/Rome). Можно переопределить:
    TARGET_DATE=YYYY-MM-DD
    """
    override = os.getenv("TARGET_DATE", "").strip()
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()

    tz, _ = get_tz()
    if tz is None:
        return (datetime.utcnow() - timedelta(days=1)).date()

    return (datetime.now(tz) - timedelta(days=1)).date()


def parse_bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "y", "on"}


def http_get(session: requests.Session, url: str, timeout_s: int = 30) -> requests.Response:
    # лёгкие ретраи для временных ошибок
    for attempt in range(1, 4):
        try:
            r = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout_s, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * attempt)
                continue
            return r
        except Exception:
            time.sleep(2 * attempt)
    return session.get(url, headers=DEFAULT_HEADERS, timeout=timeout_s, allow_redirects=True)


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS posted (url TEXT PRIMARY KEY, posted_at TEXT NOT NULL)")
    conn.commit()
    return conn


def already_posted(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.execute("SELECT 1 FROM posted WHERE url = ?", (url,))
    return cur.fetchone() is not None


def mark_posted(conn: sqlite3.Connection, url: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO posted(url, posted_at) VALUES(?, ?)",
        (url, datetime.utcnow().isoformat()),
    )
    conn.commit()


def parse_pubdate(pub: str) -> datetime | None:
    s = (pub or "").strip()
    if not s:
        return None

    # 1) ISO 8601, например 2026-02-04T09:24:25Z
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    # 2) RFC822 (на всякий случай)
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def extract_items_from_rss(xml_text: str) -> list[dict]:
    soup = BeautifulSoup(xml_text, "xml")
    items = []
    for it in soup.find_all("item"):
        title = it.title.get_text(strip=True) if it.title else ""
        link = it.link.get_text(strip=True) if it.link else ""
        pub = it.pubDate.get_text(strip=True) if it.pubDate else ""
        if title and link and pub:
            items.append({"title": title, "url": link, "pubDate": pub})
    # дедуп по url
    uniq = {p["url"]: p for p in items}
    return list(uniq.values())


def telegram_send_message(
    session: requests.Session,
    bot_token: str,
    chat_id: str,
    title: str,
    url: str,
    disable_preview: bool,
) -> None:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"<b>{html.escape(title)}</b>\n{html.escape(url)}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    r = session.post(api_url, data=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage failed: status={r.status_code} body={r.text}")


def main() -> None:
    bot_token = env_required("BOT_TOKEN")
    channel = normalize_channel(env_any("CHANNEL_CHAT_ID", "CHANNEL_USERNAME"))

    rss_url = os.getenv("RSS_FEED_URL", DEFAULT_RSS_FEED_URL).strip() or DEFAULT_RSS_FEED_URL
    disable_preview = parse_bool_env("DISABLE_WEB_PAGE_PREVIEW", default=False)

    yday = target_yesterday_date()
    tz, tz_name = get_tz()
    print(f"[INFO] TZ={tz_name} target_date(yesterday)={yday.isoformat()}")
    print(f"[INFO] RSS_FEED_URL={rss_url}")

    conn = init_db()

    with requests.Session() as session:
        r = http_get(session, rss_url)
        if r.status_code != 200:
            raise RuntimeError(f"RSS fetch failed: status={r.status_code} url={rss_url} body={r.text[:300]}")

        items = extract_items_from_rss(r.text)
        print(f"[INFO] RSS items total: {len(items)}")

        # фильтруем по дате "вчера" в нужной TZ
        posts = []
        for it in items:
            dt = parse_pubdate(it.get("pubDate", ""))
            if not dt:
                continue
            dt_local = dt.astimezone(tz) if tz is not None else dt.astimezone(timezone.utc)
            if dt_local.date() == yday:
                it["pub_dt"] = dt_local
                posts.append(it)

        posts.sort(key=lambda x: (x["pub_dt"], x["url"]))
        print(f"[INFO] Posts for {yday.isoformat()}: {len(posts)}")

        sent, skipped = 0, 0
        for p in posts:
            if already_posted(conn, p["url"]):
                skipped += 1
                continue

            telegram_send_message(session, bot_token, channel, p["title"], p["url"], disable_preview)
            mark_posted(conn, p["url"])
            sent += 1
            time.sleep(1.0)

        print(f"[DONE] sent={sent} skipped_already_posted={skipped}")


if __name__ == "__main__":
    main()
