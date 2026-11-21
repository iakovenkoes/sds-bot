"""Database connection and transaction helpers."""
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from app.models import Base, User, TripAttendee, UserRole, BotSetting
from app.rules import normalize_whitelist_name
from pathlib import Path
import json


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self):
        # Stage 1 convenience; change to Alembic migration before production.
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)


    async def seed_initial_admins(self):
        """One-time bootstrap of v0.8 admins plus v0.8.1 addition.

        Previously imported roles are never re-granted after a manual revocation.
        The v0.8.1 migration also runs against an already initialized database.
        """
        path = Path(__file__).resolve().parent.parent / 'data' / 'initial_admin_ids.json'
        ids = [int(x) for x in json.loads(path.read_text('utf-8'))['ids']]
        async with self.sessions() as session:
            old_marker = await session.get(BotSetting, 'initial_admins_v08_imported')
            if old_marker is None:
                pending = []
                for uid in ids:
                    user = await session.get(User, uid)
                    if user is None:
                        pending.append(uid)
                    elif await session.get(UserRole, (uid, 'admin')) is None:
                        session.add(UserRole(user_id=uid, role='admin'))
                session.add(BotSetting(key='initial_admins_v08_imported', value='true'))
                session.add(BotSetting(key='pending_admins_v08', value=json.dumps(pending)))
                await session.commit()

            # A database bootstrapped by v0.8 already has the v08 marker,
            # so the newly supplied ID must be migrated separately.
            migration_key = 'admin_971043456_v081_imported'
            if await session.get(BotSetting, migration_key) is None:
                uid = 971043456
                user = await session.get(User, uid)
                if user is not None:
                    if await session.get(UserRole, (uid, 'admin')) is None:
                        session.add(UserRole(user_id=uid, role='admin'))
                else:
                    pending_row = await session.get(BotSetting, 'pending_admins_v08')
                    if pending_row is None:
                        pending_row = BotSetting(key='pending_admins_v08', value='[]')
                        session.add(pending_row)
                    pending = json.loads(pending_row.value)
                    if uid not in pending:
                        pending.append(uid)
                        pending_row.value = json.dumps(pending)
                session.add(BotSetting(key=migration_key, value='true'))
                await session.commit()

    async def seed_trip_attendees(self):
        """Non-destructive insertion. Existing Telegram bindings are preserved on updates."""
        data = json.loads((Path(__file__).resolve().parent.parent / 'data' / 'attendee_whitelist.json').read_text('utf-8'))
        async with self.sessions() as session:
            for name in data['names']:
                norm = normalize_whitelist_name(name)
                found = await session.get(TripAttendee, norm)
                if not found:
                    session.add(TripAttendee(normalized_name=norm, full_name=name))
            await session.commit()

    async def close(self):
        await self.engine.dispose()


async def upsert_user(session, telegram_user):
    user = await session.get(User, telegram_user.id)
    first_visit = user is None
    if first_visit:
        user = User(id=telegram_user.id, username=telegram_user.username)
        session.add(user)
    else:
        user.username = telegram_user.username
    await session.flush()
    pending = await session.get(BotSetting, 'pending_admins_v08')
    if pending:
        ids = json.loads(pending.value)
        if user.id in ids:
            if not await session.get(UserRole, (user.id, 'admin')):
                session.add(UserRole(user_id=user.id, role='admin'))
            ids.remove(user.id)
            pending.value = json.dumps(ids)
            await session.flush()
    return user, first_visit
