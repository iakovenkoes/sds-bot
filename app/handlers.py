"""Telegram handlers: stage 1 participant core, submissions, and basic admin export."""
import asyncio
import io
import logging
from datetime import timedelta
from decimal import Decimal
from aiogram import F, Router, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, BufferedInputFile, FSInputFile, InputMediaPhoto
from sqlalchemy import select, func, delete
from sqlalchemy.exc import IntegrityError
import xlsxwriter
from pathlib import Path

from app.db import upsert_user
from app.media import classify_task_part, forward_task_part
from app.models import User, UserRole, Registration, PointEntry, Submission, SubmissionPart, PlannedMessage, BotSetting, SupportCase, SupportMirror, TripAttendee, UrgentBroadcast, utcnow
from app.messages import WELCOME, REGISTRATION, SUBMISSION_RECEIVED, STYLES
from app import keyboards as kb
from app import rules
from app.tasks import STAGES, TASKS, stage_name

log = logging.getLogger(__name__)

async def formatted_answer(message, text, **kwargs):
    """Preserve editor's Word bold/italic in Telegram without reformatting dynamic data."""
    if text in (WELCOME, REGISTRATION, SUBMISSION_RECEIVED):
        return await message.answer(text, parse_mode='HTML', **kwargs)
    styled = STYLES.get(text)
    if styled is not None:
        return await message.answer(styled, parse_mode='HTML', **kwargs)
    return await message.answer(text, **kwargs)

router = Router(name='atlantis')


class Input(StatesGroup):
    name = State()
    task = State()
    admin_find = State()
    admin_adjust = State()
    question = State()
    urgent_content = State()
    urgent_confirm = State()


# Configured by main.py when the bot starts.
DB = None
SETTINGS = None
# Local, per-superadmin override. This is intentionally not persisted across Docker restarts.
PARTICIPANT_VIEW = set()


def configure(db, settings):
    global DB, SETTINGS
    DB, SETTINGS = db, settings


async def _ensure(message: Message):
    async with DB.sessions() as s:
        await upsert_user(s, message.from_user)
        await s.commit()


async def role(telegram_id: int, requested: str) -> bool:
    if telegram_id == SETTINGS.super_admin_id:
        return True
    async with DB.sessions() as s:
        found = await s.get(UserRole, (telegram_id, requested))
        return found is not None


async def is_admin(telegram_id: int) -> bool:
    return await role(telegram_id, 'admin')


async def has_hero_access(user_id: int) -> bool:
    """Allowlisted trip person, bound once to Telegram ID; or isolated Super Admin preview."""
    if preview_enabled(user_id):
        return True
    async with DB.sessions() as session:
        user = await session.get(User, user_id)
        if not user or not user.full_name:
            return False
        name = rules.normalize_whitelist_name(user.full_name)
        attendee = await session.get(TripAttendee, name, with_for_update=True)
        if not attendee:
            return False
        # A claimed participant cannot change names to claim someone else's identity.
        existing = await session.scalar(select(TripAttendee).where(TripAttendee.telegram_id == user_id))
        if existing and existing.normalized_name != name:
            return False
        if attendee.telegram_id is None:
            attendee.telegram_id = user_id
            await session.commit()
            return True
        return attendee.telegram_id == user_id


async def registered(user_id: int, day: int) -> bool:
    async with DB.sessions() as s:
        r = await s.get(Registration, (user_id, day))
        return bool(r and r.active)


def preview_enabled(user_id: int) -> bool:
    """Development preview is restricted to the configured Super Admin only."""
    return bool(SETTINGS and SETTINGS.test_mode and user_id == SETTINGS.super_admin_id and user_id not in PARTICIPANT_VIEW)


def can_register_for(user_id: int, day: int) -> bool:
    # Preview permits pre-launch registration in the LOCAL database only.
    return rules.can_register(day) or (preview_enabled(user_id) and rules.current_msk() < rules.FIRST_DATE)


def stage_open_for(user_id: int, stage: str) -> bool:
    return rules.hero_stage_open(stage) or (preview_enabled(user_id) and rules.current_msk() < rules.ACTION_END)


async def display_menu(message: Message, actor=None):
    actor = actor or message.from_user
    await formatted_answer(message,'Главное меню ATLANTIS', reply_markup=kb.menu(show_hero=await has_hero_access(actor.id)))


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if message.chat.type != 'private':
        return
    await state.clear()
    async with DB.sessions() as s:
        user, first = await upsert_user(s, message.from_user)
        if first:
            # Background worker resumes this after restarts; unique (user, kind).
            s.add(PlannedMessage(user_id=user.id, kind='intro', due_at=utcnow()+timedelta(seconds=11)))
            user.intro_queued = True
        await s.commit()
    if first:
        await formatted_answer(message,WELCOME)
    await display_menu(message)


@router.message(Command('myid'))
async def my_id(message: Message):
    if message.chat.type != 'private':
        return
    await formatted_answer(message,f'Твой Telegram ID: {message.from_user.id}\nПередай этот ID главному администратору, если тебе нужны права администратора.')


@router.message(Command('menu'))
async def menu_command(message: Message, state: FSMContext):
    if message.chat.type != 'private':
        return
    await state.clear()
    await _ensure(message)
    await display_menu(message)


@router.message(Command('participant'))
async def participant_mode(message: Message, state: FSMContext):
    """Super Admin can compare with the real participant's current-date permissions."""
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    PARTICIPANT_VIEW.add(message.from_user.id)
    await state.clear()
    await formatted_answer(message,'Режим участника. Сейчас действуют обычные ограничения по датам. Чтобы снова увидеть предпросмотр — /preview; админ-панель — /admin.')
    await display_menu(message)


