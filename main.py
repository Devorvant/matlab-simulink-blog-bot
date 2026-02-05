import os
import time
import sqlite3
import html
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE = "https://blogs.mathworks.com/"
BLOGGER_LIST_URL = urljoin(BASE, "blogger/")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Connection": "close",
}

DB_PATH = "state.sqlite"


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


def normalize_channel(value: str) -> str:
    v = value.strip()
    if not v:
        return v
    # allow numeric chat_id
    if v.lstrip("-").isdigit():
        return v
    # public username
    if not v.startswith("@"):
        v = "@" + v
    return v


def get_target_date_utc() -> datetime:
    """
    Берём ВЧЕРА в UTC (Railway cron показывает UTC).
    Для теста можно задать TARGET_DATE=YYYY-MM-DD
    """
    override = os.getenv("TARGET_DATE", "").strip()
    if override:
        dt = datetime.strptime(override, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def http_get(session: requests.Session, url: str, timeout_s: int = 30) -> requests.Response:
    last_err = None
    for attempt in range(1, 4):
        try:
            r = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout_s, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * attempt)
                continue
            return r
        except Exception as e:
            last_err = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"GET failed: {url} | last error: {last_err}")


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS posted (url TEXT PRIMARY KEY, posted_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def already_posted(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.execute("SELECT 1 FROM posted WHERE url = ?", (url,))
    return cur.fetchone() is not None


def mark_posted(conn: sqlite3.Connection, url: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO posted(url, posted_at) VALUES(?, ?)",
        (url, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def extract_blog_base_urls(session: requests.Session) -> list[str]:
    """
    Список блогов со страницы:
    https://blogs.mathworks.com/blogger/
    Берём ссылки вида https://blogs.mathworks.com/<slug>/
    """
    r = http_get(session, BLOGGER_LIST_URL)
    if r.status_code != 200:
        raise RuntimeError(f"Cannot open blogger list: {BLOGGER_LIST_URL} status={r.status_code}")

    soup = BeautifulSoup(r.text, "html.parser")
    bases = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if href.startswith("/"):
            href = urljoin(BASE, href)

        if not href.startswith(BASE):
            continue

        parsed = urlparse(href)
        path = (parsed.path or "").strip("/")
        if not path:
            continue

        segs = path.split("/")
        if len(segs) != 1:
            continue

        slug = segs[0].strip()
        if not slug:
            continue

        if slug in {"blogger", "blogs", "search"}:
            continue
        if slug.isdigit():
            continue

        bases.add(urljoin(BASE, slug + "/"))

    return sorted(bases)


def extract_posts_from_daily_page(html_text: str) -> list[dict]:
    """
    На дневной странице /YYYY/MM/DD/ вытаскиваем заголовок и ссылку.
    Обычно селектор: .entry-title a
    """
    soup = BeautifulSoup(html_text, "html.parser")
    posts = []

    for a in soup.select(".entry-title a[href]"):
        title = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if not title or not href:
            continue
        posts.append({"title": title, "url": href})

    uniq = {}
    for p in posts:
        uniq[p["url"]] = p
    return list(uniq.values())


def telegram_send_message(session: requests.Session, bot_token: str, chat_id: str, title: str, url: str) -> None:
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = f"<b>{html.escape(title)}</b>\n{html.escape(url)}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = session.post(api_url, data=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage failed: status={r.status_code} body={r.text}")


def main() -> None:
    bot_token = env_required("BOT_TOKEN")
    channel = normalize_channel(env_required("CHANNEL_CHAT_ID"))

    target_date = get_target_date_utc()
    yyyy = target_date.strftime("%Y")
    mm = target_date.strftime("%m")
    dd = target_date.strftime("%d")

    print(f"[INFO] Target date (UTC): {target_date.date()}")

    conn = init_db()

    with requests.Session() as session:
        blog_bases = extract_blog_base_urls(session)
        print(f"[INFO] Found blog bases: {len(blog_bases)}")

        all_posts = []

        for base_url in blog_bases:
            daily_url = urljoin(base_url, f"{yyyy}/{mm}/{dd}/")
            try:
                r = http_get(session, daily_url)
            except Exception as e:
                print(f"[WARN] {daily_url} fetch error: {e}")
                continue

            if r.status_code == 404:
                continue
            if r.status_code != 200:
                print(f"[WARN] {daily_url} status={r.status_code}")
                continue

            posts = extract_posts_from_daily_page(r.text)
            if posts:
                print(f"[INFO] {daily_url} -> {len(posts)} post(s)")
                all_posts.extend(posts)

        uniq = {}
        for p in all_posts:
            uniq[p["url"]] = p
        posts_to_send = list(uniq.values())
        posts_to_send.sort(key=lambda x: x["url"])

        print(f"[INFO] Total unique posts for {target_date.date()}: {len(posts_to_send)}")

        sent = 0
        skipped = 0
        for p in posts_to_send:
            if already_posted(conn, p["url"]):
                skipped += 1
                continue

            telegram_send_message(session, bot_token, channel, p["title"], p["url"])
            mark_posted(conn, p["url"])
            sent += 1
            time.sleep(1.0)

        print(f"[DONE] sent={sent} skipped_already_posted={skipped}")


if __name__ == "__main__":
    main()
