"""Persistent aiogram FSM storage backed by the existing PostgreSQL database."""
import hashlib
import json
from dataclasses import asdict

from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.state import State
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.models import BotSetting


class PostgresStorage(BaseStorage):
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _key(key: StorageKey, field: str) -> str:
        digest = hashlib.sha256(json.dumps(asdict(key), sort_keys=True).encode()).hexdigest()
        return f'fsm:{field}:{digest}'

    async def _get(self, key, field):
        async with self.db.sessions() as session:
            row = await session.get(BotSetting, self._key(key, field))
            return json.loads(row.value) if row is not None else None

    async def _set(self, key, field, value):
        name = self._key(key, field)
        async with self.db.sessions() as session:
            if value is None:
                await session.execute(delete(BotSetting).where(BotSetting.key == name))
            else:
                stmt = insert(BotSetting).values(key=name, value=json.dumps(value, ensure_ascii=False))
                await session.execute(stmt.on_conflict_do_update(
                    index_elements=[BotSetting.key], set_={'value': stmt.excluded.value}))
            await session.commit()

    async def set_state(self, key: StorageKey, state=None):
        await self._set(key, 'state', state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey):
        return await self._get(key, 'state')

    async def set_data(self, key: StorageKey, data: dict):
        if not isinstance(data, dict):
            raise TypeError('FSM data must be a dict')
        await self._set(key, 'data', data or None)

    async def get_data(self, key: StorageKey):
        return await self._get(key, 'data') or {}

    async def close(self):
        # The application owns the database engine.
        pass
