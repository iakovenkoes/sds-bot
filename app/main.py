"""Entrypoint: polling or webhook mode, both with the durable scheduler worker."""
import asyncio
import logging
import os
from secrets import compare_digest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from app.config import load_settings
from app.db import Database
from app.scheduler import scheduler_worker
from app import handlers

log = logging.getLogger('atlantis')


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
    worker=asyncio.create_task(scheduler_worker(bot,db))
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
_webhook_runtime: tuple[Bot, Dispatcher, asyncio.Task] | None = None


async def get_webhook_runtime() -> tuple[Bot, Dispatcher]:
    global _webhook_runtime

    async with _webhook_lock:
        if _webhook_runtime is None:
            cfg = load_settings()
            db = Database(cfg.database_url)
            try:
                await db.init()
                await db.seed_initial_admins()
                await db.seed_trip_attendees()
                handlers.configure(db, cfg)
                dispatcher = Dispatcher(storage=MemoryStorage())
                dispatcher.include_router(handlers.router)
                bot = Bot(cfg.bot_token.get_secret_value())
                worker = asyncio.create_task(scheduler_worker(bot,db))
            except BaseException:
                await db.close()
                raise

            _webhook_runtime = bot, dispatcher, worker

        return _webhook_runtime[0], _webhook_runtime[1]


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
