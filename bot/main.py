import asyncio
import logging

from bot.config import get_settings
from bot.db import init_db
from bot.handlers import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    init_db()
    bot = TelegramBot(settings)
    await bot.poll()


if __name__ == "__main__":
    asyncio.run(main())
