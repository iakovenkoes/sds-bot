from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from app.rules import (
    can_register, visible_lecture_button, visible_mk_button, hero_stage_open,
    normalize_code_word, valid_full_name, show_lectures_menu, show_hero_menu,
)

TZ=ZoneInfo('Europe/Moscow')

def t(d,h,m=0):
    return datetime(2026,11,d,h,m,tzinfo=TZ)


def test_registrations_cut_off_at_1810():
    assert can_register(23,t(23,18,9))
    assert not can_register(23,t(23,18,10))
    assert can_register(25,t(23,18,10))


def test_button_timing():
    assert not visible_lecture_button(24,t(24,18,9))
    assert visible_lecture_button(24,t(24,18,10))
    assert not visible_mk_button(24,t(24,19,29))
    assert visible_mk_button(24,t(24,19,30))
    assert visible_lecture_button(24,t(24,20,59))
    assert not visible_lecture_button(24,t(24,21))


def test_hero_openings():
    assert hero_stage_open('Research',t(21,10))
    assert hero_stage_open('Practice',t(27,23,58))
    assert not hero_stage_open('Theory',t(27,23,59))
    assert not hero_stage_open('Action',t(28,9,59))
    assert hero_stage_open('Action',t(28,10))
    assert not hero_stage_open('Action',t(29,9))


def test_normalization_and_name():
    assert normalize_code_word('  КОКА КОЛА  ') == 'кока-кола'
    assert normalize_code_word('кока—кола') == 'кока-кола'
    assert normalize_code_word('Гидрофон')=='гидрофон'
    assert valid_full_name(' Иванова   Мария ')
    assert not valid_full_name('Мария')


def test_menu_end():
    assert show_lectures_menu(t(25,20,59))
    assert not show_lectures_menu(t(25,21))
    assert show_hero_menu(t(28,23))
    assert not show_hero_menu(t(29,9))


def test_hero_data_is_complete_and_public_only():
    from app.tasks import STAGES, TASKS
    assert list(STAGES) == ['Research','Practice','Theory','Action']
    assert len(TASKS) == 17
    assert sum(t['points'] for t in TASKS.values()) == 45
    assert all('Ответ:' not in t['text'] for t in TASKS.values())


def test_database_model_integrity():
    from sqlalchemy import create_engine
    from app.models import Base
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    assert {'users','user_roles','registrations','point_entries','submissions','submission_parts','planned_messages'} <= set(Base.metadata.tables)
