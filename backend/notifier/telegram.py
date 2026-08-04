import logging
import httpx
from config import settings

logger = logging.getLogger("telegram_notifier")

class TelegramNotifier:
    def __init__(self, bot_token: str = None, chat_id: str = None, enabled: bool = None):
        self.bot_token = bot_token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.enabled = enabled if enabled is not None else settings.TELEGRAM_ENABLE

    async def send_message(self, text: str) -> bool:
        if not self.enabled or not self.bot_token or not self.chat_id:
            logger.debug("Telegram notification disabled or unconfigured.")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 200:
                    logger.info("Telegram notification sent successfully.")
                    return True
                else:
                    logger.error(f"Telegram API failed: {res.status_code} - {res.text}")
                    return False
        except Exception as e:
            logger.error(f"Telegram notification error: {e}")
            return False

telegram_notifier = TelegramNotifier()
