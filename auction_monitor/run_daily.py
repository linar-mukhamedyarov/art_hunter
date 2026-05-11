"""
Art Hunter — Auction Monitor (Part 2).
Pipeline:
  Haiku  -> search lots via WebSearch
  Sonnet -> filter by target artists
  Opus   -> expert analysis (text, for the report)
  Python -> build Telegram messages from parsed data
  Haiku  -> send each message via MCP send_telegram_message tool
NO import anthropic. Subprocess only via src/cli.py.
"""
import sys
import json
import os
import re
from pathlib import Path
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

THIS_DIR = Path(__file__).parent
PROJECT_ROOT = THIS_DIR.parent
LOGS_DIR = THIS_DIR / "logs"
MCP_CONFIG = PROJECT_ROOT / "mcp_config.json"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT / "src"))
from cli import claude_call  # noqa: E402

MODEL_SEARCH = "claude-haiku-4-5-20251001"
MODEL_FILTER = "claude-sonnet-4-6"
MODEL_RECOMMEND = "claude-opus-4-7"

PROMPTS_DIR = THIS_DIR / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def load_artists() -> dict:
    return json.loads((PROJECT_ROOT / "config" / "artists.json").read_text(encoding="utf-8"))


def step_search(artists: dict) -> str:
    search_terms = []
    for artist in artists["artists"]:
        search_terms.extend(artist["search_terms"][:3])

    query = (
        "Find upcoming auction lots for these artists at Sotheby's, Christie's, Bonhams, Phillips "
        f"in 2026: {', '.join(search_terms)}"
    )

    print("  [Haiku] Searching auction lots via WebSearch...")
    return claude_call(
        prompt=query,
        system_prompt=load_prompt("search_auctions.md"),
        model=MODEL_SEARCH,
        web_search=True,
        step_name="search_lots",
        logs_dir=str(LOGS_DIR),
    )


def step_filter(raw_lots: str, artists: dict) -> str:
    artists_block = "\n".join(
        f"- {a['name_ru']} / {a['name_en']}: {', '.join(a['search_terms'])}"
        for a in artists["artists"]
    )
    prompt = (
        f"TARGET ARTISTS:\n{artists_block}\n\n"
        f"---\nRAW SEARCH RESULTS:\n{raw_lots}"
    )

    print("  [Sonnet] Filtering relevant lots...")
    return claude_call(
        prompt=prompt,
        system_prompt=load_prompt("filter_lots.md"),
        model=MODEL_FILTER,
        step_name="filter_lots",
        logs_dir=str(LOGS_DIR),
    )


def step_recommend(filtered_lots: str) -> str:
    """Opus: expert analysis text (saved to report)."""
    print("  [Opus] Expert analysis...")
    return claude_call(
        prompt=f"Analyze these auction lots:\n\n{filtered_lots}",
        system_prompt=load_prompt("recommend_lot.md"),
        model=MODEL_RECOMMEND,
        step_name="recommend_lots",
        logs_dir=str(LOGS_DIR),
    )


# ── Message building ──────────────────────────────────────────────────────────

def _extract_verdicts(opus_text: str) -> dict[str, tuple[str, str, str]]:
    """Find СТОИТ/НЕ СТОИТ/УТОЧНИТЬ verdicts per artist name in Opus text."""
    verdicts: dict[str, tuple[str, str]] = {}
    for verdict in ("НЕ УЧАСТВОВАТЬ", "УЧАСТВОВАТЬ", "УТОЧНИТЬ"):
        for m in re.finditer(re.escape(verdict), opus_text):
            start = max(0, m.start() - 200)
            context_before = opus_text[start : m.start()].lower()
            # Extract brief reason after verdict
            after = opus_text[m.end() : m.end() + 150].strip(" —\n:")
            reason = after.split("\n")[0][:100]
            verdicts[f"ctx_{m.start()}"] = (verdict, reason, context_before)
    return verdicts


def _find_verdict_for(artist: str, verdicts: dict) -> tuple[str, str]:
    """Pick the first verdict whose context contains the artist name."""
    name_lower = artist.lower()[:12]
    for key, (verdict, reason, ctx) in verdicts.items():
        if name_lower in ctx:
            return verdict, reason
    return "УТОЧНИТЬ", "Детали уточняются"


