import os
import time
import sqlite3
import html
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


BASE = "https://blogs.mathworks.com/"
BLOGGER_LIST_URL = urljoin(BASE, "blogger/")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; matlab-simulink-blog-bot/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Connection": "close",
}

DB_PATH = "state.sqlite"


def env_any(*names: str) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    raise SystemExit(f"Missing env var: one of {', '.join(names)}")


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing env var: {name}")
    return v


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
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def target_yesterday_date():
    override = os.getenv("TARGET_DATE", "").strip()
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()

    tz = get_tz()
    if tz is None:
        return (datetime.utcnow() - timedelta(days=1)).date()
    return (datetime.now(tz) - timedelta(days=1)).date()


def http_get(session: requests.Session, url: str, timeout_s: int = 30) -> requests.Response:
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


def extract_slugs_from_html(html_text: str) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    slugs = set()

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
        if slug in {"blogger", "blogs", "search", "tag", "category", "wp-content", "wp-json", "feedmlc"}:
            continue
        if slug.isdigit():
            continue

        slugs.add(slug)

    return sorted(slugs)


def extract_blog_base_urls_from_blogger(session: requests.Session) -> list[str] | None:
    r = http_get(session, BLOGGER_LIST_URL)
    if r.status_code != 200:
        print(f"[WARN] /blogger/ blocked/unavailable: status={r.status_code}")
        return None

    slugs = extract_slugs_from_html(r.text)
    return [urljoin(BASE, s + "/") for s in slugs]


def extract_blog_base_urls_from_home(session: requests.Session) -> list[str] | None:
    r = http_get(session, BASE)
    if r.status_code != 200:
        print(f"[WARN] home page blocked/unavailable: status={r.status_code}")
        return None

    slugs = extract_slugs_from_html(r.text)
    if not slugs:
        return None
    return [urljoin(BASE, s + "/") for s in slugs]


def blog_bases_from_slugs_file() -> list[str]:
    path = os.getenv("BLOG_SLUGS_FILE", "").strip()
    if not path:
        return []
    if not os.path.exists(path):
        print(f"[WARN] BLOG_SLUGS_FILE not found: {path}")
        return []
    slugs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip().strip("/")
            if s and not s.startswith("#"):
                slugs.append(s)
    slugs = sorted(set(slugs))
    return [urljoin(BASE, s + "/") for s in slugs]


def blog_bases_fallback_from_env() -> list[str]:
    slugs = os.getenv("BLOG_SLUGS", "").strip()
    if not slugs:
        return []
    items = [s.strip().strip("/") for s in slugs.split(",") if s.strip()]
    items = sorted(set(items))
    return [urljoin(BASE, s + "/") for s in items]


def extract_posts_from_daily_page(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    posts = []
    for a in soup.select(".entry-title a[href]"):
        title = a.get_text(strip=True)
        href = a.get("href", "").strip()
        if title and href:
            posts.append({"title": title, "url": href})
    uniq = {p["url"]: p for p in posts}
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
    channel = normalize_channel(env_any("CHANNEL_CHAT_ID", "CHANNEL_USERNAME"))

    yday = target_yesterday_date()
    yyyy, mm, dd = yday.strftime("%Y"), yday.strftime("%m"), yday.strftime("%d")
    print(f"[INFO] Target date: {yday.isoformat()}")

    conn = init_db()

    with requests.Session() as session:
        blog_bases = extract_blog_base_urls_from_blogger(session)

        if not blog_bases:
            blog_bases = extract_blog_base_urls_from_home(session)

        if not blog_bases:
            blog_bases = blog_bases_from_slugs_file()

        if not blog_bases:
            blog_bases = blog_bases_fallback_from_env()

        if not blog_bases:
            # минимальный дефолт, чтобы хоть что-то работало
            blog_bases = [urljoin(BASE, s + "/") for s in ["matlab", "simulink", "cleve"]]
            print("[WARN] Using minimal default slugs: matlab, simulink, cleve")

        print(f"[INFO] Blog bases to scan: {len(blog_bases)}")

        all_posts = []
        for base_url in blog_bases:
            daily_url = urljoin(base_url, f"{yyyy}/{mm}/{dd}/")
            r = http_get(session, daily_url)

            if r.status_code == 404:
                continue
            if r.status_code != 200:
                print(f"[WARN] {daily_url} status={r.status_code}")
                continue

            posts = extract_posts_from_daily_page(r.text)
            if posts:
                print(f"[INFO] {daily_url} -> {len(posts)} post(s)")
                all_posts.extend(posts)

        uniq = {p["url"]: p for p in all_posts}
        posts_to_send = list(uniq.values())
        posts_to_send.sort(key=lambda x: x["url"])

        print(f"[INFO] Total unique posts for {yday.isoformat()}: {len(posts_to_send)}")

        sent, skipped = 0, 0
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
