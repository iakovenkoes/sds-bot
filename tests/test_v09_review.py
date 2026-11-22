"""Offline checks for the v0.9 review permission/sync code (integration needs Telegram)."""
import ast
from pathlib import Path

from app.models import ReviewDelivery, Base
from app.media import classify_task_part

BASE = Path(__file__).resolve().parents[1]
HANDLERS = (BASE / 'app/handlers.py').read_text(encoding='utf-8')


def test_handlers_have_required_entrypoints():
    module = ast.parse(HANDLERS)
    names = {item.name for item in module.body if isinstance(item, ast.AsyncFunctionDef)}
    assert {'bind_review', 'bind_review_stage', 'review_status', 'review_sync',
            'add_reviewer', 'remove_reviewer', 'list_reviewers',
            'review', 'support_group_reply', 'review_override',
            'deliver_review_submission', 'can_support', 'can_review'} <= names


def test_only_one_review_callback_handler_and_forwarder():
    module = ast.parse(HANDLERS)
    assert sum(isinstance(x, ast.AsyncFunctionDef) and x.name == 'review' for x in module.body) == 1
    assert 'not await can_support(message.from_user.id)' in HANDLERS
    assert 'not await can_support(call.from_user.id)' in HANDLERS
    assert "not await can_review(call.from_user.id)" in HANDLERS


def test_review_delivery_model_and_independent_role():
    table = Base.metadata.tables['review_deliveries']
    assert ReviewDelivery.__tablename__ == 'review_deliveries'
    assert {'submission_id', 'chat_id', 'message_id', 'thread_id'} <= set(table.columns.keys())
    assert table.primary_key.columns.keys() == ['submission_id']
    assert "role='reviewer'" in HANDLERS
    assert "role == 'reviewer'" in HANDLERS


def test_review_uses_for_update_and_unique_point_source():
    assert 'with_for_update()' in HANDLERS
    assert "source_key=f'task:{sub.user_id}:{sub.task_id}'" in HANDLERS
    assert "message_thread_id=thread_id" in HANDLERS
    assert 'ReviewDelivery(submission_id=sub.id' in HANDLERS


def test_media_forwarder_supports_all_requested_types():
    from app import media
    import inspect
    source = inspect.getsource(media.forward_task_part)
    for kind in ('text', 'photo', 'video', 'video_note', 'document'):
        assert f"kind == '{kind}'" in source
    assert '**kwargs' in source
