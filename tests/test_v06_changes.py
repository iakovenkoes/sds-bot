"""Regressions for guest eligibility, 12:10 speaker release, and video notes."""
import json
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app import rules
from app.media import classify_task_part, forward_task_part
from app.models import Base, TripAttendee


def _msg(**kwargs):
    values = dict(text=None, photo=None, video=None, video_note=None, document=None, caption=None)
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_circle_attachment_is_stored_as_reusable_file_id():
    kind, text, file_id = classify_task_part(_msg(video_note=SimpleNamespace(file_id='circle_123')))
    assert (kind, text, file_id) == ('video_note', None, 'circle_123')


def test_multiple_other_attachment_types_unchanged():
    assert classify_task_part(_msg(text='hello')) == ('text', 'hello', None)
    assert classify_task_part(_msg(photo=[SimpleNamespace(file_id='small'), SimpleNamespace(file_id='large')])) == ('photo', None, 'large')
    assert classify_task_part(_msg(video=SimpleNamespace(file_id='vid'))) == ('video', None, 'vid')
    assert classify_task_part(_msg(document=SimpleNamespace(file_id='pdf'))) == ('document', None, 'pdf')


def test_forward_circle_uses_telegram_send_video_note():
    bot = SimpleNamespace(send_video_note=AsyncMock())
    asyncio.run(forward_task_part(bot, -100100, 'video_note', None, 'circle_123'))
    bot.send_video_note.assert_awaited_once_with(-100100, video_note='circle_123')


def test_allowlist_includes_superadmin_with_unique_full_names():
    data = json.loads((Path(__file__).resolve().parents[1] / 'data' / 'attendee_whitelist.json').read_text('utf8'))
    names = data['names']
    assert data['count'] == 81 == len(names)
    assert 'Фильченкова Анастасия Сергеевна' in names
    assert len({rules.normalize_whitelist_name(x) for x in names}) == 81
    assert 'trip_attendees' in Base.metadata.tables
    assert TripAttendee.__tablename__ == 'trip_attendees'


def test_allowed_yo_equivalence_and_no_prefix_matching():
    assert rules.normalize_whitelist_name('  ПЁТР   ЁЛКИН ') == rules.normalize_whitelist_name('петр елкин')
    assert rules.normalize_whitelist_name('Ваня Иванов') != rules.normalize_whitelist_name('Ваня Иванов Ильич')


def test_lecturer_reveals_exactly_at_1210_msk():
    msk = ZoneInfo('Europe/Moscow')
    for d in (23,24,25):
        assert not rules.speaker_revealed(d, datetime(2026,11,d,12,9,59,tzinfo=msk))
        assert rules.speaker_revealed(d, datetime(2026,11,d,12,10,tzinfo=msk))
        assert d in rules.LECTURE_SPEAKERS and d in rules.LECTURE_TITLES


def test_hero_button_only_for_authorized_user():
    import pytest
    pytest.importorskip('aiogram')
    from app import keyboards
    with_hero = keyboards.menu(show_hero=True)
    without = keyboards.menu(show_hero=False)
    labels = lambda k: [row[0].text for row in k.keyboard]
    assert keyboards.HERO_LABEL in labels(with_hero)
    assert keyboards.HERO_LABEL not in labels(without)
    assert keyboards.LECTURES_LABEL in labels(without)
