"""The agreed time windows and rules. Pure functions, easy to test."""
from datetime import datetime, time
from zoneinfo import ZoneInfo
import re
import unicodedata

MOSCOW = ZoneInfo('Europe/Moscow')
FIRST_DATE = datetime(2026, 11, 21, 0, 0, tzinfo=MOSCOW)
PRETRIP_END = datetime(2026, 11, 27, 23, 59, tzinfo=MOSCOW)
ACTION_START = datetime(2026, 11, 28, 10, 0, tzinfo=MOSCOW)
ACTION_END = datetime(2026, 11, 29, 9, 0, tzinfo=MOSCOW)
LECTURE_DATES = (23, 24, 25)
DIRECTIONS = {23: 'PR', 24: 'ER', 25: 'Партнёрка'}
LECTURE_ROOMS = {23: '346', 24: '346', 25: '246'}
LECTURE_SPEAKERS = {23: 'Валерия Бархатова', 24: 'Сергей Ковалёв', 25: 'Дмитрий Караневский'}
LECTURE_TITLES = {
    23: 'Как заслужить доверие аудитории без крика и баннеров: опыт Самоката',
    24: 'Ваш лучший матчдэй. Почему вы до сих пор не были на «Газпром Арене»',
    25: 'Партнёрства в культуре: как соединить музыку, кино и городское пространство в одном проекте',
}



def current_msk() -> datetime:
    return datetime.now(MOSCOW)


def speaker_reveal_at(day: int) -> datetime:
    """Show the full theme and lecturer silently from 12:10 on that date."""
    return datetime(2026, 11, day, 12, 10, tzinfo=MOSCOW)


def speaker_revealed(day: int, now: datetime | None = None) -> bool:
    return (now or current_msk()).astimezone(MOSCOW) >= speaker_reveal_at(day)


def normalize_whitelist_name(value: str) -> str:
    """Casefold, whitespace and ё/е equivalence; no fuzzy matching between people."""
    return ' '.join(value.replace('Ё', 'Е').replace('ё','е').casefold().split())


def lecture_start(day: int) -> datetime:
    return datetime(2026, 11, day, 18, 10, tzinfo=MOSCOW)


def lecture_end(day: int) -> datetime:
    return datetime(2026, 11, day, 19, 30, tzinfo=MOSCOW)


def mk_start(day: int) -> datetime:
    return lecture_end(day)


def day_end(day: int) -> datetime:
    return datetime(2026, 11, day, 21, 0, tzinfo=MOSCOW)


def can_register(day: int, now: datetime | None = None) -> bool:
    now = (now or current_msk()).astimezone(MOSCOW)
    return FIRST_DATE <= now < lecture_start(day)


def visible_lecture_button(day: int, now: datetime | None = None) -> bool:
    now = (now or current_msk()).astimezone(MOSCOW)
    return lecture_start(day) <= now < day_end(day)


def visible_mk_button(day: int, now: datetime | None = None) -> bool:
    now = (now or current_msk()).astimezone(MOSCOW)
    return mk_start(day) <= now < day_end(day)


def hero_stage_open(stage_title: str, now: datetime | None = None) -> bool:
    now = (now or current_msk()).astimezone(MOSCOW)
    if stage_title == 'Action':
        return ACTION_START <= now < ACTION_END
    return FIRST_DATE <= now < PRETRIP_END


def show_hero_menu(now: datetime | None = None) -> bool:
    now = (now or current_msk()).astimezone(MOSCOW)
    return FIRST_DATE <= now < ACTION_END


def show_lectures_menu(now: datetime | None = None) -> bool:
    now = (now or current_msk()).astimezone(MOSCOW)
    return FIRST_DATE <= now < day_end(25)


def normalize_full_name(value: str) -> str:
    return ' '.join(value.split()).strip()


def valid_full_name(value: str) -> bool:
    value = normalize_full_name(value)
    return len(value) >= 5 and len(value) <= 180 and len(value.split()) >= 2


def normalize_code_word(value: str) -> str:
    """Normalize keyboard case/spaces and typography, including 'кока-кола'."""
    value = unicodedata.normalize('NFKC', value).casefold().strip()
    value = re.sub(r'[\u2010-\u2015\u2212]', '-', value)
    value = re.sub(r'\s*-\s*', '-', value)
    value = re.sub(r'\s+', ' ', value)
    if value in ('кока кола', 'кока-кола'):
        return 'кока-кола'
    return value


def points_label(value: int | float) -> str:
    """Correct Russian inflection: 1 балл, 2 балла, 5 баллов, 0,5 балла."""
    number = float(value)
    if not number.is_integer():
        return f'{str(value).replace(".", ",")} балла'
    n = int(number)
    ending = n % 100
    if 11 <= ending <= 14:
        word = 'баллов'
    elif n % 10 == 1:
        word = 'балл'
    elif n % 10 in (2, 3, 4):
        word = 'балла'
    else:
        word = 'баллов'
    return f'{n} {word}'
