import asyncio
import logging
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

from bot.notifier import Notifier
from bot.scheduler import build_scheduler
from bot.sheets import init_sheet
from bot.vk_listener import build_bot
from db.database import init_db


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("vkbottle").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def get_tokens() -> list[str]:
    """Поддерживает обе формы: VK_GROUP_TOKENS=tok1,tok2 или legacy VK_GROUP_TOKEN=tok"""
    multi = os.getenv("VK_GROUP_TOKENS", "").strip()
    if multi:
        return [t.strip() for t in multi.split(",") if t.strip()]
    single = os.getenv("VK_GROUP_TOKEN", "").strip()
    return [single] if single else []


def main() -> None:
    setup_logging()
    logger = logging.getLogger("main")

    tokens = get_tokens()
    if not tokens:
        raise RuntimeError("No VK tokens set (VK_GROUP_TOKENS or VK_GROUP_TOKEN)")

    logger.info("Resolving group_id for %d token(s)...", len(tokens))

    def _resolve_group_id(token: str) -> int:
        # sync через requests, чтобы не создавать asyncio loop до vkbottle
        r = requests.get(
            "https://api.vk.com/method/groups.getById",
            params={"access_token": token, "v": "5.199"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"VK API error: {data['error']}")
        groups = data["response"]["groups"]
        if not groups:
            raise RuntimeError(f"No group for token ...{token[-8:]}")
        return groups[0]["id"]

    group_ids = [_resolve_group_id(t) for t in tokens]
    for tok, gid in zip(tokens, group_ids):
        logger.info("  token...%s -> group_id=%s", tok[-8:], gid)

    bots = [
        build_bot(tok, gid, label=f"g{gid}")
        for tok, gid in zip(tokens, group_ids)
    ]
    primary = bots[0]  # его loop_wrapper будет драйвером для всех

    notifier = Notifier(apis=[b.api for b in bots])
    scheduler = build_scheduler(notifier=notifier)

    async def _startup() -> None:
        logger.info("Initializing database...")
        await init_db()
        logger.info("Initializing Google Sheets...")
        await init_sheet()
        logger.info("Starting scheduler...")
        scheduler.start()

        # Polling доп. ботов запускаем внутри уже работающего event loop —
        # тогда aiohttp ClientSession каждого бота привязывается к корректному loop.
        for extra in bots[1:]:
            asyncio.create_task(_run_polling(extra))

        logger.info("Startup complete — %d bot(s) listening", len(bots))

    async def _shutdown() -> None:
        logger.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)

    primary.loop_wrapper.on_startup.append(_startup())
    primary.loop_wrapper.on_shutdown.append(_shutdown())

    logger.info("Starting VK Long Poll for all bots...")
    primary.run_forever()


async def _run_polling(bot) -> None:
    """Inner polling — копия логики vkbottle.Bot.run_polling без loop_wrapper.run().
    Используется для extra-ботов, чтобы они работали в общем loop с primary."""
    async for event in bot.polling.listen():
        for update in event.get("updates", []):
            asyncio.create_task(bot.router.route(update, bot.polling.api))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
