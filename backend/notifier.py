import logging

import httpx

from backend.config import settings

logger = logging.getLogger("telegram")


async def send_telegram(message: str) -> bool:
    if not settings.telegram_enabled or not settings.telegram_token or not settings.telegram_chat_id:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message,
                },
            )
        if response.is_success:
            return True
        logger.error("Telegram returned HTTP %s", response.status_code)
    except Exception:
        logger.exception("Telegram notification failed")
    return False
