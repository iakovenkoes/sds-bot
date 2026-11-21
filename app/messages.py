"""Майя: text shared with the organizer-approved review catalogue.

No names of fictional characters other than Maya; real speaker names are allowed.
Source: data/messages_catalog.json (the organizer-facing review register).
"""
import json
from pathlib import Path

_CATALOG_PATH = Path(__file__).resolve().parent.parent / 'data' / 'messages_catalog.json'
CATALOG = json.loads(_CATALOG_PATH.read_text(encoding='utf-8'))
TEXTS = {item['id']: item['text'] for item in CATALOG['messages']}
HTML_TEXTS = {item['id']: item.get('html', item['text']) for item in CATALOG['messages']}
STYLES = json.loads((_CATALOG_PATH.parent / 'message_styles.json').read_text(encoding='utf-8'))
WELCOME = HTML_TEXTS['S01']
INSTRUCTION = HTML_TEXTS['S02']
REGISTRATION = HTML_TEXTS['S03']
SUBMISSION_RECEIVED = HTML_TEXTS['U26']
TRIP_INTRO = TEXTS['U33']
