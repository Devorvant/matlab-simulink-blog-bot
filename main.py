import os
import html
from typing import List, Dict, Optional

import requests


# --- ThingSpeak ---
THINGSPEAK_CHANNEL_ID = os.getenv("THINGSPEAK_CHANNEL_ID", "247718")
THINGSPEAK_READ_KEY = os.getenv("THINGSPEAK_READ_KEY")  # пусто если Public
THINGSPEAK_RESULTS = int(os.getenv("THINGSPEAK_RESULTS", "20"))

# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID")  # "@channel" или "-100..."

TELEGRAM_DISABLE_PREVIEW = os.getenv("TELEGRAM_DISABLE_PREVIEW", "0") != "0"

# --- Поведение отправки ---
# "list"  -> одним сообщением списком (может разбить на несколько, если длинно)
# "single"-> по одному сообщению на каждую запись
SEND_MODE = os.getenv("SEND_MODE", "single").strip().lower()

STATE_FILE = os.getenv("STATE_FILE", "/tmp/last_sent_entry_id.txt")


def fetch_thingspeak_feeds(channel_id: str, results: int = 20, read_key: Optional[str] = None) -> Dict:
    url = f"https://api.thingspeak.com/channels/{channel_id}/feeds.json"
    params = {"results": results}
    if read_key:
        params["api_key"] = read_key

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def save_last_sent_entry_id(path: str, entry_id: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(entry_id))


def normalize_entries(data: Dict) -> List[Dict]:
    feeds = data.get("feeds") or []
    out: List[Dict] = []

    for f in feeds:
        entry_id = f.get("entry_id")
        title = (f.get("field1") or "").strip()
        link = (f.get("field2") or "").strip()
        text = (f.get("field3") or "").strip()
        created_at = f.get("created_at")

        if not entry_id or not title or not link:
            continue

        out.append(
            {
                "entry_id": int(entry_id),
                "title": title,
                "text": text,  # field3
                "link": link,
                "created_at": created_at,
            }
        )

    # старые -> новые
    out.sort(key=lambda x: x["entry_id"])

    # дедуп по ссылке (на всякий)
    seen = set()
    uniq = []
    for x in out:
        if x["link"] in seen:
            continue
        seen.add(x["link"])
        uniq.append(x)

    return uniq


def telegram_send(token: str, chat_id: str, text_html: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": TELEGRAM_DISABLE_PREVIEW,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def chunk_list_message(lines: List[str], header: str = "") -> List[str]:
    MAX_LEN = 3900
    msgs = []

    cur = header.strip()
    if cur:
        cur += "\n\n"

    for line in lines:
        add = line + "\n"
        if len(cur) + len(add) > MAX_LEN:
            msgs.append(cur.rstrip())
            cur = ""
        cur += add

    if cur.strip():
        msgs.append(cur.rstrip())

    return msgs


def build_single_message(title: str, text: str, link: str) -> str:
    """
    Формат:
    <b>Title</b>
    Text (обычный)
    link
    """
    title_h = html.escape(title)
    link_h = html.escape(link)
    text_h = html.escape(text).strip()

    if text_h:
        return f"<b>{title_h}</b>\n{text_h}\n{link_h}"
    return f"<b>{title_h}</b>\n{link_h}"


def main():
    if not BOT_TOKEN or not CHANNEL_CHAT_ID:
        raise SystemExit("Set BOT_TOKEN and CHANNEL_CHAT_ID env vars")

    data = fetch_thingspeak_feeds(
        channel_id=THINGSPEAK_CHANNEL_ID,
        results=THINGSPEAK_RESULTS,
        read_key=THINGSPEAK_READ_KEY,
    )

    entries = normalize_entries(data)
    if not entries:
        print("No entries with field1/field2 found.")
        return

    # ВАЖНО: отправляем ВСЕ записи каждый запуск
    if SEND_MODE == "single":
        for e in entries:
            msg = build_single_message(e["title"], e.get("text", ""), e["link"])
            telegram_send(BOT_TOKEN, CHANNEL_CHAT_ID, msg)

        print(f"Sent {len(entries)} entries as single messages.")
    else:
        # list-режим (заголовок кликабельный, snippet добавим после тире)
        lines = []
        for e in entries:
            title = html.escape(e["title"])
            link = html.escape(e["link"])
            text = html.escape((e.get("text") or "").strip())
            if text:
                lines.append(f"• <a href=\"{link}\">{title}</a>\n{text}")
            else:
                lines.append(f"• <a href=\"{link}\">{title}</a>")

        header = f"ThingSpeak {html.escape(THINGSPEAK_CHANNEL_ID)}: {len(entries)} items"
        messages = chunk_list_message(lines, header=header)
        for m in messages:
            telegram_send(BOT_TOKEN, CHANNEL_CHAT_ID, m)

        print(f"Sent {len(entries)} entries as list ({len(messages)} msg).")

    # STATE_FILE не блокирует отправку — просто пишем “для истории”
    max_sent = max(e["entry_id"] for e in entries)
    save_last_sent_entry_id(STATE_FILE, max_sent)
    print(f"Updated last_sent_entry_id={max_sent} (state does NOT block sending)")


if __name__ == "__main__":
    main()
