"""Regression checks: show entry point to everyone, gate content and submissions."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parents[1] / 'app'


def test_public_menu_does_not_call_hero_permission_check():
    handlers = (SRC/'handlers.py').read_text(encoding='utf-8')
    assert "reply_markup=kb.menu())" in handlers
    assert 'kb.menu(show_hero=await has_hero_access(actor.id))' not in handlers


def test_permission_stays_on_hero_entry_and_submissions():
    handlers = (SRC/'handlers.py').read_text(encoding='utf-8')
    assert "if not await has_hero_access(actor.id):" in handlers
    assert "if not await has_hero_access(call.from_user.id):" in handlers
    assert "not await has_hero_access(message.from_user.id)" in handlers
    assert "not await has_hero_access(call.from_user.id)" in handlers


def test_non_attendees_see_explanation_and_profile_link():
    handlers = (SRC/'handlers.py').read_text(encoding='utf-8')
    assert '«Путь героя» — задания для участников выезда' in handlers
    assert "('Мой профиль', 'profile')" in handlers
    assert "('Главное меню', 'menu')" in handlers


def test_hero_button_visible_even_when_no_identity_verified():
    import pytest
    pytest.importorskip('aiogram')
    from app import keyboards
    now = datetime(2026, 11, 21, 19, 0, tzinfo=ZoneInfo('Europe/Moscow'))
    menu = keyboards.menu(now=now, show_hero=False)
    assert 'Путь героя' in [button.text for row in menu.keyboard for button in row]