@router.message(Command('preview'))
async def preview_mode(message: Message, state: FSMContext):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    if not SETTINGS.test_mode:
        await formatted_answer(message,'Предпросмотр выключен в настройках. Он доступен только для локального тестирования.')
        return
    PARTICIPANT_VIEW.discard(message.from_user.id)
    await state.clear()
    await formatted_answer(message,'Предпросмотр включён: ты можешь открыть разделы заранее. Для панели администратора — /admin.')
    await display_menu(message)


# Register persistent reply-keyboard actions BEFORE FSM input handlers.
# Otherwise pressing a menu button while writing a draft would be saved as text.
@router.message(F.text.in_({kb.HERO_LABEL, kb.PROFILE_LABEL, kb.LECTURES_LABEL,
                            kb.QUESTION_LABEL}))
async def main_reply_keyboard(message: Message, state: FSMContext):
    """Handle the permanent big gray Telegram buttons, not callback buttons."""
    if message.chat.type != 'private':
        return
    await _ensure(message)
    if message.text == kb.HERO_LABEL:
        await show_hero(message, message.from_user, state)
    elif message.text == kb.PROFILE_LABEL:
        await state.clear()
        await show_profile(message, message.from_user)
    elif message.text == kb.LECTURES_LABEL:
        await show_lectures(message, message.from_user, state)
    elif message.text == kb.QUESTION_LABEL:
        await state.clear()
        await show_question(message, state)