def _parse_lots_from_filter(filtered_text: str, artists: dict) -> list[dict]:
    """
    Extract structured lot data from Sonnet's free-form markdown output.
    Handles both MATCHED_LOT blocks and markdown tables.
    """
    lots = []

    # Strategy 1: explicit MATCHED_LOT blocks
    if "MATCHED_LOT" in filtered_text:
        for block in re.split(r"---+\s*\n", filtered_text):
            if "MATCHED_LOT" not in block:
                continue
            lot = {}
            for field in ("ARTIST", "TITLE", "AUCTION_HOUSE", "ESTIMATE", "SALE_DATE", "URL"):
                m = re.search(rf"{field}:\s*(.+)", block)
                if m:
                    lot[field.lower()] = m.group(1).strip()
            if lot.get("artist"):
                lots.append(lot)
        if lots:
            return lots

    # Strategy 2: markdown table rows  (| col | col | ...)
    # Look for rows mentioning known artist search terms
    all_terms = []
    for a in artists["artists"]:
        all_terms.extend(a["search_terms"])

    table_rows = re.findall(r"\|([^|\n]+)\|([^|\n]+)\|([^|\n]+)\|([^|\n]*)\|?", filtered_text)
    for row in table_rows:
        row_text = " | ".join(c.strip() for c in row)
        row_lower = row_text.lower()
        if any(t.lower() in row_lower for t in all_terms):
            # Skip header/separator rows
            if re.search(r"^[\s|:-]+$", row_text):
                continue
            # Try to extract date from any cell
            date_m = re.search(r"\b(\w+ \d{1,2}[–\-,]?\w*\s*20\d\d|20\d\d)", row_text)
            lots.append({
                "artist": row[0].strip().strip("*"),
                "title": row[1].strip().strip("*") if len(row) > 1 else "TBC",
                "auction_house": "",
                "estimate": "",
                "sale_date": date_m.group(0) if date_m else "TBC",
                "url": "",
                "raw_row": row_text,
            })

    return lots


def build_messages(filtered_text: str, opus_analysis: str, chat_id: str) -> list[dict]:
    """Build Telegram notification dicts [{chat_id, text}, ...] in Python."""
    today = datetime.now().strftime("%d.%m.%Y")

    artists = load_artists()
    lots = _parse_lots_from_filter(filtered_text, artists)
    lots = [l for l in lots if l.get("estimate") and l.get("url")]
    verdicts = _extract_verdicts(opus_analysis)

    messages = []

    if lots:
        for lot in lots:
            artist = lot.get("artist", "Unknown artist")
            title = lot.get("title", "TBC")
            house = lot.get("auction_house", "")
            estimate = lot.get("estimate", "TBC")
            sale_date = lot.get("sale_date", "TBC")
            url = lot.get("url", "")

            verdict, reason = _find_verdict_for(artist, verdicts)

            lines = [
                f"\U0001f3a8 {artist} — {title}",   # 🎨
                f"\U0001f4b0 Эстимэт: {estimate}",  # 💰 Эстимейт:
                f"\U0001f4c5 Торги: {sale_date}",             # 📅 Торги:
            ]
            if house:
                lines.append(f"\U0001f3db {house}")   # 🏛
            if url:
                lines.append(f"\U0001f517 {url}")     # 🔗
            lines.append(f"\U0001f4ac Мнение агента: {verdict} — {reason}")  # 💬

            messages.append({"chat_id": chat_id, "text": "\n".join(lines)})

    # Always send Opus summary as a final message (truncated to Telegram limit)
    opus_truncated = opus_analysis[:3800].rsplit("\n", 1)[0] if len(opus_analysis) > 3800 else opus_analysis
    messages.append({
        "chat_id": chat_id,
        "text": f"\U0001f4ca Art Hunter — Экспертный анализ ({today})\n\n{opus_truncated}",
    })

    return messages


# ── Send step ─────────────────────────────────────────────────────────────────

