"""Static regression checks for targeted emergency broadcasts; no Telegram token needed."""
import ast
from pathlib import Path

SOURCE = (Path(__file__).parents[1] / 'app' / 'handlers.py').read_text('utf-8')
MODULE = ast.parse(SOURCE)
FUNCTIONS = {n.name: n for n in MODULE.body if isinstance(n, ast.AsyncFunctionDef)}
A = next(n for n in MODULE.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'URGENT_AUDIENCES' for t in n.targets))
AUDIENCES = ast.literal_eval(A.value)


def test_expected_audiences():
    assert set(AUDIENCES) == {'all', 'trip', 'lectures', 'lecture23', 'lecture24', 'lecture25'}
    assert all(AUDIENCES.values())


def test_all_broadcast_stages_present():
    for name in ('urgent_recipient_ids', 'urgent_begin', 'urgent_select_audience', 'urgent_content', 'urgent_confirm'):
        assert name in FUNCTIONS
    assert 'urgent_audience = State()' in SOURCE
    assert "Input.urgent_audience.state" in SOURCE
    assert "Input.urgent_confirm.state" in SOURCE


def test_recipients_are_filtered_in_db():
    node = FUNCTIONS['urgent_recipient_ids']
    body = ast.get_source_segment(SOURCE, node)
    assert "TripAttendee.telegram_id == User.id" in body
    assert 'Registration.active.is_(True)' in body
    assert 'Registration.day == day' in body
    assert 'sorted(set(' in body


def test_no_broadcast_without_confirm():
    begin = ast.get_source_segment(SOURCE, FUNCTIONS['urgent_begin'])
    select_audience = ast.get_source_segment(SOURCE, FUNCTIONS['urgent_select_audience'])
    content = ast.get_source_segment(SOURCE, FUNCTIONS['urgent_content'])
    confirm = ast.get_source_segment(SOURCE, FUNCTIONS['urgent_confirm'])
    assert "'urgent:send'" in content
    assert 'await state.set_state(Input.urgent_confirm)' in content
    assert "action != 'send' or current_state != Input.urgent_confirm.state" in confirm
    assert 'await state.clear()' in confirm
    assert "'urgent:cancel'" in begin
    assert 'await state.set_state(Input.urgent_content)' in select_audience
    assert 'await bot.copy_message(' in confirm


def test_existing_sql_schema_unchanged():
    assert "session.add(BotSetting(key=f'urgent_audience:{record_id}', value=audience))" in SOURCE
    assert 'class UrgentBroadcast' not in SOURCE  # ORM remains in models.py
