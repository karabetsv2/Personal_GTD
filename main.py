"""Точка входа: сборка бота, планировщик напоминаний, запуск polling."""
import asyncio
import logging
from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import utils
from handlers import BOT_COMMANDS, build_balance_report, router

logger = logging.getLogger(__name__)


async def check_reminders(bot: Bot) -> None:
    """Раз в config.REMINDER_CHECK_INTERVAL_SECONDS отправляет просроченные напоминания владельцу."""
    owner_chat_id = await db.get_owner_chat_id()
    if owner_chat_id is None:
        return
    now_iso = utils.dt_to_iso(utils.now_local())
    due = await db.get_due_reminders(now_iso)
    for reminder in due:
        deadline_dt = utils.dt_from_iso(reminder["remind_at"]) + timedelta(minutes=config.REMINDER_LEAD_TIME_MINUTES)
        text = (
            f"⏰ Через {config.REMINDER_LEAD_TIME_MINUTES} мин дедлайн: "
            f"«{reminder['task_title']}» ({utils.format_dt(deadline_dt)})"
        )
        try:
            await bot.send_message(owner_chat_id, text)
        except Exception:
            logger.exception("Не удалось отправить напоминание task_id=%s", reminder["task_id"])
            continue
        await db.mark_reminder_sent(reminder["id"])


async def send_monthly_balance(bot: Bot) -> None:
    """Раз в месяц (1-е число, 09:00) присылает тот же отчёт, что и команда /balance."""
    owner_chat_id = await db.get_owner_chat_id()
    if owner_chat_id is None:
        return
    try:
        report = await build_balance_report()
    except Exception:
        logger.exception("Не удалось сформировать ежемесячный отчёт баланса")
        return
    await bot.send_message(owner_chat_id, "📅 Ежемесячный обзор баланса\n\n" + report)


async def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await db.init_db()

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        check_reminders,
        "interval",
        seconds=config.REMINDER_CHECK_INTERVAL_SECONDS,
        args=(bot,),
    )
    scheduler.add_job(
        send_monthly_balance,
        "cron",
        day=1,
        hour=9,
        minute=0,
        args=(bot,),
    )
    scheduler.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        logger.info("Бот запущен, начинаю polling")
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await db.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
