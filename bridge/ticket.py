"""Post-call webhook sender - formats payload for supportdesk webhook."""

import logging
import httpx
from bridge.config import get_settings
from bridge import db

logger = logging.getLogger(__name__)


async def send_webhook(
    call_sid: str,
    caller_number: str,
    caller_name: str,
    business_name: str,
    duration: int,
    summary: str,
    sentiment: str,
    recording_url: str,
    start_time: str,
):
    """Send webhook in supportdesk-compatible format.

    Expected format by /api/main/tickets/webhook.ts:
    {
      "event": "call_analyzed",
      "call": {
        "call_id": "...",
        "from_number": "...",
        "recording_url": "...",
        "duration_ms": 45000,
        "direction": "inbound",
        "call_analysis": {
          "custom_analysis_data": {
            "call_summary_robotpos": "..."
          }
        }
      }
    }
    """
    settings = get_settings()
    webhook_url = settings.get("webhook_url", "").strip()

    if not webhook_url:
        logger.info("No webhook_url configured, skipping for call %s", call_sid)
        return

    # Build call summary with caller info
    summary_parts = []
    if caller_name:
        summary_parts.append(f"Arayan: {caller_name}")
    if business_name:
        summary_parts.append(f"Isletme: {business_name}")
    if summary:
        summary_parts.append(f"Sorun: {summary}")
    if sentiment:
        summary_parts.append(f"Duygu: {sentiment}")

    call_summary = "\n".join(summary_parts)

    payload = {
        "event": "call_analyzed",
        "call": {
            "call_id": call_sid,
            "from_number": caller_number,
            "to_number": "",
            "recording_url": recording_url,
            "duration_ms": duration * 1000,
            "direction": "inbound",
            "call_analysis": {
                "custom_analysis_data": {
                    "call_summary_robotpos": call_summary,
                },
                "call_summary": call_summary,
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code in (200, 201):
                data = resp.json()
                ticket_id = data.get("id") or ""
                if ticket_id:
                    await db.update_ticket(call_sid, str(ticket_id))
                logger.info("Webhook sent for call %s: %s", call_sid, resp.status_code)
            else:
                logger.error(
                    "Webhook failed for call %s: %s %s",
                    call_sid, resp.status_code, resp.text[:200],
                )
    except Exception as e:
        logger.error("Webhook error for call %s: %s", call_sid, e)
