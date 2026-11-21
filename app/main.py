"""Entrypoint: one bot instance with PostgreSQL and a durable intro queue."""
import asyncio
import logging
import os
from secrets import compare_digest
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
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


app = FastAPI()
_webhook_lock = asyncio.Lock()
_webhook_runtime: tuple[Bot, Dispatcher] | None = None


async def get_webhook_runtime() -> tuple[Bot, Dispatcher]:
    global _webhook_runtime

    async with _webhook_lock:
        if _webhook_runtime is None:
            cfg = load_settings()
            db = Database(cfg.database_url)
            try:
                await db.init()
                handlers.configure(db, cfg)
                dispatcher = Dispatcher(storage=MemoryStorage())
                dispatcher.include_router(handlers.router)
                bot = Bot(cfg.bot_token.get_secret_value())
            except BaseException:
                await db.close()
                raise

            _webhook_runtime = bot, dispatcher

        return _webhook_runtime


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not compare_digest(received.encode(), expected.encode()):
        raise HTTPException(status_code=403)

    bot, dispatcher = await get_webhook_runtime()
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dispatcher.feed_update(bot, update)
    return {"ok": True}


if __name__=='__main__':
    asyncio.run(main())
