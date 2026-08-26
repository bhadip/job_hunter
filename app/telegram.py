import requests

from .config import settings

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000


def _escape(text) -> str:
    """Escape HTML special chars for Telegram's HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_message(text: str, log=print) -> bool:
    if not settings.telegram_configured:
        log("[TELEGRAM] Not configured - skipping notification.")
        return False
    chunks = [text[i : i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(
                API.format(token=settings.TELEGRAM_BOT),
                json={
                    "chat_id": settings.TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                log(f"[TELEGRAM] HTTP {resp.status_code}: {resp.text[:200]}")
                ok = False
        except Exception as exc:
            log(f"[TELEGRAM] Send failed: {exc}")
            ok = False
    return ok


def notify_run(stats: dict, log=print) -> bool:
    user = stats.get("user_email", "")
    user_part = f" for {_escape(user)}" if user else ""
    lines = [
        f"<b>Job Hunter run {_escape(stats.get('status', 'done'))}</b> (v{settings.VERSION}){user_part}",
        f"Keywords: {stats.get('keywords', 0)} | Location: {_escape(stats.get('location', '-'))}",
        f"Scraped: {stats.get('scraped', 0)} | New: {stats.get('added', 0)} "
        f"| Dupes skipped: {stats.get('dupes', 0)}",
        f"Scored: {stats.get('scored', 0)} | >= {stats.get('threshold', 65)}%: "
        f"{stats.get('qualified', 0)} | Files generated: {stats.get('generated', 0)}",
        f"LLM tokens used: {stats.get('tokens', 0)}",
        f"Duration: {stats.get('duration', '-')}",
    ]
    if stats.get("dry_run"):
        lines.append("(DRY RUN - nothing written to the sheet, no files generated)")
    top = stats.get("top_matches") or []
    if top:
        lines.append("")
        lines.append("<b>Top matches:</b>")
        for match in top[:5]:
            lines.append(
                f"- {match['score']}% - {_escape(match['company'])}: {_escape(match['position'])}\n"
                f"  {_escape(match['url'])}"
            )
    if stats.get("output_dir"):
        lines.append("")
        lines.append(f"Output: {_escape(stats['output_dir'])}")
    if stats.get("errors"):
        lines.append("")
        lines.append(f"<b>Errors: {stats['errors']}</b>")
        for detail in (stats.get("error_details") or [])[:5]:
            lines.append(f"- {_escape(str(detail)[:200])}")
    return send_message("\n".join(lines), log=log)
