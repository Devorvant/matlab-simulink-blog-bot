
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

    last_created_at, last_entry_id = load_state(STATE_FILE)
    last_dt = parse_ts(last_created_at)

    # Новые = строго позже по времени, либо то же время, но entry_id больше
    new_entries = [
        e for e in entries
        if (e["created_dt"] > last_dt) or (e["created_dt"] == last_dt and e["entry_id"] > last_entry_id)
    ]

    if not new_entries:
        print(f"No new entries. last_created_at={last_created_at} last_entry_id={last_entry_id}")
        return

    total = len(new_entries)
    sent = 0

    try:
        if SEND_MODE == "list":
            # список с кликабельными заголовками (без голых ссылок)
            lines = []
            for e in new_entries:
                title = html.escape(e["title"])
                link = html.escape(e["link"])
                lines.append(f"• <a href=\"{link}\">{title}</a>")

            header = f"ThingSpeak {html.escape(THINGSPEAK_CHANNEL_ID)}: {total} new"
            messages = chunk_list_message(lines, header=header)

            for m in messages:
                telegram_send(m)

            # state обновляем только когда все сообщения списка отправились
            last_e = new_entries[-1]
            save_state(
                STATE_FILE,
                last_created_at=last_e["created_at"],
                last_entry_id=last_e["entry_id"],
            )
            sent = total
            print(f"Sent {sent} entries as list ({len(messages)} msg).")
            print(f"Updated state: {last_e['created_at']} / {last_e['entry_id']}")

        else:
            # single: state обновляем ПОСЛЕ КАЖДОЙ успешной отправки
            for e in new_entries:
                title = html.escape(e["title"])
                link = html.escape(e["link"])
                msg = f"🟧 <b><a href=\"{link}\">{title}</a></b>"

                telegram_send(msg)

                # зафиксировать прогресс сразу после успеха
                save_state(
                    STATE_FILE,
                    last_created_at=e["created_at"],
                    last_entry_id=e["entry_id"],
                )
                sent += 1
                print(f"Sent entry_id={e['entry_id']} | Updated state: {e['created_at']} / {e['entry_id']}")

            print(f"Sent {sent} entries as single messages.")

    except Exception as ex:
        # Важно: при ошибке мы НЕ перескакиваем state на конец пачки
        print(f"ERROR while sending. sent={sent}/{total}. state remains at last successful item. {ex}")
        raise

    # Чистим канал только если отправили ВСЕ новые записи без ошибок
    if CLEAR_AFTER_SEND and sent == total:
        if not THINGSPEAK_USER_API_KEY:
            raise SystemExit("CLEAR_AFTER_SEND=1, but THINGSPEAK_USER_API_KEY is not set")
        clear_thingspeak_channel(THINGSPEAK_CHANNEL_ID, THINGSPEAK_USER_API_KEY)
        print("Channel cleared.")
