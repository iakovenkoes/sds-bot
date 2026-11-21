"""Entrypoint: one bot instance with PostgreSQL and a durable intro queue."""
import asyncio
import logging
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.config import load_settings
from app.db import Database
from app.models import PlannedMessage
from app.messages import INSTRUCTION
from app import handlers

log = logging.getLogger('atlantis')


async def intro_worker(bot: Bot, db: Database):
    while True:
        try:
            async with db.sessions() as s:
                messages=(await s.scalars(select(PlannedMessage).where(
                    PlannedMessage.delivered_at.is_(None),
                    PlannedMessage.kind=='intro',
                    PlannedMessage.due_at<=datetime.now(timezone.utc)
                ).order_by(PlannedMessage.id).limit(20).with_for_update(skip_locked=True))).all()
                for item in messages:
                    try:
                        await bot.send_message(item.user_id, INSTRUCTION, parse_mode='HTML')
                        item.delivered_at=datetime.now(timezone.utc)
                    except Exception:
                        log.exception('Intro delivery failed for user %s', item.user_id)
                await s.commit()
        except Exception:
            log.exception('Background worker failed; will retry')
        await asyncio.sleep(5)


async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    cfg=load_settings()
    db=Database(cfg.database_url)
    await db.init()
    await db.seed_initial_admins()
    await db.seed_trip_attendees()
    handlers.configure(db,cfg)
    bot=Bot(cfg.bot_token.get_secret_value())
    dp=Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.router)
    worker=asyncio.create_task(intro_worker(bot,db))
    try:
        me=await bot.get_me()
        log.info('Bot started: @%s, id=%s', me.username, me.id)
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await db.close()


if __name__=='__main__':
    asyncio.run(main())
