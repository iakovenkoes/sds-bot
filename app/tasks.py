"""Public tasks taken verbatim from the organizer-supplied source JSON."""
import json
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / 'data' / 'hero_tasks.json'
DATA = json.loads(DATA_PATH.read_text(encoding='utf-8'))
STAGES = {stage['title']: stage for stage in DATA['stages']}
TASKS = {task['id']: {**task, 'stage': stage['title']} for stage in DATA['stages'] for task in stage['tasks']}
assert len(TASKS) == 17, 'Unexpected number of tasks: check organizer document'


# Display names only: keep stage titles and task IDs stable for already saved submissions.
STAGE_DISPLAY_NAMES = {stage["title"]: stage["display_title"] for stage in DATA["stages"]}

def stage_name(stage_id: str) -> str:
    return STAGE_DISPLAY_NAMES.get(stage_id, stage_id)
