"""Regression checks for v0.7 (no Telegram network calls)."""
from pathlib import Path
import ast
from datetime import datetime
from zoneinfo import ZoneInfo
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def test_sources_parse():
    for file in (ROOT/'app').glob('*.py'):
        ast.parse(file.read_text('utf-8'), filename=str(file))


def test_two_intact_new_card_images():
    a, b = ROOT/'data/hero_cards/archive_recon.png', ROOT/'data/hero_cards/assembly.png'
    assert a.exists() and b.exists()
    assert Image.open(a).size == Image.open(b).size == (1448, 2048)
    assert not (ROOT/'data/hero_cards/research.jpg').exists()


def test_menu_and_lecture_changes():
    from app import rules
    assert rules.LECTURE_ROOMS == {23:'346',24:'346',25:'246'}
    assert rules.speaker_revealed(23, datetime(2026,11,23,12,10,tzinfo=ZoneInfo('Europe/Moscow')))
    k = (ROOT/'app/keyboards.py').read_text('utf-8')
    assert 'labels.append(QUESTION_LABEL)' in k
    assert 'labels.extend((TRIP_LABEL, QUESTION_LABEL))' not in k
    handlers = (ROOT/'app/handlers.py').read_text('utf-8')
    assert "'Тему лекции узнаете позже'" in handlers
    assert 'Кантемировская, 3к1' in handlers
    assert 'timedelta(seconds=11)' in handlers
    assert 'answer_media_group' in handlers


def test_admin_authorization_and_urgent_confirmation():
    h = (ROOT/'app/handlers.py').read_text('utf-8')
    for name in ('addadmin','removeadmin','admins'):
        assert f"Command('{name}')" in h
    assert "message.from_user.id != SETTINGS.super_admin_id" in h
    assert 'await is_admin(call.from_user.id)' in h
    assert "admin:urgent" in (ROOT/'app/keyboards.py').read_text('utf-8')
    assert "Input.urgent_confirm" in h
    assert "await state.clear()  # Prevent a second click" in h
    assert 'class UrgentBroadcast' in (ROOT/'app/models.py').read_text('utf-8')


def test_schedule_not_enabled_without_review():
    import json
    data = json.loads((ROOT/'data/scheduled_messages_agreed.json').read_text('utf-8'))
    assert data['implementation'].startswith('NOT_YET_ACTIVE')
