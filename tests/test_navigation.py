"""User-interface regressions. Run in the Docker Python environment or after pip install."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

pytest.importorskip('aiogram')
from aiogram.types import ReplyKeyboardMarkup
from app import keyboards as kb

TZ = ZoneInfo('Europe/Moscow')

def msk(day, hour):
    return datetime(2026,11,day,hour,0,tzinfo=TZ)


def labels(reply_markup):
    return [b.text for row in reply_markup.keyboard for b in row]


def inline_labels(inline_markup):
    return [b.text for row in inline_markup.inline_keyboard for b in row]


def test_home_is_persistent_reply_keyboard_even_on_nov_20():
    menu = kb.menu(msk(21,18), show_hero=True)
    assert isinstance(menu, ReplyKeyboardMarkup)
    assert menu.is_persistent
    assert menu.resize_keyboard
    assert labels(menu) == [
        'Путь героя', 'Мой профиль', 'Лекции и мастер-классы',
        'Вопрос Майе'
    ]
    assert all(len(row)==1 for row in menu.keyboard)


def test_preview_displays_all_four_stages():
    view = inline_labels(kb.stages(msk(20,18), preview=True))
    assert 'Архив' in view and 'Разведка' in view and 'Сборка' in view
    assert 'Погружение' in view
    public = inline_labels(kb.stages(msk(23,18)))
    assert 'Архив' in public
    assert 'Погружение' in public


def test_preview_exposes_registration_without_changing_public_calendar():
    assert 'Зарегистрироваться' in inline_labels(kb.lecture(23,False,msk(20,18),preview=True))
    assert 'Зарегистрироваться' not in inline_labels(kb.lecture(23,False,msk(20,18),preview=False))
    assert 'Зарегистрироваться' in inline_labels(kb.lecture(23,False,msk(21,18),preview=False))
    assert 'Зарегистрироваться' not in inline_labels(kb.lecture(23,False,msk(23,19),preview=False))


def test_registration_success_and_cancel_offer_direct_home():
    actions = inline_labels(kb.registration_result())
    assert actions == ['К лекциям', 'Главное меню']
    assert 'Главное меню' in inline_labels(kb.lecture(23, False, msk(22, 18)))


def test_stage_buttons_keep_machine_ids_stable():
    stage_keyboard = kb.stages(msk(28, 10))
    options = [(b.text, b.callback_data) for row in stage_keyboard.inline_keyboard for b in row]
    assert ('Погружение', 'stage:Action') in options
    assert ('Архив', 'stage:Research') not in options
