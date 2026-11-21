import re
from app.messages import TEXTS, CATALOG, WELCOME, INSTRUCTION, REGISTRATION, SUBMISSION_RECEIVED


def test_message_catalog_complete():
    assert len(CATALOG['messages']) == 63
    assert len(TEXTS) == len(CATALOG['messages'])
    assert WELCOME == CATALOG['messages'][0]['html']
    assert INSTRUCTION == next(x['html'] for x in CATALOG['messages'] if x['id']=='S02')
    assert REGISTRATION == next(x['html'] for x in CATALOG['messages'] if x['id']=='S03')


def test_fictional_characters_removed():
    # Real invited lecturers are explicitly not covered by this constraint.
    forbidden = ('Шарлотт', 'Генрих', 'Бланк', 'Виктор Грей', 'Катрин',
                 'Маруся', 'Марина', 'Маргарита', 'Милана')
    for item in CATALOG['messages']:
        text = item['text'] + item['title']
        for name in forbidden:
            assert name.casefold() not in text.casefold(), (item['id'], name)


def test_submission_confirmation_is_not_verdict():
    assert 'учёным на проверку' in SUBMISSION_RECEIVED
    assert 'отклон' not in SUBMISSION_RECEIVED.lower()
    assert 'не засч' not in SUBMISSION_RECEIVED.lower()
    assert 'пересда' not in SUBMISSION_RECEIVED.lower()


def test_source_stages_kept():
    from app.tasks import STAGES
    assert list(STAGES)==['Research','Practice','Theory','Action']


def test_approved_stage_names_reach_catalog_without_changing_task_ids():
    from app.tasks import STAGE_DISPLAY_NAMES, TASKS
    assert STAGE_DISPLAY_NAMES == {'Research': 'Архив', 'Practice': 'Разведка', 'Theory': 'Сборка', 'Action': 'Погружение'}
    assert len(TASKS) == 17
    assert all('Пробуждение' not in x.get('text', '') for x in CATALOG['messages'])
