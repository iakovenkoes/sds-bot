import json
from pathlib import Path

from app import rules, tasks
from app.models import Base


def test_cards_exist():
    root = Path(__file__).resolve().parents[1] / 'data' / 'hero_cards'
    assert sorted(x.name for x in root.iterdir()) == ['archive_recon.png','assembly.png']
    assert all((root / n).stat().st_size > 5000 for n in ('archive_recon.png','assembly.png'))


def test_points_spelling():
    assert [rules.points_label(n) for n in (1,2,3,4,5,11,12,21,22,0.5)] == [
        '1 балл','2 балла','3 балла','4 балла','5 баллов','11 баллов',
        '12 баллов','21 балл','22 балла','0,5 балла']


def test_support_tables():
    assert {'support_cases','support_mirrors','bot_settings'} <= set(Base.metadata.tables)


def test_no_emoji_in_public_catalog():
    import regex
    contents = json.loads((Path(__file__).resolve().parents[1] / 'data' / 'messages_catalog.json').read_text('utf-8'))
    for message in contents['messages']:
        assert not regex.search(r'\p{Extended_Pictographic}', message['text'])


def test_new_public_stage_names():
    assert [tasks.stage_name(x) for x in ('Research','Practice','Theory','Action')] == [
        'Архив','Разведка','Сборка','Погружение']
