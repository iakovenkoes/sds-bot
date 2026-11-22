from datetime import datetime, timezone
from pathlib import Path
import json
import ast
from app import scheduler

BASE = Path(__file__).resolve().parents[1]


def test_new_admin_name_and_81_names():
    names=json.loads((BASE/'data/attendee_whitelist.json').read_text('utf8'))['names']
    assert names.count('Фильченкова Анастасия Сергеевна')==1
    assert len(names)==81


def test_all_nine_events_have_real_message_text():
    assert len(scheduler.EVENTS)==9
    for event in scheduler.EVENTS:
        body=scheduler.payload_for(event['id'])
        assert body and len(body)>50
        assert '«Тема лекции»' not in body
        assert '«Аудитория»' not in body
        assert '11:30' not in event['send_at']
    assert '346' in scheduler.payload_for('lecture_reminder_23')
    assert '346' in scheduler.payload_for('lecture_reminder_24')
    assert '246' in scheduler.payload_for('lecture_reminder_25')


def test_story_events_are_exact_moscow_times():
    required={'lore_1':'2026-11-22T14:00:00+03:00',
              'lore_2':'2026-11-24T13:30:00+03:00',
              'lore_3':'2026-11-27T16:00:00+03:00'}
    for item in scheduler.EVENTS:
        if item['id'] in required:
            assert item['send_at']==required[item['id']]


def test_event_workers_and_first_start_are_hooked():
    main=(BASE/'app/main.py').read_text('utf8')
    handlers=(BASE/'app/handlers.py').read_text('utf8')
    assert 'scheduler_worker(bot,db)' in main
    assert 'queue_late_join_story(s, user.id)' in handlers
    assert 'user_id == SETTINGS.super_admin_id or preview_enabled(user_id)' in handlers
    source=(BASE/'app/scheduler.py').read_text('utf8')
    assert 'on_conflict_do_nothing' in source
    assert "'skipped_late'" in source
    assert 'item.delivered_at = datetime.now(timezone.utc)' in source


def test_all_messages_have_valid_supported_html():
    from html.parser import HTMLParser
    class Tags(HTMLParser):
        def __init__(self): super().__init__(); self.stack=[]
        def handle_starttag(self, tag, attrs):
            assert tag in ('b','i','u')
            self.stack.append(tag)
        def handle_endtag(self, tag): assert self.stack.pop()==tag
    for e in scheduler.EVENTS:
        t=Tags(); t.feed(scheduler.payload_for(e['id'])); assert not t.stack
