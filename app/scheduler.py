"""Durable, Moscow-time scheduled campaign messages.

One polling worker per bot. Uses existing planned_messages for per-user deduplication;
BotSetting is a persistent marker for enqueueing each timed event.
"""
from __future__ import annotations
import asyncio
import html
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from aiogram import Bot
from sqlalchemy import select, or_
from sqlalchemy.dialects.postgresql import insert

from app.models import BotSetting, PlannedMessage, Registration, TripAttendee, User
from app.rules import LECTURE_ROOMS, LECTURE_TITLES, normalize_whitelist_name

LOG = logging.getLogger('atlantis.scheduler')
DATA = Path(__file__).resolve().parent.parent / 'data'
EVENTS = json.loads((DATA / 'scheduled_messages_agreed.json').read_text('utf-8'))['messages']
SCHEDULED_TEXTS = json.loads((DATA / 'scheduled_texts.json').read_text('utf-8'))
MOSCOW = ZoneInfo('Europe/Moscow')
STORY_IDS = {'lore_1', 'lore_2', 'lore_3'}
LAST_EVENT = datetime(2026, 11, 29, 9, tzinfo=MOSCOW)


def event_time(item):
    return datetime.fromisoformat(item['send_at']).astimezone(timezone.utc)


def payload_for(kind):
    """Never expose placeholders in participant messages."""
    payload = SCHEDULED_TEXTS[kind]
    if kind.startswith('lecture_reminder_'):
        day = int(kind.rsplit('_', 1)[1])
        payload = payload.replace('«Тема лекции»', html.escape(LECTURE_TITLES[day]))
        payload = payload.replace('«Аудитория»', LECTURE_ROOMS[day])
        payload = payload.replace('Аудитория: '+LECTURE_ROOMS[day],
                                  'Аудитория: '+LECTURE_ROOMS[day]+'\nАдрес: Кантемировская, 3к1')
    return payload


async def audience_ids(session, item):
    aud = item['audience']
    if aud == 'all':
        return list((await session.scalars(select(User.id))).all())
    if aud.startswith('registered_'):
        day = int(aud.rsplit('_', 1)[1])
        return list((await session.scalars(select(Registration.user_id).where(
            Registration.day == day, Registration.active.is_(True)))).all())
    if aud == 'trip':
        users = (await session.scalars(select(User).where(User.full_name.is_not(None)))).all()
        attendees = (await session.scalars(select(TripAttendee))).all()
        by_name = {row.normalized_name: row.telegram_id for row in attendees}
        return [u.id for u in users if (name := normalize_whitelist_name(u.full_name)) in by_name
                and by_name[name] in (None, u.id)]
    raise ValueError(f'Unknown audience: {aud}')


async def enqueue_due_events(db, now=None):
    now = now or datetime.now(timezone.utc)
    for item in EVENTS:
        when = event_time(item)
        if now < when:
            continue
        marker_key = 'auto_v010_' + item['id']
        async with db.sessions() as session:
            claim = await session.execute(insert(BotSetting).values(
                key=marker_key, value='processing'
            ).on_conflict_do_nothing(index_elements=['key']).returning(BotSetting.key))
            if claim.scalar_one_or_none() is None:
                continue
            marker = await session.get(BotSetting, marker_key)
            # Do not surprise users with historical broadcasts on a delayed deployment.
            # Late-join story drops are separately queued by /start below.
            if now - when > timedelta(minutes=30):
                marker.value = 'skipped_late'
                await session.commit()
                LOG.warning('Skipped stale event %s (deployment was too late)', item['id'])
                continue
            ids = await audience_ids(session, item)
            for uid in ids:
                await session.execute(insert(PlannedMessage).values(
                    user_id=uid, kind='auto:'+item['id'], due_at=when
                ).on_conflict_do_nothing(index_elements=['user_id', 'kind']))
            marker.value = f'queued:{len(ids)}'
            await session.commit()
            LOG.info('Enqueued %s for %d users', item['id'], len(ids))


async def queue_late_join_story(session, user_id, now=None):
    """Called within first /start transaction; missed story drops are staggered 12 seconds."""
    now = now or datetime.now(timezone.utc)
    if now >= LAST_EVENT.astimezone(timezone.utc):
        return
    prior = [x for x in EVENTS if x['id'] in STORY_IDS and event_time(x) <= now]
    for index, item in enumerate(prior):
        await session.execute(insert(PlannedMessage).values(
            user_id=user_id, kind='auto:'+item['id'],
            due_at=now + timedelta(seconds=24+index*12)
        ).on_conflict_do_nothing(index_elements=['user_id', 'kind']))


async def deliver_pending(bot: Bot, db):
    # Keep each row locked until its own delivery transaction finishes.
    for _ in range(30):
        async with db.sessions() as session:
            item = await session.scalar(select(PlannedMessage).where(
                PlannedMessage.delivered_at.is_(None),
                PlannedMessage.due_at <= datetime.now(timezone.utc),
                or_(PlannedMessage.kind == 'intro', PlannedMessage.kind.startswith('auto:'))
            ).order_by(PlannedMessage.due_at, PlannedMessage.id).limit(1).with_for_update(skip_locked=True))
            if item is None:
                return
            try:
                if item.kind == 'intro':
                    from app.messages import INSTRUCTION
                    content = INSTRUCTION
                else:
                    content = payload_for(item.kind[5:])
                await bot.send_message(item.user_id, content, parse_mode='HTML')
            except Exception as exc:
                LOG.warning('Delivery %s to %s failed: %s', item.kind, item.user_id, exc)
                item.due_at = datetime.now(timezone.utc) + timedelta(minutes=2)
            else:
                item.delivered_at = datetime.now(timezone.utc)
            await session.commit()
        await asyncio.sleep(0.08)


async def scheduler_worker(bot: Bot, db):
    while True:
        try:
            await enqueue_due_events(db)
            await deliver_pending(bot, db)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('Scheduler failed; retrying')
        await asyncio.sleep(5)
