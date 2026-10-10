"""Reliable read-only Telegram report delivery.

Telegram limits messages to 4096 UTF-16 code units. Send long dashboards as
ordered chunks with the interactive menu attached to the last chunk. Fail the
hourly workflow visibly if a message was not accepted by Telegram.
"""
import math

MAX_UNITS = 3500


def _units(value):
    return len(value.encode("utf-16-le")) // 2


def split_report(value):
    text = str(value or "").strip()
    if not text:
        raise ValueError("Empty Telegram dashboard")
    parts = []
    while text:
        if _units(text) <= MAX_UNITS:
            parts.append(text)
            break
        units = 0
        limit = 0
        for index, char in enumerate(text):
            char_units = 2 if ord(char) > 0xFFFF else 1
            if units + char_units > MAX_UNITS:
                break
            units += char_units
            limit = index + 1
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        part = text[:cut].rstrip()
        if not part:
            part, cut = text[:limit], limit
        parts.append(part)
        text = text[cut:].lstrip("\n")
    return parts


def send_report(api_fn, text, keyboard, chat_id):
    if not chat_id:
        raise RuntimeError("CHAT_ID not configured for hourly Telegram report")
    pieces = split_report(text)
    for index, part in enumerate(pieces):
        payload = {"chat_id": chat_id, "text": part}
        if index == len(pieces) - 1 and keyboard:
            payload["reply_markup"] = keyboard
        response = api_fn("sendMessage", payload)
        if not isinstance(response, dict) or response.get("ok") is not True:
            description = response.get("description", "no valid Telegram response") if isinstance(response, dict) else "Telegram delivery returned no response"
            raise RuntimeError(f"Telegram hourly send failed on part {index+1}/{len(pieces)}: {description}")
        print(f"Telegram hourly delivered part {index+1}/{len(pieces)} ({_units(part)} UTF-16 units)")
    return len(pieces)