def step_send_messages(messages: list[dict]) -> int:
    """Haiku + MCP: send each message via send_telegram_message tool."""
    sent = 0
    for i, msg in enumerate(messages, 1):
        chat_id = str(msg.get("chat_id", ""))
        text = str(msg.get("text", ""))
        if not chat_id or not text:
            continue

        print(f"  [Haiku+MCP] Sending message {i}/{len(messages)}...")
        # Pass text in system prompt context to avoid shell-quoting issues with apostrophes
        safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
        claude_call(
            prompt=(
                f'Use the send_telegram_message tool. '
                f'Arguments: chat_id="{chat_id}", text="{safe_text}"'
            ),
            system_prompt=(
                "You MUST call the send_telegram_message tool immediately with the exact "
                "chat_id and text provided. After calling it, output only: done."
            ),
            model=MODEL_SEARCH,
            step_name=f"send_telegram_{i}",
            mcp_config=str(MCP_CONFIG),
            logs_dir=str(LOGS_DIR),
        )
        sent += 1

    return sent


def save_report(raw: str, filtered: str, analysis: str, messages: list, sent: int) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = OUTPUT_DIR / f"auction_report_{ts}.md"

    msgs_text = "\n\n---\n".join(
        f"**Message {i+1}:**\n```\n{m.get('text','')}\n```"
        for i, m in enumerate(messages)
    )

    content = (
        f"# Art Hunter — Auction Monitor\n"
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"**Messages sent:** {sent}/{len(messages)}\n\n"
        f"---\n\n"
        f"## Step 1: Raw Search (Haiku)\n\n{raw}\n\n"
        f"---\n\n"
        f"## Step 2: Filtered Lots (Sonnet)\n\n{filtered}\n\n"
        f"---\n\n"
        f"## Step 3: Expert Analysis (Opus)\n\n{analysis}\n\n"
        f"---\n\n"
        f"## Step 4: Telegram Messages\n\n{msgs_text}\n"
    )
    out_file.write_text(content, encoding="utf-8")
    return out_file


def main():
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID not set.")
        print("  $env:TELEGRAM_CHAT_ID = 'your_chat_id'")
        sys.exit(1)
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set.")
        print("  $env:TELEGRAM_BOT_TOKEN = 'your_bot_token'")
        sys.exit(1)

    artists = load_artists()

    print("\nArt Hunter — Auction Monitor")
    print(f"Artists: {', '.join(a['name_ru'] for a in artists['artists'])}")
    print(f"Chat ID: {chat_id[:6]}***\n")

    t0 = datetime.now()
    raw_lots = step_search(artists)
    print(f"  Done in {int((datetime.now()-t0).total_seconds())}s, {len(raw_lots)} chars\n")

    t0 = datetime.now()
    filtered_lots = step_filter(raw_lots, artists)
    print(f"  Done in {int((datetime.now()-t0).total_seconds())}s, {len(filtered_lots)} chars\n")

    parsed = _parse_lots_from_filter(filtered_lots, artists)
    parsed = [l for l in parsed if l.get("estimate") and l.get("url")]

    if not parsed:
        print("  Нет лотов, прошедших фильтр. Opus не вызываем.")
        no_lots_text = "🔍 Art Hunter: предстоящих лотов по заданным художникам не найдено. Следующая проверка — завтра."
        safe_text = no_lots_text.replace("\\", "\\\\").replace('"', '\\"')
        claude_call(
            prompt=f'Use the send_telegram_message tool. Arguments: chat_id="{chat_id}", text="{safe_text}"',
            system_prompt=(
                "You MUST call the send_telegram_message tool immediately with the exact "
                "chat_id and text provided. After calling it, output only: done."
            ),
            model=MODEL_SEARCH,
            step_name="send_no_lots",
            mcp_config=str(MCP_CONFIG),
            logs_dir=str(LOGS_DIR),
        )
        save_report(raw_lots, filtered_lots, "(no lots found)", [], 1)
        print("Telegram: сообщение 'лотов не найдено' отправлено.")
        return

    t0 = datetime.now()
    analysis = step_recommend(filtered_lots)
    print(f"  Done in {int((datetime.now()-t0).total_seconds())}s, {len(analysis)} chars\n")

    messages = build_messages(filtered_lots, analysis, chat_id)
    print(f"  Built {len(messages)} messages to send\n")

    t0 = datetime.now()
    sent = step_send_messages(messages)
    print(f"  Done in {int((datetime.now()-t0).total_seconds())}s, {sent} sent\n")

    out_file = save_report(raw_lots, filtered_lots, analysis, messages, sent)
    print(f"Report saved: {out_file}")
    print(f"Telegram messages sent: {sent}/{len(messages)}")


if __name__ == "__main__":
    main()
