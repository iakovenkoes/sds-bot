from aiogram.types import (InlineKeyboardButton as B, InlineKeyboardMarkup as K,
                           KeyboardButton, ReplyKeyboardMarkup)
from app.rules import (current_msk, can_register, show_hero_menu, show_lectures_menu,
                       visible_lecture_button, visible_mk_button, LECTURE_DATES, DIRECTIONS, hero_stage_open,
                       ACTION_END, day_end, FIRST_DATE)
from app.tasks import stage_name


def make(rows: list[list[tuple[str, str]]]) -> K:
    return K(inline_keyboard=[[B(text=text, callback_data=data) for text, data in row] for row in rows])


# These are permanent, Telegram-native bottom keyboard buttons (not inline links).
HERO_LABEL = 'Путь героя'
PROFILE_LABEL = 'Мой профиль'
LECTURES_LABEL = 'Лекции и мастер-классы'
TRIP_LABEL = 'Организационная информация'
QUESTION_LABEL = 'Вопрос Майе'


def menu(now=None, *, show_hero: bool = True) -> ReplyKeyboardMarkup:
    now = now or current_msk()
    # Show the Hero Path entry to everyone; actual stage and submission access is gated in handlers.
    # After the programme: finished sections disappear from new menus.
    labels = []
    if now < ACTION_END:
        labels.append(HERO_LABEL)
    labels.append(PROFILE_LABEL)
    if now < day_end(25):
        labels.append(LECTURES_LABEL)
    labels.append(QUESTION_LABEL)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label)] for label in labels],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder='Выберите раздел',
    )

def lectures() -> K:
    return make([[ (f'{day}.11 — {DIRECTIONS[day]}', f'lecture:{day}') ] for day in LECTURE_DATES] + [[('Меню', 'menu')]])


def lecture(day: int, registered: bool, now=None, preview: bool = False) -> K:
    now = now or current_msk()
    rows = []
    if can_register(day, now) or (preview and now < FIRST_DATE):
        rows.append([('Отменить регистрацию'if registered else 'Зарегистрироваться', f'reg:{day}:cancel'if registered else f'reg:{day}:add')])
    if visible_lecture_button(day, now):
        rows.append([('Кодовое слово лекции', f'code:{day}:lecture')])
        if registered:
            rows.append([('Счастливый билет', f'ticket:{day}')])
    if visible_mk_button(day, now):
        rows.append([('Кодовое слово мастер-класса', f'code:{day}:mk')])
    rows.append([('К лекциям', 'lectures')])
    rows.append([('Главное меню', 'menu')])
    return make(rows)


def registration_result() -> K:
    """Offer two direct exit routes after both registration and cancellation."""
    return make([[('К лекциям', 'lectures')], [('Главное меню', 'menu')]])


def stages(now=None, preview: bool = False) -> K:
    now = now or current_msk()
    rows = [[(stage_name(s), f'stage:{s}')] for s in ('Research', 'Practice', 'Theory')
            if hero_stage_open(s, now) or (preview and now < ACTION_END)]
    if hero_stage_open('Action', now) or (preview and now < ACTION_END):
        rows.append([(stage_name('Action'), 'stage:Action')])
    elif now < ACTION_END:
        rows.append([(f'{stage_name("Action")}', 'locked:Action')])
    rows.append([('Карточки без сжатия', 'hero:originals')])
    rows.append([('Меню', 'menu')])
    return make(rows)

def task_list(stage, tasks):
    from app.rules import points_label
    return make([[(f'Задание {t["number"]} — {points_label(t["points"])}', f'task:{t["id"]}')] for t in tasks] + [[('К блокам', 'hero')]])


def task_actions(task_id):
    return make([[('Сдать задание',f'submit:{task_id}')],[('К блоку',f'backstage:{task_id}')]])


def support_actions(case_id: int) -> K:
    return make([[('Ответить', f'support:reply:{case_id}'),
                  ('Закрыть вопрос', f'support:close:{case_id}')]])


def admin_menu(is_super: bool = False) -> K:
    rows = [[('Управление кодовыми словами', 'admin:codes')],
            [('Срочная рассылка', 'admin:urgent')],
            [('Выгрузить регистрации', 'admin:export')],
            [('Найти участника', 'admin:find')]]
    if is_super:
        rows.append([('Группа вопросов Майе', 'admin:support')])
    rows.append([('Меню участника', 'menu')])
    return make(rows)
