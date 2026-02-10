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
        print(f"[ThingSpeak {THINGSPEAK_CHANNEL_ID}] No entries with field1/field2 found.")
        return

    print(f"[ThingSpeak {THINGSPEAK_CHANNEL_ID}] Fetched {len(entries)} entries. Sending...")

    # ВАЖНО: каждый запуск отправляет ВСЕ записи, сколько entry_id -> столько отправок
    sent = 0
    for e in entries:
        title = html.escape(e["title"])
        link = html.escape(e["link"])

        # если хочешь кликабельный заголовок без "голой" ссылки — раскомментируй 2 строки ниже
        # msg = f"🟧 <b><a href=\"{link}\">{title}</a></b>"
        # telegram_send(BOT_TOKEN, CHANNEL_CHAT_ID, msg)

        # максимально близко к твоему старому формату
        msg = f"<b>{title}</b>\n{link}"
        telegram_send(BOT_TOKEN, CHANNEL_CHAT_ID, msg)

        sent += 1
        print(f"[ThingSpeak {THINGSPEAK_CHANNEL_ID}] Sent entry_id={e['entry_id']}")

    print(f"[ThingSpeak {THINGSPEAK_CHANNEL_ID}] Done. Sent {sent}/{len(entries)} messages.")