@router.callback_query(F.data == 'menu')
async def menu_call(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await display_menu(call.message, call.from_user)


async def show_profile(message: Message, actor) -> None:
    async with DB.sessions() as s:
        user, _ = await upsert_user(s, actor)
        regs = (await s.scalars(select(Registration).where(Registration.user_id == user.id,
                                     Registration.active.is_(True)))).all()
        total = await s.scalar(select(func.coalesce(func.sum(PointEntry.delta), 0)).where(
            PointEntry.user_id == user.id))
        await s.commit()
    dates = ', '.join(f'{x.day}.11'for x in sorted(regs, key=lambda x: x.day)) or 'Нет записей'
    telegram = f'@{user.username}'if user.username else 'не указан'
    await formatted_answer(message,
        f'Мой профиль\nФИО: {user.full_name or "«ФИО не указано»"}\n'
        f'Telegram: {telegram}\nРегистрации: {dates}\nБаллы: {total}',
        reply_markup=kb.make([[('Изменить ФИО', 'name:profile')], [('Меню', 'menu')]])
    )


@router.callback_query(F.data == 'profile')
async def profile(call: CallbackQuery):
    await call.answer()
    await show_profile(call.message, call.from_user)


async def show_lectures(message: Message, actor, state: FSMContext) -> None:
    await state.clear()
    if not rules.show_lectures_menu() and not (rules.current_msk() < rules.FIRST_DATE):
        await formatted_answer(message,'Образовательная часть уже завершилась.', reply_markup=kb.menu())
        return
    if rules.current_msk() < rules.FIRST_DATE and not preview_enabled(actor.id):
        await formatted_answer(message,'Регистрация откроется 21 ноября. Расписание можно посмотреть уже сейчас.')
    if preview_enabled(actor.id):
        await formatted_answer(message,'Тестовый предпросмотр для главного администратора. '
                             'Записи и задания сохраняются в локальной базе.')
    await formatted_answer(message,REGISTRATION, reply_markup=kb.lectures())


@router.callback_query(F.data == 'lectures')
async def lectures(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await show_lectures(call.message, call.from_user, state)


@router.callback_query(F.data.startswith('lecture:'))
async def lecture_card(call: CallbackQuery):
    await call.answer()
    try:
        day = int(call.data.split(':')[1])
    except (ValueError,IndexError):
        return
    if day not in rules.LECTURE_DATES or rules.current_msk() >= rules.day_end(25):
        await formatted_answer(call.message,'Раздел уже недоступен.',reply_markup=kb.menu())
        return
    yes = await registered(call.from_user.id,day)
    label = rules.DIRECTIONS[day]
    now = rules.current_msk()
    revealed = rules.speaker_revealed(day, now) or preview_enabled(call.from_user.id)
    topic = rules.LECTURE_TITLES[day] if revealed else 'Тему лекции узнаете позже'
    speaker_line = f'\nСпикер: {rules.LECTURE_SPEAKERS[day]}' if revealed else '' 
    status = ('Вы записаны'if yes else 'Нет записи')
    if not can_register_for(call.from_user.id, day):
        status += '; регистрация ещё не открыта'if now < rules.FIRST_DATE else '; регистрация закрыта'
    await formatted_answer(call.message,
        f' {day} ноября — {label}\nЛекция: 18:10–19:30\n'
        f'Тема: {topic}{speaker_line}\nАудитория: {rules.LECTURE_ROOMS[day]}\n'
        f'Адрес: Кантемировская, 3к1\n'
        f'Мастер-класс: 19:30–21:00\nРегистрация: {status}',
        reply_markup=kb.lecture(day, yes, preview=preview_enabled(call.from_user.id))
    )


async def require_name(call: CallbackQuery, state: FSMContext, action: str) -> bool:
    async with DB.sessions() as s:
        u,_=await upsert_user(s,call.from_user)
        known=bool(u.full_name)
        await s.commit()
    if known:
        return False
    await state.update_data(continue_action=action)
    await state.set_state(Input.name)
    await formatted_answer(call.message,'Пожалуйста, напиши полное ФИО (фамилию, имя и отчество, если есть).')
    return True


@router.callback_query(F.data.startswith('name:'))
async def name_edit(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(continue_action='profile')
    await state.set_state(Input.name)
    await formatted_answer(call.message,'Напиши полное ФИО (фамилию, имя и отчество, если есть) одним сообщением.')


@router.message(Command('addadmin'))
async def add_admin_by_id(message: Message):
    """Only the configured superadmin can grant admin rights. No SQL edits needed."""
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    args = (message.text or '').split()
    if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
        await formatted_answer(message,'Формат: /addadmin 123456789. Человек должен предварительно открыть бота и нажать /start.')
        return
    user_id = int(args[1])
    if user_id == SETTINGS.super_admin_id:
        await formatted_answer(message,'Этот ID уже назначен главным администратором в настройках.')
        return
    async with DB.sessions() as s:
        target = await s.get(User, user_id)
        if not target:
            await formatted_answer(message,'Пользователь пока не найден в базе. Попроси его сначала отправить Майе /start.')
            return
        if await s.get(UserRole, (user_id, 'admin')):
            await formatted_answer(message,f'ID {user_id} уже является администратором.')
            return
        s.add(UserRole(user_id=user_id, role='admin'))
        await s.commit()
    await formatted_answer(message,f'Администратор назначен: {target.full_name or target.username or user_id} (ID {user_id}). Для входа — /admin.')


@router.message(Command('removeadmin'))
async def remove_admin_by_id(message: Message):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    args = (message.text or '').split()
    if len(args) != 2 or not args[1].isdigit():
        await formatted_answer(message,'Формат: /removeadmin 123456789')
        return
    user_id = int(args[1])
    if user_id == SETTINGS.super_admin_id:
        await formatted_answer(message,'Главного администратора нельзя снять этой командой.')
        return
    async with DB.sessions() as s:
        deleted = await s.execute(delete(UserRole).where(UserRole.user_id == user_id, UserRole.role == 'admin'))
        await s.commit()
    await formatted_answer(message,'Права администратора сняты.' if deleted.rowcount else 'Указанный ID не был обычным администратором.')


@router.message(Command('admins'))
async def list_admin_ids(message: Message):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    async with DB.sessions() as s:
        rows = (await s.execute(select(UserRole.user_id, User.full_name, User.username)
                   .join(User, User.id == UserRole.user_id)
                   .where(UserRole.role == 'admin').order_by(UserRole.user_id))).all()
    lines = [f'Главный администратор: {SETTINGS.super_admin_id}', f'Обычных администраторов: {len(rows)}']
    lines.extend(f'{uid} — {name or ("@" + uname if uname else "ФИО не указано")}' for uid, name, uname in rows)
    await formatted_answer(message,'\n'.join(lines))



@router.message(Command('cancel'))
async def cancel_action(message: Message, state: FSMContext):
    if message.chat.type != 'private':
        return
    await state.clear()
    await formatted_answer(message,'Действие отменено.')
    await display_menu(message)



@router.message(Command('admin'))
async def admin(message: Message, state: FSMContext):
    if message.chat.type != 'private' or not await is_admin(message.from_user.id):
        return
    await state.clear()
    if message.from_user.id == SETTINGS.super_admin_id:
        PARTICIPANT_VIEW.discard(message.from_user.id)
    await formatted_answer(message,
        'Панель администратора: выгрузка регистраций, поиск участников, корректировка баллов и срочная рассылка. '
        'Срочная рассылка доступна всем назначенным администраторам, отправка — после подтверждения. ' +
        ('Главный администратор: /addadmin ID, /removeadmin ID, /admins. ' if message.from_user.id == SETTINGS.super_admin_id else '') +
        ('Режим участника — /participant, предпросмотр — /preview.' if message.from_user.id == SETTINGS.super_admin_id else ''),
        reply_markup=kb.admin_menu(is_super=message.from_user.id == SETTINGS.super_admin_id)
    )



@router.message(Input.name)
async def name_receive(message: Message, state: FSMContext):
    if not message.text or not rules.valid_full_name(message.text):
        await formatted_answer(message,'Не совсем поняла. Напиши, пожалуйста, фамилию и имя одним сообщением.')
        return
    data = await state.get_data()
    async with DB.sessions() as s:
        u,_=await upsert_user(s,message.from_user)
        u.full_name=rules.normalize_full_name(message.text)
        await s.commit()
    await state.clear()
    await formatted_answer(message,'Спасибо! ФИО сохранила.')
    action = data.get('continue_action','profile')
    if action.startswith('reg:'):
        _,day,op = action.split(':')
        await change_registration(message, message.from_user, int(day), op)
    elif action.startswith('task:'):
        await open_task(message, message.from_user, state, action[5:])
    else:
        await display_menu(message)


async def change_registration(message: Message, actor, day: int, action: str):
    if day not in rules.LECTURE_DATES or not can_register_for(actor.id, day):
        await formatted_answer(message,'Регистрация и отмена записи на эту лекцию уже закрыты.')
        return
    async with DB.sessions() as s:
        u,_=await upsert_user(s,actor)
        r=await s.get(Registration,(u.id,day),with_for_update=True)
        if action=='add':
            if r and r.active:
                result='Ты уже записан(а) на эту лекцию.'
            elif r:
                r.active=True
                result=f'Ты успешно зарегистрирован(а) на {day} ноября!'
            else:
                s.add(Registration(user_id=u.id, day=day, active=True))
                result=f'Ты успешно зарегистрирован(а) на {day} ноября!'
        elif action=='cancel':
            if r and r.active:
                r.active=False
                result=f'Регистрация на {day} ноября отменена.'
            else:
                result='Активной регистрации на эту лекцию нет.'
        else:
            result='Неизвестное действие.'
        await s.commit()
    await formatted_answer(message,result, reply_markup=kb.registration_result())
    # Refresh permanent menu if the user's full name unlocked trip participation.
    if await has_hero_access(actor.id):
        await display_menu(message, actor)


@router.callback_query(F.data.startswith('reg:'))
async def registration_call(call: CallbackQuery, state: FSMContext):
    await call.answer()
    try:
        _,day_s,op=call.data.split(':')
        day=int(day_s)
    except (ValueError,IndexError):
        return
    if day not in rules.LECTURE_DATES or op not in ('add','cancel'):
        return
    if not can_register_for(call.from_user.id, day):
        await formatted_answer(call.message,'Регистрация на эту лекцию уже закрыта.')
        return
    if op=='add'and await require_name(call,state,call.data):
        return
    await change_registration(call.message, call.from_user, day,op)


async def show_hero(message: Message, actor, state: FSMContext) -> None:
    await state.clear()
    if not await has_hero_access(actor.id):
        await formatted_answer(message,'«Путь героя» доступен только участникам выезда. Укажи ФИО в профиле; если ты в списке и раздел не появился, напиши в «Вопрос Майе».')
        return
    now = rules.current_msk()
    if now >= rules.ACTION_END:
        await formatted_answer(message,'Приём заданий «Пути героя» уже закрыт.', reply_markup=kb.menu())
        return
    if now < rules.FIRST_DATE and not preview_enabled(actor.id):
        await formatted_answer(message,'Задания «Пути героя» откроются 21 ноября.', reply_markup=kb.stages())
        return
    if preview_enabled(actor.id):
        await formatted_answer(message,'Предпросмотр заданий для главного администратора.')
    # Two intact high-resolution PNG cards, delivered as one Telegram photo album.
    # Telegram can compress photos. An additional document button supplies originals.
    if now < rules.PRETRIP_END or preview_enabled(actor.id):
        cards = Path(__file__).resolve().parent.parent / 'data' / 'hero_cards'
        images = [cards / 'archive_recon.png', cards / 'assembly.png']
        if all(img.is_file() for img in images):
            await message.answer_media_group(media=[InputMediaPhoto(media=FSInputFile(img)) for img in images])
    await formatted_answer(message,'Путь героя\nВыбери блок. Погружение откроется в день выезда.',
                         reply_markup=kb.stages(preview=preview_enabled(actor.id)))


@router.callback_query(F.data == 'hero')
async def hero(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await show_hero(call.message, call.from_user, state)


@router.callback_query(F.data == 'hero:originals')
async def hero_originals(call: CallbackQuery):
    await call.answer()
    if not await has_hero_access(call.from_user.id):
        await formatted_answer(call.message,'Нет доступа.')
        return
    cards = Path(__file__).resolve().parent.parent / 'data' / 'hero_cards'
    for name, caption in [('archive_recon.png', 'Архив и Разведка'), ('assembly.png', 'Сборка')]:
        path = cards / name
        if path.is_file():
            await call.message.answer_document(FSInputFile(path), caption=f'{caption} — исходное качество')


@router.callback_query(F.data == 'locked:Action')
async def locked_action(call: CallbackQuery):
    await call.answer('Погружение откроется в день выезда.', show_alert=True)


@router.callback_query(F.data.startswith('stage:'))
async def stage(call:CallbackQuery):
    await call.answer()
    if not await has_hero_access(call.from_user.id):
        await formatted_answer(call.message,'Этот раздел доступен только участникам выезда.')
        return
    title=call.data[6:]
    if title not in STAGES or not stage_open_for(call.from_user.id, title):
        await formatted_answer(call.message,'Этот блок пока недоступен или уже закрыт.')
        return
    tasks = STAGES[title]['tasks']
    await formatted_answer(call.message,f'{stage_name(title)}. Выбери задание:', reply_markup=kb.task_list(title, tasks))


@router.callback_query(F.data.startswith('backstage:'))
async def back_stage(call:CallbackQuery):
    task=TASKS.get(call.data[10:])
    await call.answer()
    if not await has_hero_access(call.from_user.id):
        await formatted_answer(call.message,'Этот раздел доступен только участникам выезда.')
        return
    if task:
        await formatted_answer(call.message,f'Блок {stage_name(task["stage"])}:', reply_markup=kb.task_list(task['stage'],STAGES[task['stage']]['tasks']))


async def open_task(message:Message,actor,state:FSMContext,task_id:str):
    if not await has_hero_access(actor.id):
        await formatted_answer(message,'Задания доступны только подтверждённым участникам выезда.')
        return
    t=TASKS.get(task_id)
    if not t or not stage_open_for(actor.id, t['stage']):
        await formatted_answer(message,'Задание уже закрыто или пока не доступно.')
        return
    async with DB.sessions() as s:
        sub=(await s.scalar(select(Submission).where(Submission.user_id==actor.id,Submission.task_id==task_id)))
        if sub and sub.status!='draft':
            await formatted_answer(message,'Это задание уже отправлено на проверку. Повторная сдача невозможна.')
            return
        if not sub:
            sub=Submission(user_id=actor.id,task_id=task_id)
            s.add(sub)
            await s.commit()
    await state.set_state(Input.task)
    await state.update_data(task_id=task_id)
    await formatted_answer(message,f'Задание {t["number"]} · {stage_name(t["stage"])}\n{t["text"]}\nВес: {rules.points_label(t["points"])}\n\n'
                         'Отправь текст, фото, видео, видеокружочек или файл. Можно несколько сообщений. Когда всё готово — нажми «Сдать задание».',
                         reply_markup=kb.task_actions(task_id))


@router.callback_query(F.data.startswith('task:'))
async def task_open(call:CallbackQuery,state:FSMContext):
    await call.answer()
    task_id=call.data[5:]
    if task_id not in TASKS:
        return
    if await require_name(call,state,call.data):
        return
    # Callback.message.from_user is the bot, use call.from_user as participant identity.
    await open_task(call.message,call.from_user,state,task_id)


@router.message(Input.task)
async def task_part(message:Message,state:FSMContext):
    if message.text and message.text.startswith('/'):
        await formatted_answer(message,'Открой /menu, чтобы выйти из редактирования. Черновик сохранится.')
        return
    data=await state.get_data()
    task_id=data.get('task_id')
    t=TASKS.get(task_id)
    if not t or not stage_open_for(message.from_user.id, t['stage']) or not await has_hero_access(message.from_user.id):
        await state.clear()
        await formatted_answer(message,'Приём ответов закрыт. Черновик сохранён, но отправить его уже нельзя.')
        return
    kind, content, file_id = classify_task_part(message)
    if not kind:
        await formatted_answer(message,'Пришли текст, фотографию, видео, видеокружочек или файл.')
        return
    async with DB.sessions() as s:
        sub=await s.scalar(select(Submission).where(Submission.user_id==message.from_user.id,Submission.task_id==task_id).with_for_update())
        if not sub or sub.status!='draft':
            await formatted_answer(message,'Задание уже отправлено на проверку.')
            return
        s.add(SubmissionPart(submission_id=sub.id,content_type=kind,text=content,file_id=file_id))
        await s.commit()
    await formatted_answer(message,'Добавила к черновику. Можешь прислать ещё материалы или нажать «Сдать задание».',reply_markup=kb.task_actions(task_id))


@router.callback_query(F.data.startswith('submit:'))
async def submit(call:CallbackQuery,state:FSMContext,bot:Bot):
    await call.answer()
    task_id=call.data[7:]
    t=TASKS.get(task_id)
    if not t or not stage_open_for(call.from_user.id, t['stage']) or not await has_hero_access(call.from_user.id):
        await formatted_answer(call.message,'Дедлайн уже наступил — отправить черновик нельзя.')
        return
    async with DB.sessions() as s:
        sub=await s.scalar(select(Submission).where(Submission.user_id==call.from_user.id,Submission.task_id==task_id).with_for_update())
        if not sub or sub.status!='draft':
            await formatted_answer(call.message,'Задание уже отправлено или черновик не найден.')
            return
        parts=(await s.scalars(select(SubmissionPart).where(SubmissionPart.submission_id==sub.id))).all()
        if not parts:
            await formatted_answer(call.message,'Сначала добавь хотя бы одно сообщение или вложение.')
            return
        sub.status='pending'
        sub.sent_at=utcnow()
        sub_id=sub.id
        copies=[(x.content_type,x.text,x.file_id) for x in parts]
        await s.commit()
    await state.clear()
    await formatted_answer(call.message,SUBMISSION_RECEIVED)
    # If the organizer chat is not configured yet, the DB submission remains pending.
    if SETTINGS.review_chat_id:
        try:
            await bot.send_message(SETTINGS.review_chat_id,f'Работа №{sub_id}\nУчастник ID: {call.from_user.id}\nЗадание: {task_id}\nБаллы: {t["points"]}',
                reply_markup=kb.make([[('Принять',f'review:accept:{sub_id}'),('Отклонить',f'review:reject:{sub_id}')]]))
            for kind, text, file_id in copies:
                await forward_task_part(bot, SETTINGS.review_chat_id, kind, text, file_id)
        except Exception:
            log.exception('Could not forward submission %s to review group',sub_id)


@router.callback_query(F.data.startswith('review:'))
async def review(call:CallbackQuery):
    if not SETTINGS.review_chat_id or call.message.chat.id != SETTINGS.review_chat_id or not await role(call.from_user.id,'reviewer'):
        await call.answer('Нет доступа',show_alert=True)
        return
    try:
        _,action,sid=call.data.split(':')
        sub_id=int(sid)
    except (ValueError,IndexError):
        return
    if action not in ('accept','reject'):
        return
    async with DB.sessions() as s:
        sub=await s.scalar(select(Submission).where(Submission.id==sub_id).with_for_update())
        if not sub or sub.status!='pending':
            await call.answer('Работа уже обработана',show_alert=True)
            return
        if action == 'accept' and not preview_enabled(sub.user_id):
            submitter = await s.get(User, sub.user_id)
            attendee = (await s.get(TripAttendee, rules.normalize_whitelist_name(submitter.full_name))
                        if submitter and submitter.full_name else None)
            if not attendee or attendee.telegram_id != sub.user_id:
                await call.answer('Нет подтверждённого допуска к «Пути героя»', show_alert=True)
                return
        sub.status='accepted'if action=='accept'else 'rejected'
        sub.reviewed_by=call.from_user.id
        sub.reviewed_at=utcnow()
        if action=='accept':
            value=Decimal(str(TASKS[sub.task_id]['points']))
            s.add(PointEntry(user_id=sub.user_id,delta=value,source_key=f'task:{sub.user_id}:{sub.task_id}',actor_id=call.from_user.id))
        await s.commit()
    await call.answer('Решение сохранено')
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply(f'Работа №{sub_id}: {"Принято"if action=="accept"else "Отклонено"} · {call.from_user.full_name}')


async def show_trip(message: Message) -> None:
    # One information panel only. The content will be provided by organizers.
    await formatted_answer(message,'Организационная информация')


@router.callback_query(F.data == 'trip')
async def trip(call: CallbackQuery):
    await call.answer()
    await show_trip(call.message)


@router.callback_query(F.data.startswith('trip:'))
async def obsolete_trip_buttons(call: CallbackQuery):
    # Older inline buttons may remain in Telegram chat history.
    await call.answer()
    await show_trip(call.message)


async def get_support_chat_id() -> int:
    async with DB.sessions() as s:
        setting = await s.get(BotSetting, 'support_chat_id')
    return int(setting.value) if setting else SETTINGS.support_chat_id


async def show_question(message: Message, state: FSMContext) -> None:
    if not await get_support_chat_id():
        await formatted_answer(message,'Приём вопросов пока не подключён. Организаторы сообщат, когда он заработает.')
        return
    await state.clear()
    await state.set_state(Input.question)
    await formatted_answer(message,
        'Напиши здесь свой вопрос. Можно отправить текст, фотографию, видео, видеокружочек или документ. '
        'Если нужно что-то дополнить, отправь ещё одно сообщение — оно попадёт в ту же переписку. '
        'Для выхода нажми /menu.'
    )


@router.callback_query(F.data == 'question')
async def question(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await show_question(call.message, state)


@router.message(Input.question)
async def participant_question(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != 'private':
        return
    if not (message.text or message.photo or message.video or message.video_note or message.document):
        await formatted_answer(message,'Пришли текст, фотографию, видео, видеокружочек или документ.')
        return
    chat_id = await get_support_chat_id()
    if not chat_id:
        await state.clear()
        await formatted_answer(message,'Приём вопросов временно не настроен. Попробуй позже.')
        return
    async with DB.sessions() as s:
        user, _ = await upsert_user(s, message.from_user)
        # Lock the participant for serialization of concurrent messages from one account.
        await s.execute(select(User).where(User.id == user.id).with_for_update())
        case = await s.scalar(select(SupportCase).where(
            SupportCase.user_id == user.id, SupportCase.status == 'open'
        ).order_by(SupportCase.id.desc()).limit(1).with_for_update())
        if not case:
            case = SupportCase(user_id=user.id, status='open')
            s.add(case)
            await s.flush()
        try:
            if not case.group_root_id:
                identity = f'@{user.username}'if user.username else 'не указан'
                header = (f'Новое обращение №{case.id}\n'
                          f'Участник: {user.full_name or "ФИО не указано"}\n'
                          f'Telegram: {identity}\nTelegram ID: {user.id}')
                root = await bot.send_message(chat_id, header, reply_markup=kb.support_actions(case.id))
                case.group_root_id = root.message_id
                s.add(SupportMirror(group_message_id=root.message_id, case_id=case.id))
            copied = await message.copy_to(chat_id=chat_id, reply_to_message_id=case.group_root_id)
            s.add(SupportMirror(group_message_id=copied.message_id, case_id=case.id))
            await s.commit()
        except Exception:
            log.exception('Failed to deliver support question for %s', user.id)
            await s.rollback()
            await formatted_answer(message,'Не удалось доставить вопрос организаторам. Попробуй ещё раз немного позже.')
            return
    await formatted_answer(message,'Твой вопрос отправлен! Мы ответим в ближайшее время.')


@router.message(Command('bind_support'))
async def bind_support(message: Message):
    """Super Admin binds this exact private staff group; no secrets or manual IDs."""
    if message.chat.type not in ('group', 'supergroup') or message.from_user.id != SETTINGS.super_admin_id:
        return
    async with DB.sessions() as s:
        setting = await s.get(BotSetting, 'support_chat_id')
        if setting:
            setting.value = str(message.chat.id)
        else:
            s.add(BotSetting(key='support_chat_id', value=str(message.chat.id)))
        await s.commit()
    await formatted_answer(message,'Эта группа подключена для вопросов участников. На сообщения от организаторов бот ответит участникам от имени Майи.')


@router.callback_query(F.data == 'admin:support')
async def admin_support_status(call: CallbackQuery):
    if call.from_user.id != SETTINGS.super_admin_id:
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    gid = await get_support_chat_id()
    await formatted_answer(call.message,
        ('Группа вопросов подключена. ID группы: '+str(gid)) if gid else
        'Группа вопросов не подключена. Создай закрытую Telegram-группу, добавь туда Майю и отправь в группе /bind_support со своего аккаунта.'
    )


@router.callback_query(F.data.startswith('support:'))
async def support_action(call: CallbackQuery, bot: Bot):
    gid = await get_support_chat_id()
    if not gid or call.message.chat.id != gid or not await role(call.from_user.id, 'support'):
        await call.answer('Нет доступа', show_alert=True)
        return
    try:
        _, action, raw_case = call.data.split(':', 2)
        case_id = int(raw_case)
    except (ValueError, IndexError):
        await call.answer('Неверная команда', show_alert=True)
        return
    async with DB.sessions() as s:
        case = await s.scalar(select(SupportCase).where(SupportCase.id == case_id).with_for_update())
        if not case or case.status != 'open':
            await call.answer('Обращение уже закрыто', show_alert=True)
            return
        if action == 'close':
            case.status = 'closed'
            case.closed_at = utcnow()
            await s.commit()
        elif action != 'reply':
            await call.answer('Неверная команда', show_alert=True)
            return
    await call.answer()
    if action == 'close':
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f'Обращение №{case_id} закрыто.')
    else:
        prompt = await call.message.reply(
            f'Ответь через Reply на это сообщение для обращения №{case_id}. '
            'Майя доставит ответ участнику.'
        )
        async with DB.sessions() as s:
            s.add(SupportMirror(group_message_id=prompt.message_id, case_id=case_id))
            await s.commit()


@router.message(F.chat.type.in_({'group', 'supergroup'}))
async def support_group_reply(message: Message, bot: Bot):
    gid = await get_support_chat_id()
    if not gid or message.chat.id != gid or not message.from_user or message.from_user.is_bot:
        return
    if not await role(message.from_user.id, 'support'):
        return
    if not message.reply_to_message or not (message.text or message.photo or message.video or message.video_note or message.document):
        return
    async with DB.sessions() as s:
        mirror = await s.get(SupportMirror, message.reply_to_message.message_id)
        if not mirror:
            return
        case = await s.get(SupportCase, mirror.case_id)
        if not case or case.status != 'open':
            await message.reply('Обращение уже закрыто, ответ не отправлен.')
            return
        user_id, case_id = case.user_id, case.id
    try:
        if message.text:
            await bot.send_message(user_id, 'Ответ на твой вопрос:\n\n'+message.text)
        else:
            await bot.send_message(user_id, 'Ответ на твой вопрос:')
            await message.copy_to(chat_id=user_id)
    except Exception:
        log.exception('Support reply to %s failed', user_id)
        await message.reply('Не удалось доставить ответ участнику. Проверь, не заблокировал ли он бота.')
        return
    async with DB.sessions() as s:
        s.add(SupportMirror(group_message_id=message.message_id, case_id=case_id))
        await s.commit()
    await message.reply('Ответ доставлен участнику от имени Майи.')


@router.callback_query(F.data.startswith('code:') | F.data.startswith('ticket:'))
async def code_or_ticket(call:CallbackQuery):
    await call.answer()
    await formatted_answer(call.message,'Этот раздел подключим после настройки администраторских окон кодовых слов и счастливых билетов. Сейчас ввод недоступен.')


@router.callback_query(F.data == 'admin:urgent')
async def urgent_begin(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.set_state(Input.urgent_content)
    await formatted_answer(call.message,'Срочная рассылка всем пользователям бота. Пришли ОДНО сообщение: текст, фотографию, видео, видеокружочек или документ. Форматирование сохранится. Для отмены — /cancel.')


@router.message(Input.urgent_content)
async def urgent_content(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id) or message.chat.type != 'private':
        await state.clear()
        return
    if not (message.text or message.photo or message.video or message.video_note or message.document):
        await formatted_answer(message,'Пришли текст, фото, видео, кружочек или документ. Для отмены — /cancel.')
        return
    await state.update_data(urgent_message_id=message.message_id, urgent_chat_id=message.chat.id)
    await state.set_state(Input.urgent_confirm)
    await formatted_answer(message,'Предпросмотр рассылки:')
    await message.copy_to(message.chat.id)
    async with DB.sessions() as s:
        total = await s.scalar(select(func.count(User.id)))
    await formatted_answer(message,f'Получатели: до {total} пользователей, которые запускали бота. Отправить прямо сейчас?',
                         reply_markup=kb.make([[('Отправить всем', 'urgent:send')], [('Отмена', 'urgent:cancel')]]))


@router.callback_query(F.data.startswith('urgent:'))
async def urgent_confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_admin(call.from_user.id) or call.message.chat.type != 'private':
        await call.answer('Нет доступа', show_alert=True)
        return
    action = call.data.split(':', 1)[1]
    if await state.get_state() != Input.urgent_confirm.state:
        await call.answer('Рассылка уже обработана или отменена.', show_alert=True)
        return
    data = await state.get_data()
    await state.clear()  # Prevent a second click from sending a duplicate broadcast.
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    if action != 'send':
        await formatted_answer(call.message,'Рассылка отменена.')
        return
    # Persist the audit record and progress even if the bot is interrupted.
    async with DB.sessions() as s:
        record = UrgentBroadcast(actor_id=call.from_user.id, source_chat_id=data['urgent_chat_id'],
                                 source_message_id=data['urgent_message_id'], status='sending')
        s.add(record)
        await s.commit()
        record_id = record.id
    await formatted_answer(call.message,'Запустила срочную рассылку. Сообщение отправляется пользователям.')
    async with DB.sessions() as s:
        ids = (await s.scalars(select(User.id).order_by(User.id))).all()
    ok = failed = 0
    for user_id in ids:
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=data['urgent_chat_id'],
                                   message_id=data['urgent_message_id'])
            ok += 1
        except Exception:
            failed += 1
            log.warning('Urgent broadcast %s: could not deliver to user %s', record_id, user_id)
        await asyncio.sleep(0.08)
    async with DB.sessions() as s:
        record = await s.get(UrgentBroadcast, record_id)
        record.sent_count, record.failed_count, record.status = ok, failed, 'complete'
        await s.commit()
    notice = f'Срочная рассылка №{record_id} завершена. Доставлено: {ok}. Не доставлено: {failed}. Отправитель: {call.from_user.id}.'
    await formatted_answer(call.message,notice)
    if call.from_user.id != SETTINGS.super_admin_id:
        try:
            await bot.send_message(SETTINGS.super_admin_id, notice)
        except Exception:
            log.exception('Could not notify superadmin about urgent broadcast')


@router.callback_query(F.data == 'admin:export')
async def admin_exports(call:CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа',show_alert=True)
        return
    await call.answer()
    await formatted_answer(call.message,'Выбери лекцию для выгрузки:',reply_markup=kb.make([
        [(f' {day}.11 — {rules.DIRECTIONS[day]}',f'export:{day}')] for day in rules.LECTURE_DATES]))


@router.callback_query(F.data.startswith('export:'))
async def admin_export(call:CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа',show_alert=True)
        return
    await call.answer()
    try:
        day=int(call.data.split(':')[1])
    except (ValueError,IndexError):
        return
    if day not in rules.LECTURE_DATES:
        return
    async with DB.sessions() as s:
        rows=(await s.execute(select(User.full_name,User.username,User.id)
                .join(Registration,Registration.user_id==User.id)
                .where(Registration.day==day,Registration.active.is_(True))
                .order_by(User.full_name,User.id))).all()
    stream=io.BytesIO()
    book=xlsxwriter.Workbook(stream,{'in_memory':True})
    sheet=book.add_worksheet(f'{day} ноября')
    h=book.add_format({'bold':True,'bg_color':'#E2EFE9','border':1})
    for c,value in enumerate(('ФИО','Telegram','Telegram ID','Дата лекции','Направление')):
        sheet.write(0,c,value,h)
    sheet.set_column('A:A',36)
    sheet.set_column('B:B',25)
    sheet.set_column('C:C',19)
    sheet.set_column('D:E',18)
    for i,(name,username,tg_id) in enumerate(rows,1):
        sheet.write(i,0,name or '')
        sheet.write(i,1,('@'+username) if username else '')
        sheet.write_string(i,2,str(tg_id))
        sheet.write(i,3,f'{day}.11.2026')
        sheet.write(i,4,rules.DIRECTIONS[day])
    sheet.autofilter(0,0,max(len(rows),1),4)
    sheet.freeze_panes(1,0)
    book.close()
    data=BufferedInputFile(stream.getvalue(),filename=f'registrations_{day}_11_2026.xlsx')
    await call.message.answer_document(data,caption=f'Регистрации на {day} ноября: {len(rows)} участник(ов).')


@router.callback_query(F.data == 'admin:find')
async def admin_find(call:CallbackQuery,state:FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа',show_alert=True)
        return
    await call.answer()
    await state.set_state(Input.admin_find)
    await formatted_answer(call.message,'Введи ФИО, @username или числовой Telegram ID для поиска.')


@router.message(Input.admin_find)
async def admin_search_result(message:Message,state:FSMContext):
    if not await is_admin(message.from_user.id):
        return
    q=(message.text or '').strip()
    if not q:
        return
    async with DB.sessions() as s:
        if q.isdigit():
            users=[u for u in [await s.get(User,int(q))] if u]
        else:
            from sqlalchemy import or_
            users=(await s.scalars(select(User).where(or_(User.full_name.ilike('%'+q+'%'),User.username.ilike('%'+q.lstrip('@')+'%'))).limit(10))).all()
    await state.clear()
    if not users:
        await formatted_answer(message,'Участников не нашла. Попробуй другой запрос.')
    for u in users:
        await formatted_answer(message,f' {u.full_name or "ФИО не указано"}\n@{u.username or "нет"}\nTelegram ID: {u.id}',
                             reply_markup=kb.make([[(' /  Изменить баллы',f'adjust:{u.id}')]]))


@router.callback_query(F.data.startswith('adjust:'))
async def admin_adjust_begin(call:CallbackQuery,state:FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа',show_alert=True)
        return
    await call.answer()
    target=int(call.data.split(':')[1])
    await state.update_data(target=target)
    await state.set_state(Input.admin_adjust)
    await formatted_answer(call.message,f'Укажи корректировку баллов для ID {target}, например +1 или -0.5. Причина не нужна.')


@router.message(Input.admin_adjust)
async def admin_adjust_save(message:Message,state:FSMContext):
    if not await is_admin(message.from_user.id):
        return
    raw=(message.text or '').replace(',','.').strip()
    try:
        value=Decimal(raw)
        if not value.is_finite() or value==0 or value.as_tuple().exponent < -1 or abs(value)>1000:
            raise ValueError()
    except Exception:
        await formatted_answer(message,'Введи число с шагом 0,5, например +1, -0.5 или +1.5.')
        return
    if value*2 != int(value*2):
        await formatted_answer(message,'Можно указать только сумму с шагом 0,5.')
        return
    data=await state.get_data()
    target=data['target']
    async with DB.sessions() as s:
        if not await s.get(User,target):
            await formatted_answer(message,'Пользователь не найден.')
            return
        from uuid import uuid4
        s.add(PointEntry(user_id=target,delta=value,source_key='admin:'+uuid4().hex,actor_id=message.from_user.id))
        await s.commit()
    await state.clear()
    await formatted_answer(message,f'Готово. Баланс участника {target} изменён на {"+" if value > 0 else "−"}{rules.points_label(abs(value))}.')


@router.message()
async def fallback(message:Message):
    if message.chat.type != 'private':
        return
    await formatted_answer(message,'Ой-ой, простите, я немного запуталась в бумагах и не совсем поняла ваше сообщение. Воспользуйтесь кнопками или откройте /menu.')
    await display_menu(message)
