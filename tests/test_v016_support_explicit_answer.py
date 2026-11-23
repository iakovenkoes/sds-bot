"""Regression: internal staff Reply must never be forwarded to a participant."""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'app' / 'handlers.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'))


def test_support_delivery_requires_explicit_answer_command():
    fn = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'support_group_reply')
    commands = [node for decorator in fn.decorator_list for node in ast.walk(decorator) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Command']
    assert len(commands) == 1
    assert [arg.value for arg in commands[0].args if isinstance(arg, ast.Constant)] == ['answer']
    body = ast.get_source_segment(SOURCE.read_text(encoding='utf-8'), fn)
    assert "answer_text = (message.text or '').partition(' ')[2].strip()" in body
    assert "delivered = await bot.send_message(user_id, 'Ответ на твой вопрос:" in body


def test_existing_edit_and_correction_commands_remain():
    names = {n.name for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)}
    assert {'edit_support_reply', 'correct_support_reply', 'support_group_reply'} <= names
