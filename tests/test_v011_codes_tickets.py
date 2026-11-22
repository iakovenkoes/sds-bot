"""Offline checks for the lecture code and lucky ticket implementation."""
import ast
import json
from pathlib import Path

from app import rules
from app.models import Base

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = (ROOT / 'app/handlers.py').read_text('utf-8')
KEYBOARDS = (ROOT / 'app/keyboards.py').read_text('utf-8')
DATA = json.loads((ROOT / 'data/code_words_tickets.json').read_text('utf-8'))


def test_exact_six_code_words():
    assert DATA['code_words'] == {
        '23': {'lecture': 'МЕДУЗА', 'mk': 'АНАНАС'},
        '24': {'lecture': 'КОМЕТА', 'mk': 'ФЛАМИНГО'},
        '25': {'lecture': 'НОУТБУК', 'mk': 'КОКА-КОЛА'},
    }
    assert rules.normalize_code_word('  КОКА — КОЛА  ') == rules.normalize_code_word('КОКА-КОЛА')
    assert rules.normalize_code_word(' МеДуЗа  ') == 'медуза'


def test_ticket_codes_are_unique_and_grouped_by_day():
    tickets = DATA['tickets']
    assert set(tickets) == {'23', '24', '25'}
    assert all(len(v) == 5 for v in tickets.values())
    flat = [code for codes in tickets.values() for code in codes]
    assert len(flat) == len(set(flat)) == 15
    assert all(code.startswith('ATL'+day+'-') for day, vals in tickets.items() for code in vals)


def test_database_constraints():
    windows = Base.metadata.tables['code_windows']
    attempts = Base.metadata.tables['code_attempts']
    tickets = Base.metadata.tables['ticket_redemptions']
    assert tuple(windows.primary_key.columns.keys()) == ('day','kind')
    assert tuple(attempts.primary_key.columns.keys()) == ('user_id','day','kind')
    assert tuple(tickets.primary_key.columns.keys()) == ('code',)
    assert any(c.name == 'uq_ticket_user_day' for c in tickets.constraints)


def test_admin_and_participant_callbacks_are_distinct():
    module = ast.parse(HANDLERS)
    names = {node.name for node in module.body if isinstance(node, ast.AsyncFunctionDef)}
    assert {'admin_code_controls','admin_code_detail','admin_code_change',
            'code_enter','code_receive','ticket_enter','ticket_receive'} <= names
    assert 'not await is_admin(call.from_user.id)' in HANDLERS
    assert "('Управление кодовыми словами', 'admin:codes')" in KEYBOARDS
    assert 'Ваш ответ принят!' in HANDLERS
    assert 'on_conflict_do_nothing' in HANDLERS
    assert "source_key=f'code:{day}:{kind}:{message.from_user.id}'" in HANDLERS
    assert "source_key=f'ticket:{day}:{message.from_user.id}'" in HANDLERS


def test_no_old_placeholder_for_code_ticket():
    assert 'Этот раздел подключим после настройки администраторских окон' not in HANDLERS
