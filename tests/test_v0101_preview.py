from pathlib import Path
import ast
ROOT = Path(__file__).resolve().parents[1]

def test_preview_available_to_superadmin_on_server():
    text = (ROOT / 'app/handlers.py').read_text('utf8')
    assert 'PREVIEW_USERS.add(message.from_user.id)' in text
    assert 'PREVIEW_USERS.discard(message.from_user.id)' in text
    assert 'SETTINGS.test_mode and user_id == SETTINGS.super_admin_id' not in text
    assert 'user_id in PREVIEW_USERS' in text
    ast.parse(text)

def test_schedule_and_support_reviews():
    main = (ROOT/'app/main.py').read_text('utf8')
    handlers = (ROOT/'app/handlers.py').read_text('utf8')
    assert 'scheduler_worker(bot,db)' in main
    assert "Command('bind_review')" in handlers
    assert "Command('bind_stage')" in handlers
    assert "Command('addreviewer')" in handlers
    assert "Command('schedule_status')" in handlers
    assert "return await is_admin(telegram_id) or await role(telegram_id, 'support')" in handlers
