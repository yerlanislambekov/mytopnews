#!/usr/bin/env python3
"""
Daily Kazakhstan & World news digest.

Fetches raw headlines via RSS (no API key needed), asks Claude (Anthropic API)
to synthesize them into a digest, sends the result to Telegram recipients,
and commits a copy of the digest into digests/ in this repo.

Required environment variables (set as GitHub Actions secrets):
  TELEGRAM_BOT_TOKEN  - Telegram bot token (same one used by send_digest.py)
  ANTHROPIC_API_KEY   - Anthropic API key from console.anthropic.com
"""
import os
import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests
import anthropic

ALMATY_TZ = timezone(timedelta(hours=5))
TODAY = datetime.now(ALMATY_TZ).strftime("%Y-%m-%d")

ROOT = Path(__file__).parent
RECIPIENTS_FILE = ROOT / "recipients.json"
DIGEST_DIR = ROOT / "digests"
DIGEST_DIR.mkdir(exist_ok=True)

KZ_FEEDS = [
    "https://news.google.com/rss/search?q=%D0%9A%D0%B0%D0%B7%D0%B0%D1%85%D1%81%D1%82%D0%B0%D0%BD&hl=ru&gl=KZ&ceid=KZ:ru",
    "https://tengrinews.kz/rss/",
]
WORLD_FEEDS = [
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=world%20news&hl=en-US&gl=US&ceid=US:en",
]

MAX_ITEMS_PER_FEED = 12


def fetch_feed_items(urls):
    items = []
    for url in urls:
        try:
            parsed = feedparser.parse(url)
            for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
                source = ""
                if entry.get("source"):
                    source = entry.get("source", {}).get("title", "")
                summary = re.sub("<[^<]+?>", "", entry.get("summary", "")).strip()
                items.append({
                    "title": entry.get("title", "").strip(),
                    "summary": summary[:400],
                    "link": entry.get("link", ""),
                    "source": source,
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            print(f"WARN: failed to fetch {url}: {e}")
    return items


def format_items(items):
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. {it['title']}\n   {it['summary']}\n   Источник: {it['source']} {it['link']}"
        )
    return "\n".join(lines)


def build_digest_with_claude(kz_items, world_items):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Ты редактор утреннего новостного дайджеста на русском языке. Сегодня {TODAY}.

Вот сырые заголовки и описания новостей по Казахстану:
{format_items(kz_items)}

Вот сырые заголовки и описания мировых новостей:
{format_items(world_items)}

Составь дайджест в формате Markdown со следующей структурой:
# Утренний дайджест новостей — {TODAY}

## Казахстан
(3-5 самых важных и разнообразных новостей: политика, экономика, общество.
Для каждой: **Заголовок**, 2-3 предложения сути, "Источник: [name](url)")

## Мир
(3-5 главных международных новостей, тот же формат)

## Резюме
(2-4 предложения общего резюме дня)

Пиши кратко, информативно, без воды, без выдумывания фактов — используй только
информацию из предоставленных заголовков и описаний. Если каких-то деталей не
хватает, не домысливай их."""

    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def md_to_html(text):
    text = re.sub(r'^#{1,3}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def split_message(text, max_len=4096):
    parts, current = [], ""
    for para in text.split("\n\n"):
        block = para + "\n\n"
        if len(current) + len(block) > max_len:
            if current:
                parts.append(current.strip())
            current = block if len(block) <= max_len else ""
            if len(block) > max_len:
                while block:
                    parts.append(block[:max_len].strip())
                    block = block[max_len:]
        else:
            current += block
    if current.strip():
        parts.append(current.strip())
    return parts


def send_message(bot_token, chat_id, text, part_num=0, total_parts=1):
    if total_parts > 1:
        text = f"<i>({part_num}/{total_parts})</i>\n\n" + text
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": False
    }, timeout=15)
    return r.json()


def main():
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]

    print("Fetching RSS feeds...")
    kz_items = fetch_feed_items(KZ_FEEDS)
    world_items = fetch_feed_items(WORLD_FEEDS)
    print(f"KZ items: {len(kz_items)}, World items: {len(world_items)}")

    if not kz_items and not world_items:
        raise RuntimeError("No RSS items fetched - aborting, refusing to send an empty digest")

    print("Building digest with Claude...")
    digest_md = build_digest_with_claude(kz_items, world_items)

    digest_path = DIGEST_DIR / f"{TODAY}_дайджест.md"
    digest_path.write_text(digest_md, encoding="utf-8")
    print(f"Saved: {digest_path}")

    recipients = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
    print(f"Recipients: {len(recipients)}")

    digest_html = md_to_html(digest_md)
    parts = split_message(digest_html)

    ok, fail = 0, 0
    for rec in recipients:
        chat_id = rec["chat_id"]
        username = rec.get("username", "")
        print(f"-> {username} ({chat_id})")
        success = True
        for i, part in enumerate(parts, 1):
            res = send_message(bot_token, chat_id, part, i, len(parts))
            if res.get("ok"):
                print(f"   OK part {i}/{len(parts)}")
            else:
                print(f"   ERROR part {i}: {res}")
                success = False
                break
        if success:
            ok += 1
        else:
            fail += 1

    print(f"Done. Sent: {ok}, Failed: {fail}")
    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
