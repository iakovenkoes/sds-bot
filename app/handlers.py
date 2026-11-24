"""Telegram handlers: stage 1 participant core, submissions, and basic admin export."""
import asyncio
import io
import json
import re
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
from app.scheduler import queue_late_join_story, EVENTS, event_time
from app.media import classify_task_part, forward_task_part
from app.models import User, UserRole, Registration, PointEntry, Submission, SubmissionPart, PlannedMessage, BotSetting, SupportCase, SupportMirror, SupportSentReply, TripAttendee, UrgentBroadcast, ReviewDelivery, CodeWindow, CodeAttempt, TicketRedemption, utcnow
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
    urgent_audience = State()
    urgent_content = State()
    urgent_confirm = State()
    code_word = State()
    ticket_code = State()


# Configured by main.py when the bot starts.
DB = None
SETTINGS = None
# Local, per-superadmin override. This is intentionally not persisted across Docker restarts.
PARTICIPANT_VIEW = set()
# Explicitly enabled by /preview; NEVER active automatically on the live server.
PREVIEW_USERS = set()


def configure(db, settings):
    global DB, SETTINGS
    DB, SETTINGS = db, settings
    PREVIEW_USERS.clear()
    PARTICIPANT_VIEW.clear()


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


async def can_support(telegram_id: int) -> bool:
    # Admins may answer support conversations without a separate support role.
    return await is_admin(telegram_id) or await role(telegram_id, 'support')


async def can_review(telegram_id: int) -> bool:
    # A reviewer is not an admin; the superadmin can intervene.
    return await role(telegram_id, 'reviewer')



async def has_hero_access(user_id: int) -> bool:
    """Allowlisted trip person, bound once to Telegram ID; or isolated Super Admin preview."""
    if user_id == SETTINGS.super_admin_id or preview_enabled(user_id):
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
    """On-demand preview only for the superadmin (works on production too)."""
    return bool(SETTINGS and user_id == SETTINGS.super_admin_id and user_id in PREVIEW_USERS and user_id not in PARTICIPANT_VIEW)


def can_register_for(user_id: int, day: int) -> bool:
    # Preview only bypasses calendar restrictions for the superadmin. On a live
    # database, any resulting changes are REAL; use a test account for simulations.
    return rules.can_register(day) or (preview_enabled(user_id) and rules.current_msk() < rules.FIRST_DATE)


def stage_open_for(user_id: int, stage: str) -> bool:
    return rules.hero_stage_open(stage) or (preview_enabled(user_id) and rules.current_msk() < rules.ACTION_END)


async def display_menu(message: Message, actor=None):
    actor = actor or message.from_user
    await formatted_answer(message,'Главное меню ATLANTIS', reply_markup=kb.menu())


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
            await queue_late_join_story(s, user.id)
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
    PREVIEW_USERS.discard(message.from_user.id)
    await state.clear()
    await formatted_answer(message,'Режим участника. Сейчас действуют обычные ограничения по датам. Чтобы снова увидеть предпросмотр — /preview; админ-панель — /admin.')
    await display_menu(message)


@router.message(Command('preview'))
async def preview_mode(message: Message, state: FSMContext):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    PARTICIPANT_VIEW.discard(message.from_user.id)
    PREVIEW_USERS.add(message.from_user.id)
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



@router.message(Command('schedule_status'))
async def schedule_status(message: Message):
    """Read-only operational view: only the designated super-administrator."""
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    lines = ['Планировщик Майи. Московское время.']
    async with DB.sessions() as s:
        for event in EVENTS:
            key = 'auto_v010_' + event['id']
            marker = await s.get(BotSetting, key)
            kind = 'auto:' + event['id']
            queued = await s.scalar(select(func.count(PlannedMessage.id)).where(PlannedMessage.kind == kind))
            delivered = await s.scalar(select(func.count(PlannedMessage.id)).where(
                PlannedMessage.kind == kind, PlannedMessage.delivered_at.is_not(None)))
            dt = event_time(event).astimezone(rules.MOSCOW)
            status = marker.value if marker else 'ожидает времени'
            lines.append(f"{dt:%d.%m %H:%M} — {event['id']}: {status}; отправлено {delivered}/{queued}")
    lines.append('Команда показывает очередь и доставки, но не гарантирует доставку будущих сообщений.')
    await formatted_answer(message, '\n'.join(lines))


@router.message(Command('admin'))
async def admin(message: Message, state: FSMContext):
    if message.chat.type != 'private' or not await is_admin(message.from_user.id):
        return
    await state.clear()
    if message.from_user.id == SETTINGS.super_admin_id:
        PARTICIPANT_VIEW.discard(message.from_user.id)
        PREVIEW_USERS.discard(message.from_user.id)
    await formatted_answer(message,
        'Панель администратора: выгрузка регистраций, поиск участников, корректировка баллов и срочная рассылка. '
        'Срочная рассылка доступна всем назначенным администраторам, отправка — после подтверждения. ' +
        ('Главный администратор: /addadmin ID, /removeadmin ID, /admins. ' if message.from_user.id == SETTINGS.super_admin_id else '') +
        ('Режим участника — /participant, предпросмотр — /preview.' if message.from_user.id == SETTINGS.super_admin_id else ''),
        reply_markup=kb.admin_menu(is_super=message.from_user.id == SETTINGS.super_admin_id)
    )



@router.callback_query(F.data == 'admin:stats')
async def admin_stats(call: CallbackQuery):
    if call.message.chat.type != 'private' or not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    async with DB.sessions() as s:
        users = await s.scalar(select(func.count(User.id))) or 0
        named = await s.scalar(select(func.count(User.id)).where(
            User.full_name.is_not(None), func.length(func.trim(User.full_name)) > 0)) or 0
        registered = await s.scalar(select(func.count(func.distinct(Registration.user_id))).where(
            Registration.active.is_(True))) or 0
        trip_total = await s.scalar(select(func.count(TripAttendee.normalized_name))) or 0
        trip_bound = await s.scalar(select(func.count(TripAttendee.normalized_name)).where(
            TripAttendee.telegram_id.is_not(None))) or 0
    await formatted_answer(call.message,
        'Статистика бота\n\n'
        f'Запустили бота (записаны в базе): {users}\n'
        f'Указали ФИО: {named}\n'
        f'Зарегистрированы хотя бы на одну лекцию: {registered}\n'
        f'В списке выезда: {trip_total}\n'
        f'Участники выезда, привязанные к Telegram: {trip_bound}\n\n'
        'Статистика рассчитана по текущей базе данных.',
        reply_markup=kb.make([[('Назад в админку', 'admin:stats:back')]]))



@router.callback_query(F.data == 'admin:stats:back')
async def admin_stats_back(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    await formatted_answer(call.message, 'Панель администратора',
        reply_markup=kb.admin_menu(is_super=call.from_user.id == SETTINGS.super_admin_id))


@router.callback_query(F.data == 'admin:points_export')
async def admin_points_export(call: CallbackQuery):
    """Read-only current balance export, including users with zero points."""
    if call.message.chat.type != 'private' or not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    async with DB.sessions() as session:
        totals = (await session.execute(
            select(User.full_name, User.username, User.id,
                   func.coalesce(func.sum(PointEntry.delta), 0).label('balance'))
            .outerjoin(PointEntry, PointEntry.user_id == User.id)
            .group_by(User.id, User.full_name, User.username)
            .order_by(func.coalesce(func.sum(PointEntry.delta), 0).desc(),
                      User.full_name, User.id)
        )).all()
    stream = io.BytesIO()
    book = xlsxwriter.Workbook(stream, {'in_memory': True})
    sheet = book.add_worksheet('Баллы участников')
    header = book.add_format({'bold': True, 'bg_color': '#E2EFE9', 'border': 1})
    number = book.add_format({'num_format': '0.0'})
    for column, label in enumerate(('ФИО', 'Telegram', 'Telegram ID', 'Всего баллов')):
        sheet.write(0, column, label, header)
    sheet.set_column('A:A', 39)
    sheet.set_column('B:B', 26)
    sheet.set_column('C:C', 22)
    sheet.set_column('D:D', 18)
    for row_num, (full_name, username, telegram_id, balance) in enumerate(totals, 1):
        sheet.write_string(row_num, 0, full_name or '')
        sheet.write_string(row_num, 1, ('@' + username) if username else '')
        sheet.write_string(row_num, 2, str(telegram_id))
        sheet.write_number(row_num, 3, float(balance), number)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(len(totals), 1), 3)
    book.close()
    filename = f'ATLANTIS_points_{rules.current_msk():%Y-%m-%d_%H-%M}.xlsx'
    await call.message.answer_document(
        BufferedInputFile(stream.getvalue(), filename=filename),
        caption=f'Баллы участников на текущий момент. Пользователей: {len(totals)}. '
                'Непроверенные задания «Пути героя» не начислены.'
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
    elif action.startswith('code:'):
        _, day_s, kind = action.split(':')
        day = int(day_s)
        async with DB.sessions() as session:
            open_now = await _window_is_open(session, day, kind)
        if open_now and (kind != 'lecture' or await registered(message.from_user.id, day)):
            await state.set_state(Input.code_word)
            await state.update_data(day=day, kind=kind)
            await formatted_answer(message, 'Введите кодовое слово, которое назвал спикер. Для отмены — /cancel.')
        else:
            await formatted_answer(message, 'Сейчас ввод кодового слова недоступен.')
    elif action.startswith('ticket:'):
        day = int(action.split(':')[1])
        if rules.visible_lecture_button(day) and await registered(message.from_user.id, day):
            await state.set_state(Input.ticket_code)
            await state.update_data(day=day)
            await formatted_answer(message, 'Введите уникальный код счастливого билета. Для отмены — /cancel.')
        else:
            await formatted_answer(message, 'Сейчас счастливые билеты недоступны.')
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
        await formatted_answer(message,'«Путь героя» — задания для участников выезда 28–29 ноября.\n\nЧтобы открыть задания и отправлять ответы, необходимо быть в списке участников выезда и указать своё ФИО в разделе «Мой профиль» → «Изменить ФИО».\n\nЕсли вы едете на выезд, но после ввода ФИО задания недоступны, напишите в «Вопрос Майе».', reply_markup=kb.make([[('Мой профиль', 'profile')], [('Лекции и мастер-классы', 'lectures')], [('Главное меню', 'menu')]]))
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
    cards = Path(__file__).resolve().parent.parent / 'data' / 'hero_cards'
    if not cards.is_dir():
        cards = Path(__file__).resolve().parent.parent / 'data' / 'hero_cards'
    # Never reveal the immersion card before 28 Nov 10:00 Moscow time,
    # including in the superadmin preview. Gate the image as well as the stage.
    if rules.ACTION_START <= now < rules.ACTION_END:
        immersion = cards / 'immersion.png'
        if immersion.is_file():
            await message.answer_photo(FSInputFile(immersion), caption='Погружение — карточка заданий')
    await formatted_answer(message,
        'Путь героя\nВыбери блок.' +
        (' Погружение откроется в день выезда.' if now < rules.ACTION_START else ''),
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
    # Keep submitted work pending even before the group/topics are configured.
    try:
        await deliver_review_submission(bot, sub_id)
    except Exception:
        log.exception('Failed to mirror submission %s; /review_sync can retry', sub_id)


# Review setup is stored in PostgreSQL, not in a developer's local .env.
REVIEW_STAGES = {'Research': 'Архив', 'Practice': 'Разведка',
                 'Theory': 'Сборка', 'Action': 'Погружение'}
REVIEW_ALIASES = {
    'архив': 'Research', 'archive': 'Research', 'research': 'Research',
    'разведка': 'Practice', 'recon': 'Practice', 'practice': 'Practice',
    'сборка': 'Theory', 'assembly': 'Theory', 'theory': 'Theory',
    'погружение': 'Action', 'action': 'Action', 'dive': 'Action',
}


async def review_destination(stage: str) -> tuple[int, int] | None:
    async with DB.sessions() as s:
        group = await s.get(BotSetting, 'review_chat_id')
        topic = await s.get(BotSetting, 'review_topic_' + stage)
    chat_id = int(group.value) if group else SETTINGS.review_chat_id
    if not chat_id or not topic:
        return None
    return chat_id, int(topic.value)


async def _review_group_id() -> int:
    async with DB.sessions() as s:
        row = await s.get(BotSetting, 'review_chat_id')
    return int(row.value) if row else SETTINGS.review_chat_id


async def deliver_review_submission(bot: Bot, submission_id: int) -> bool:
    """Send one pending work to the appropriate forum topic. Safe to retry."""
    async with DB.sessions() as s:
        sub = await s.get(Submission, submission_id)
        if not sub or sub.status != 'pending' or await s.get(ReviewDelivery, submission_id):
            return False
        task = TASKS.get(sub.task_id)
        if not task:
            log.error('Unknown task %s in submission %s', sub.task_id, submission_id)
            return False
        dest = await review_destination(task['stage'])
        if not dest:
            return False
        chat_id, thread_id = dest
        participant = await s.get(User, sub.user_id)
        parts = (await s.scalars(select(SubmissionPart).where(
            SubmissionPart.submission_id == sub.id).order_by(SubmissionPart.id))).all()
        identity = ('@' + participant.username) if participant and participant.username else 'нет username'
        full_name = participant.full_name if participant and participant.full_name else 'ФИО не указано'
        card = (f'Новая работа №{sub.id}\nБлок: {REVIEW_STAGES[task["stage"]]}\n'
                f'Задание: {task["number"]}\nУчастник: {full_name}\n'
                f'Telegram: {identity}\nTelegram ID: {sub.user_id}\n'
                f'Баллы: {rules.points_label(task["points"])}\n'
                'Материалы ответа ниже. Принять или отклонить можно один раз.')
        # A row lock serializes two concurrent syncs and a live participant submission.
        await s.execute(select(Submission).where(Submission.id == sub.id).with_for_update())
        if await s.get(ReviewDelivery, submission_id):
            return False
        root = await bot.send_message(chat_id, card, message_thread_id=thread_id,
                                      reply_markup=kb.make([[
                                          ('Принять', f'review:accept:{sub.id}'),
                                          ('Отклонить', f'review:reject:{sub.id}')]]))
        # Record immediately so a delivery can never be followed by a second card
        # after a later attachment failure (retry attachments requires manual care).
        s.add(ReviewDelivery(submission_id=sub.id, chat_id=chat_id,
                             message_id=root.message_id, thread_id=thread_id))
        await s.commit()
    # Telegram file IDs are reusable by this bot; keep originals in PostgreSQL.
    for part in parts:
        try:
            await forward_task_part(bot, chat_id, part.content_type, part.text, part.file_id,
                                    message_thread_id=thread_id, reply_to_message_id=root.message_id)
        except Exception:
            log.exception('Attachment %s could not be delivered for submission %s',
                          part.id, submission_id)
            await bot.send_message(chat_id,
                f'ВНИМАНИЕ: не удалось переслать материал №{part.id} работы №{sub.id}. '
                'Пока не принимайте решение; обратитесь к разработчику.',
                message_thread_id=thread_id, reply_to_message_id=root.message_id)
    return True


@router.message(Command('bind_review'))
async def bind_review(message: Message):
    if message.chat.type != 'supergroup' or not message.from_user or message.from_user.id != SETTINGS.super_admin_id:
        return
    async with DB.sessions() as s:
        setting = await s.get(BotSetting, 'review_chat_id')
        if setting:
            setting.value = str(message.chat.id)
        else:
            s.add(BotSetting(key='review_chat_id', value=str(message.chat.id)))
        await s.commit()
    await message.answer('Группа проверки подключена. Открой каждую из четырёх тем и отправь там '
                         '/bind_stage архив, /bind_stage разведка, /bind_stage сборка или '
                         '/bind_stage погружение. Затем отправь /review_sync в General.')


@router.message(Command('bind_stage'))
async def bind_review_stage(message: Message):
    if message.chat.type != 'supergroup' or not message.from_user or message.from_user.id != SETTINGS.super_admin_id:
        return
    if message.chat.id != await _review_group_id():
        await message.answer('Сначала привяжи эту группу командой /bind_review в General.')
        return
    stage = REVIEW_ALIASES.get((message.text or '').split(maxsplit=1)[-1].strip().lower())
    if not stage or (message.text or '').count(' ') == 0:
        await message.answer('Формат: /bind_stage архив (либо разведка, сборка, погружение).')
        return
    thread_id = message.message_thread_id
    if not thread_id or thread_id == 1:
        await message.answer('Открой отдельную тему нужного блока, а не General.')
        return
    async with DB.sessions() as s:
        row = await s.get(BotSetting, 'review_topic_' + stage)
        if row:
            row.value = str(thread_id)
        else:
            s.add(BotSetting(key='review_topic_' + stage, value=str(thread_id)))
        await s.commit()
    await message.answer(f'Тема «{REVIEW_STAGES[stage]}» подключена. ID темы: {thread_id}.')


@router.message(Command('review_status'))
async def review_status(message: Message):
    if message.from_user.id != SETTINGS.super_admin_id:
        return
    async with DB.sessions() as s:
        settings = {row.key: row.value for row in (await s.scalars(
            select(BotSetting).where(BotSetting.key.like('review_%')))).all()}
        undelivered = (await s.scalars(select(Submission).where(Submission.status == 'pending',
            ~Submission.id.in_(select(ReviewDelivery.submission_id))))).all()
    group = settings.get('review_chat_id') or str(SETTINGS.review_chat_id or 'не подключена')
    detail = '\n'.join(f'{name}: ' + ('подключена' if settings.get('review_topic_' + stage) else 'не подключена')
                        for stage, name in REVIEW_STAGES.items())
    await message.answer(f'Группа: {group}\n{detail}\nОжидают пересылки: {len(undelivered)}')


@router.message(Command('review_sync'))
async def review_sync(message: Message, bot: Bot):
    if message.chat.type != 'supergroup' or message.from_user.id != SETTINGS.super_admin_id:
        return
    if message.chat.id != await _review_group_id():
        return
    async with DB.sessions() as s:
        pending = (await s.scalars(select(Submission.id).where(
            Submission.status == 'pending',
            ~Submission.id.in_(select(ReviewDelivery.submission_id))).order_by(Submission.id).limit(100))).all()
    count = 0
    for sid in pending:
        try:
            count += bool(await deliver_review_submission(bot, sid))
        except Exception:
            log.exception('Failed to sync submission %s', sid)
    await message.answer(f'Передано работ: {count}. Если часть тем не привязана, '
                         'работы останутся в очереди. /review_status покажет состояние.')


@router.message(Command('addreviewer'))
async def add_reviewer(message: Message):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    args = (message.text or '').split()
    if len(args) != 2 or not args[1].isdigit() or int(args[1]) <= 0:
        await message.answer('Формат: /addreviewer 123456789. Проверяющий должен сначала отправить боту /start.')
        return
    uid = int(args[1])
    async with DB.sessions() as s:
        user = await s.get(User, uid)
        if not user:
            await message.answer('Пользователь ещё не зарегистрирован. Пусть отправит Майе /start.')
            return
        if await s.get(UserRole, (uid, 'reviewer')):
            await message.answer('Этот пользователь уже проверяющий.')
            return
        s.add(UserRole(user_id=uid, role='reviewer'))
        await s.commit()
    await message.answer(f'Проверяющий назначен: {uid}. Доступ к /admin не предоставлен.')


@router.message(Command('removereviewer'))
async def remove_reviewer(message: Message):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    args = (message.text or '').split()
    if len(args) != 2 or not args[1].isdigit():
        await message.answer('Формат: /removereviewer 123456789')
        return
    uid = int(args[1])
    async with DB.sessions() as s:
        deleted = await s.execute(delete(UserRole).where(UserRole.user_id == uid, UserRole.role == 'reviewer'))
        await s.commit()
    await message.answer('Роль проверяющего снята.' if deleted.rowcount else 'Проверяющий с таким ID не найден.')


@router.message(Command('reviewers'))
async def list_reviewers(message: Message):
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    async with DB.sessions() as s:
        rows = (await s.scalars(select(UserRole.user_id).where(
            UserRole.role == 'reviewer').order_by(UserRole.user_id))).all()
    await message.answer('Проверяющие (не администраторы):\n' +
                         ('\n'.join(map(str, rows)) if rows else 'пока нет'))


@router.callback_query(F.data.startswith('review:'))
async def review(call: CallbackQuery):
    if call.message.chat.id != await _review_group_id() or not await can_review(call.from_user.id):
        await call.answer('Нет доступа к проверке', show_alert=True)
        return
    try:
        _, action, sid = call.data.split(':')
        sub_id = int(sid)
    except (ValueError, IndexError):
        await call.answer('Неверная команда', show_alert=True)
        return
    if action not in ('accept', 'reject'):
        await call.answer('Неверное действие', show_alert=True)
        return
    async with DB.sessions() as s:
        sub = await s.scalar(select(Submission).where(Submission.id == sub_id).with_for_update())
        delivery = await s.get(ReviewDelivery, sub_id)
        if not sub or not delivery or delivery.chat_id != call.message.chat.id or delivery.message_id != call.message.message_id:
            await call.answer('Карточка работы не найдена', show_alert=True)
            return
        if sub.status != 'pending':
            await call.answer('Работа уже обработана', show_alert=True)
            return
        t = TASKS.get(sub.task_id)
        if not t:
            await call.answer('Задание не найдено', show_alert=True)
            return
        if action == 'accept' and not preview_enabled(sub.user_id):
            participant = await s.get(User, sub.user_id)
            attendee = await s.get(TripAttendee, rules.normalize_whitelist_name(participant.full_name)) \
                       if participant and participant.full_name else None
            if not attendee or attendee.telegram_id != sub.user_id:
                await call.answer('Нет подтверждённого допуска', show_alert=True)
                return
        sub.status = 'accepted' if action == 'accept' else 'rejected'
        sub.reviewed_by = call.from_user.id
        sub.reviewed_at = utcnow()
        if action == 'accept':
            s.add(PointEntry(user_id=sub.user_id, delta=Decimal(str(t['points'])),
                             source_key=f'task:{sub.user_id}:{sub.task_id}',
                             actor_id=call.from_user.id))
        await s.commit()
    await call.answer('Решение сохранено')
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.reply(f'Работа №{sub_id}: '
                             f'{"Принято" if action == "accept" else "Отклонено"}. '
                             f'Проверил: {call.from_user.full_name}. '
                             f'Начислено: {rules.points_label(t["points"] if action == "accept" else 0)}.')


@router.message(Command('review_override'))
async def review_override(message: Message):
    """Only the superadmin may correct a completed decision; compensating ledger entry."""
    if message.chat.type != 'private' or message.from_user.id != SETTINGS.super_admin_id:
        return
    args = (message.text or '').split()
    if len(args) != 3 or not args[1].isdigit() or args[2].lower() not in ('accept', 'reject'):
        await message.answer('Формат: /review_override НОМЕР_РАБОТЫ accept или reject')
        return
    sub_id, wanted = int(args[1]), ('accepted' if args[2].lower() == 'accept' else 'rejected')
    async with DB.sessions() as s:
        sub = await s.scalar(select(Submission).where(Submission.id == sub_id).with_for_update())
        if not sub or sub.status not in ('accepted', 'rejected'):
            await message.answer('Работа не найдена или ещё не проверена.')
            return
        if sub.status == wanted:
            await message.answer('У этой работы уже такой результат. Баллы не менялись.')
            return
        task = TASKS.get(sub.task_id)
        if not task:
            await message.answer('Задание не найдено, изменение отменено.')
            return
        previous = sub.status
        delta = Decimal(str(task['points'])) * (1 if wanted == 'accepted' else -1)
        from uuid import uuid4
        s.add(PointEntry(user_id=sub.user_id, delta=delta,
                         source_key='review_override:' + uuid4().hex, actor_id=message.from_user.id))
        sub.status = wanted
        sub.reviewed_by = message.from_user.id
        sub.reviewed_at = utcnow()
        await s.commit()
    await message.answer(f'Работа №{sub_id}: {previous} → {wanted}. '
                         f'Корректировка баллов: {delta:+}.')


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
    if not gid or call.message.chat.id != gid or not await can_support(call.from_user.id):
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
            f'Ответь на это сообщение командой /answer текст для обращения №{case_id}. '
            'Обычные Reply остаются внутренней перепиской и не отправляются участнику.'
        )
        async with DB.sessions() as s:
            s.add(SupportMirror(group_message_id=prompt.message_id, case_id=case_id))
            await s.commit()


@router.message(Command('edit_reply'), F.chat.type.in_({'group', 'supergroup'}))
async def edit_support_reply(message: Message, bot: Bot):
    """Edit a previously delivered *text* answer by replying to the staff message."""
    gid = await get_support_chat_id()
    if not gid or message.chat.id != gid or not message.from_user or not await can_support(message.from_user.id):
        return
    if not message.reply_to_message:
        await message.reply('Ответь командой /edit_reply новый текст на исходное сообщение администратора.')
        return
    new_text = (message.text or '').partition(' ')[2].strip()
    if not new_text:
        await message.reply('Напиши /edit_reply и новый правильный текст в одной строке.')
        return
    if len('Ответ на твой вопрос:\n\n' + new_text) > 4096:
        await message.reply('Слишком длинный ответ: Telegram допускает не более 4096 символов.')
        return
    async with DB.sessions() as s:
        sent = await s.get(SupportSentReply, message.reply_to_message.message_id)
        if not sent:
            await message.reply('Это старый ответ без сохранённого ID сообщения. Его нельзя изменить, но можно отправить исправление через /correct_reply.')
            return
        if not sent.text_editable:
            await message.reply('Медиаответ нельзя изменить этой командой. Используй /correct_reply для текстового исправления.')
            return
        participant_id, participant_message_id = sent.participant_id, sent.participant_message_id
    try:
        await bot.edit_message_text('Ответ на твой вопрос:\n\n'+new_text,
                                    chat_id=participant_id, message_id=participant_message_id)
    except Exception:
        log.exception('Failed to edit support reply')
        await message.reply('Не удалось изменить сообщение у участника. Можно отправить исправление через /correct_reply.')
        return
    await message.reply('Ответ Майи в личном чате участника изменён.')


@router.message(Command('correct_reply'), F.chat.type.in_({'group', 'supergroup'}))
async def correct_support_reply(message: Message, bot: Bot):
    """Send a correction for legacy replies without a saved Telegram message ID."""
    gid = await get_support_chat_id()
    if not gid or message.chat.id != gid or not message.from_user or not await can_support(message.from_user.id):
        return
    if not message.reply_to_message:
        await message.reply('Ответь командой /correct_reply правильный текст на ошибочное сообщение администратора.')
        return
    correction = (message.text or '').partition(' ')[2].strip()
    if not correction:
        await message.reply('Напиши /correct_reply и правильный текст.')
        return
    if len('Исправление предыдущего ответа:\n\n' + correction) > 4096:
        await message.reply('Слишком длинное сообщение.')
        return
    async with DB.sessions() as s:
        mirror = await s.get(SupportMirror, message.reply_to_message.message_id)
        if not mirror:
            await message.reply('Не найдено обращение для этого сообщения. Ответь на исходное сообщение администратора.')
            return
        case = await s.get(SupportCase, mirror.case_id)
        if not case:
            await message.reply('Обращение не найдено.')
            return
        participant_id = case.user_id
    try:
        await bot.send_message(participant_id, 'Исправление предыдущего ответа:\n\n' + correction)
    except Exception:
        log.exception('Failed to correct support reply')
        await message.reply('Не удалось отправить исправление участнику.')
        return
    await message.reply('Исправление отправлено участнику от имени Майи.')


@router.message(Command('answer'), F.chat.type.in_({'group', 'supergroup'}))
async def support_group_reply(message: Message, bot: Bot):
    gid = await get_support_chat_id()
    if not gid or message.chat.id != gid or not message.from_user or message.from_user.is_bot:
        return
    if not await can_support(message.from_user.id):
        return
    if not message.reply_to_message:
        await message.reply('Чтобы отправить ответ участнику, ответь на его обращение командой /answer текст.')
        return
    answer_text = (message.text or '').partition(' ')[2].strip()
    if not answer_text:
        await message.reply('Напиши /answer и текст ответа в одном сообщении. Обычные Reply остаются внутри группы.')
        return
    if len('Ответ на твой вопрос:\n\n' + answer_text) > 4096:
        await message.reply('Ответ слишком длинный для Telegram (максимум 4096 символов).')
        return
    async with DB.sessions() as s:
        mirror = await s.get(SupportMirror, message.reply_to_message.message_id)
        if not mirror:
            await message.reply('Это сообщение не связано с обращением участника. Ответь командой /answer на исходный вопрос.')
            return
        case = await s.get(SupportCase, mirror.case_id)
        if not case or case.status != 'open':
            await message.reply('Обращение уже закрыто, ответ не отправлен.')
            return
        user_id, case_id = case.user_id, case.id
    try:
        delivered = await bot.send_message(user_id, 'Ответ на твой вопрос:\n\n' + answer_text)
    except Exception:
        log.exception('Support reply to %s failed', user_id)
        await message.reply('Не удалось доставить ответ участнику. Проверь, не заблокировал ли он бота.')
        return
    async with DB.sessions() as s:
        s.add(SupportMirror(group_message_id=message.message_id, case_id=case_id))
        s.add(SupportSentReply(group_message_id=message.message_id, case_id=case_id,
                               participant_id=user_id, participant_message_id=delivered.message_id,
                               text_editable=True))
        await s.commit()
    await message.reply('Ответ доставлен участнику от имени Майи. Для исправления ответь на своё сообщение командой /edit_reply новый текст.')


# Operational codes are restricted to the defined dates/times in Moscow.
CODE_FILE = Path(__file__).resolve().parent.parent / 'data' / 'code_words_tickets.json'
CODE_DATA = json.loads(CODE_FILE.read_text(encoding='utf-8'))
CODE_WORDS = {int(day): words for day, words in CODE_DATA['code_words'].items()}
TICKETS = {int(day): frozenset(codes) for day, codes in CODE_DATA['tickets'].items()}


def _kind_title(kind: str) -> str:
    return 'лекции' if kind == 'lecture' else 'мастер-класса'


def _code_time_valid(day: int, kind: str, now=None) -> bool:
    if day not in CODE_WORDS or kind not in ('lecture', 'mk'):
        return False
    now = now or rules.current_msk()
    return (rules.visible_lecture_button(day, now) if kind == 'lecture'
            else rules.visible_mk_button(day, now))


async def _window_is_open(session, day: int, kind: str, now=None) -> bool:
    if not _code_time_valid(day, kind):
        return False
    row = await session.get(CodeWindow, (day, kind))
    return bool(row and row.closes_at and row.closes_at > (now or utcnow()))


@router.callback_query(F.data == 'admin:codes')
async def admin_code_controls(call: CallbackQuery):
    if call.message.chat.type != 'private' or not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    await _show_code_controls(call.message)


async def _show_code_controls(message: Message):
    rows = []
    async with DB.sessions() as session:
        for day in rules.LECTURE_DATES:
            for kind in ('lecture', 'mk'):
                window = await session.get(CodeWindow, (day, kind))
                active = bool(window and window.closes_at and window.closes_at > utcnow()
                              and _code_time_valid(day, kind))
                label = f'{day}.11 {_kind_title(kind)} — ' + ('ОТКРЫТО' if active else 'закрыто')
                rows.append([(label, f'admincode:detail:{day}:{kind}')])
    rows.append([('Назад в админку', 'admin:codeback')])
    await formatted_answer(message, 'Управление кодовыми словами. Каждое окно открывается на 5 минут только во время соответствующего занятия. Баллы: лекция 0,5 (по регистрации), мастер-класс 1 (для всех участников с ФИО).', reply_markup=kb.make(rows))


@router.callback_query(F.data == 'admin:codeback')
async def admin_code_back(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    await formatted_answer(
        call.message, 'Панель администратора',
        reply_markup=kb.admin_menu(is_super=call.from_user.id == SETTINGS.super_admin_id))


@router.callback_query(F.data.startswith('admincode:detail:'))
async def admin_code_detail(call: CallbackQuery):
    if call.message.chat.type != 'private' or not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    try:
        _, _, day_s, kind = call.data.split(':')
        day = int(day_s)
    except (ValueError, TypeError):
        await call.answer('Неверные параметры', show_alert=True)
        return
    if day not in CODE_WORDS or kind not in ('lecture', 'mk'):
        await call.answer('Неверные параметры', show_alert=True)
        return
    await call.answer()
    async with DB.sessions() as session:
        row = await session.get(CodeWindow, (day, kind))
        closes = row.closes_at if row else None
    status = (f'Открыто до {closes.astimezone(rules.MOSCOW):%H:%M:%S} МСК'
              if closes and closes > utcnow() and _code_time_valid(day, kind) else 'Закрыто')
    allowed = _code_time_valid(day, kind)
    actions = []
    if allowed:
        actions.append([('Открыть на 5 минут', f'admincode:open:{day}:{kind}')])
    actions.append([('Закрыть сейчас', f'admincode:close:{day}:{kind}')])
    actions.append([('К окнам', 'admin:codes')])
    await formatted_answer(call.message, f'{day} ноября · {_kind_title(kind)}\nСтатус: {status}' +
                           ('' if allowed else '\nОткрытие возможно только в часы занятия.'),
                           reply_markup=kb.make(actions))


@router.callback_query(F.data.startswith('admincode:open:') | F.data.startswith('admincode:close:'))
async def admin_code_change(call: CallbackQuery):
    if call.message.chat.type != 'private' or not await is_admin(call.from_user.id):
        await call.answer('Нет доступа', show_alert=True)
        return
    try:
        _, action, day_s, kind = call.data.split(':')
        day = int(day_s)
    except (TypeError, ValueError):
        await call.answer('Неверная команда', show_alert=True)
        return
    if day not in CODE_WORDS or kind not in ('lecture', 'mk') or action not in ('open', 'close'):
        await call.answer('Неверная команда', show_alert=True)
        return
    if action == 'open' and not _code_time_valid(day, kind):
        await call.answer('Сейчас это окно нельзя открыть', show_alert=True)
        return
    now = utcnow()
    end = min(now + timedelta(minutes=5),
              rules.day_end(day).astimezone(timezone.utc)) if action == 'open' else now
    async with DB.sessions() as session:
        await session.execute(pg_insert(CodeWindow).values(day=day, kind=kind, closes_at=end,
            opened_by=call.from_user.id).on_conflict_do_update(
                index_elements=['day','kind'], set_={'closes_at':end,'opened_by':call.from_user.id, 'updated_at':now}))
        await session.commit()
    await call.answer('Окно открыто' if action == 'open' else 'Окно закрыто')
    await formatted_answer(call.message,
        f'{day} ноября · {_kind_title(kind)}: ' +
        (f'приём открыт до {end.astimezone(rules.MOSCOW):%H:%M:%S} МСК.'
         if action == 'open' else 'приём закрыт.'),
        reply_markup=kb.make([[('К окнам', 'admin:codes')]]))


@router.callback_query(F.data.startswith('code:'))
async def code_enter(call: CallbackQuery, state: FSMContext):
    try:
        _, day_s, kind = call.data.split(':')
        day = int(day_s)
    except (TypeError, ValueError):
        await call.answer('Неверный код', show_alert=True)
        return
    if day not in CODE_WORDS or kind not in ('lecture', 'mk'):
        await call.answer('Неверная команда', show_alert=True)
        return
    await call.answer()
    if not _code_time_valid(day, kind):
        await formatted_answer(call.message, 'Сейчас ввод кодового слова недоступен.')
        return
    if kind == 'lecture' and not await registered(call.from_user.id, day):
        await formatted_answer(call.message, 'Для получения баллов за лекцию нужна регистрация до её начала.')
        return
    if await require_name(call, state, call.data):
        return
    async with DB.sessions() as session:
        if not await _window_is_open(session, day, kind):
            await formatted_answer(call.message, 'Сейчас ввод кодового слова недоступен.')
            return
    await state.clear()
    await state.set_state(Input.code_word)
    await state.update_data(day=day, kind=kind)
    await formatted_answer(call.message, 'Введите кодовое слово, которое назвал спикер. Для отмены — /cancel.')


@router.message(Input.code_word)
async def code_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    day, kind = data.get('day'), data.get('kind')
    if not isinstance(day, int) or kind not in ('lecture', 'mk'):
        await state.clear()
        return
    if not message.text or message.text.startswith('/'):
        await formatted_answer(message, 'Пришли кодовое слово текстом или отправь /cancel.')
        return
    if kind == 'lecture' and not await registered(message.from_user.id, day):
        await state.clear()
        await formatted_answer(message, 'Для получения баллов за лекцию нужна регистрация.')
        return
    now = utcnow()
    async with DB.sessions() as session:
        if not await _window_is_open(session, day, kind, now):
            await state.clear()
            await formatted_answer(message, 'Сейчас ввод кодового слова недоступен.')
            return
        # Serialize retries by user and event; throttle automated guessing.
        await session.execute(pg_insert(CodeAttempt).values(user_id=message.from_user.id, day=day,
            kind=kind, attempted_at=now-timedelta(seconds=4)).on_conflict_do_nothing(index_elements=['user_id','day','kind']))
        attempt = await session.scalar(select(CodeAttempt).where(
            CodeAttempt.user_id == message.from_user.id, CodeAttempt.day == day,
            CodeAttempt.kind == kind).with_for_update())
        if attempt.attempted_at and (now - attempt.attempted_at).total_seconds() < 3:
            await formatted_answer(message, 'Подождите несколько секунд перед следующей попыткой.')
            return
        attempt.attempted_at = now
        if rules.normalize_code_word(message.text) == rules.normalize_code_word(CODE_WORDS[day][kind]):
            delta = Decimal('0.5') if kind == 'lecture' else Decimal('1.0')
            await session.execute(pg_insert(PointEntry).values(
                user_id=message.from_user.id, delta=delta, actor_id=None,
                source_key=f'code:{day}:{kind}:{message.from_user.id}').on_conflict_do_nothing(index_elements=['source_key']))
        await session.commit()
    # Deliberately neutral: do not tell the participant if the word was correct.
    await state.clear()
    await formatted_answer(message, 'Ваш ответ принят!')


@router.callback_query(F.data.startswith('ticket:'))
async def ticket_enter(call: CallbackQuery, state: FSMContext):
    try:
        day = int(call.data.split(':')[1])
    except (ValueError, IndexError):
        await call.answer('Неверная дата', show_alert=True)
        return
    await call.answer()
    if day not in TICKETS or not rules.visible_lecture_button(day):
        await formatted_answer(call.message, 'Сейчас счастливые билеты недоступны.')
        return
    if not await registered(call.from_user.id, day):
        await formatted_answer(call.message, 'Счастливые билеты доступны только зарегистрированным на лекцию.')
        return
    if await require_name(call, state, call.data):
        return
    await state.clear()
    await state.set_state(Input.ticket_code)
    await state.update_data(day=day)
    await formatted_answer(call.message, 'Нашли счастливый билет? Введите его уникальный код. Для отмены — /cancel.')


@router.message(Input.ticket_code)
async def ticket_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    day = data.get('day')
    if day not in TICKETS:
        await state.clear()
        return
    if not message.text or message.text.startswith('/'):
        await formatted_answer(message, 'Введите код билета текстом или /cancel.')
        return
    if not rules.visible_lecture_button(day) or not await registered(message.from_user.id, day):
        await state.clear()
        await formatted_answer(message, 'Сейчас счастливые билеты недоступны.')
        return
    code = re.sub(r'\s+', '', message.text).upper().replace('–', '-').replace('—', '-')
    if code not in TICKETS[day]:
        await formatted_answer(message, 'Кажется, такого билета у меня нет. Проверьте символы и попробуйте ещё раз.')
        return
    async with DB.sessions() as session:
        own = await session.scalar(select(TicketRedemption).where(
            TicketRedemption.user_id == message.from_user.id,
            TicketRedemption.day == day))
        if own:
            await state.clear()
            await formatted_answer(message, 'Вы уже активировали счастливый билет на эту лекцию!')
            return
        result = await session.execute(pg_insert(TicketRedemption).values(
            code=code, user_id=message.from_user.id, day=day
        ).on_conflict_do_nothing().returning(TicketRedemption.code))
        if result.scalar_one_or_none() is None:
            await session.rollback()
            await formatted_answer(message, 'Этот билет уже активирован или у вас уже есть билет на эту лекцию.')
            return
        await session.execute(pg_insert(PointEntry).values(
            user_id=message.from_user.id, delta=Decimal('1.0'), actor_id=None,
            source_key=f'ticket:{day}:{message.from_user.id}'
        ).on_conflict_do_nothing(index_elements=['source_key']))
        await session.commit()
    await state.clear()
    await formatted_answer(message, 'Поздравляю! Ваш счастливый билет активирован. Вам начислен 1 балл!')


# Urgent broadcasts: administrator chooses the audience before composing any message.
# Trip audience uses only explicitly linked Telegram IDs (not unverified name strings).
URGENT_AUDIENCES = {
    'all': 'Все пользователи бота',
    'trip': 'Только подтверждённые участники выезда',
    'lectures': 'Записанные хотя бы на одну лекцию',
    'lecture23': 'Записанные на PR — 23 ноября',
    'lecture24': 'Записанные на ER — 24 ноября',
    'lecture25': 'Записанные на партнёрку — 25 ноября',
}


async def urgent_recipient_ids(audience: str) -> list[int]:
    """Determine eligible recipients from persistent records, deduplicated and ordered."""
    if audience not in URGENT_AUDIENCES:
        raise ValueError('Unknown urgent broadcast audience')
    async with DB.sessions() as session:
        if audience == 'all':
            stmt = select(User.id)
        elif audience == 'trip':
            # Only allowlist identities which were claimed by a Telegram account.
            stmt = select(User.id).join(TripAttendee, TripAttendee.telegram_id == User.id)
        elif audience == 'lectures':
            stmt = select(User.id).join(Registration, Registration.user_id == User.id).where(Registration.active.is_(True))
        else:
            day = int(audience.removeprefix('lecture'))
            stmt = select(User.id).join(Registration, Registration.user_id == User.id).where(
                Registration.active.is_(True), Registration.day == day)
        return sorted(set((await session.scalars(stmt)).all()))


@router.callback_query(F.data == 'admin:urgent')
async def urgent_begin(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id) or call.message.chat.type != 'private':
        await call.answer('Нет доступа', show_alert=True)
        return
    await call.answer()
    await state.clear()
    await state.set_state(Input.urgent_audience)
    await formatted_answer(
        call.message,
        'Кому отправить срочное сообщение? Выбери аудиторию:',
        reply_markup=kb.make([
            [('Всем пользователям', 'urgent:audience:all')],
            [('Участникам выезда', 'urgent:audience:trip')],
            [('Записанным на любую лекцию', 'urgent:audience:lectures')],
            [('PR — 23 ноября', 'urgent:audience:lecture23')],
            [('ER — 24 ноября', 'urgent:audience:lecture24')],
            [('Партнёрка — 25 ноября', 'urgent:audience:lecture25')],
            [('Отмена', 'urgent:cancel')],
        ]),
    )


@router.callback_query(F.data.startswith('urgent:audience:'))
async def urgent_select_audience(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id) or call.message.chat.type != 'private':
        await call.answer('Нет доступа', show_alert=True)
        return
    audience = call.data.removeprefix('urgent:audience:')
    if audience not in URGENT_AUDIENCES or await state.get_state() != Input.urgent_audience.state:
        await call.answer('Выбор устарел, начни новую рассылку.', show_alert=True)
        return
    recipients = await urgent_recipient_ids(audience)
    await state.update_data(urgent_audience=audience, urgent_recipient_ids=recipients)
    await state.set_state(Input.urgent_content)
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await formatted_answer(
        call.message,
        f'Аудитория: {URGENT_AUDIENCES[audience]}.\n'
        f'Получателей: {len(recipients)}.\n'
        'Пришли ОДНО сообщение: текст, фото, видео, видеокружочек или документ. '
        'Сохранятся исходные текст и форматирование. Для отмены — /cancel.\n'
        'Важное уточнение: участники выезда учитываются после привязки ФИО к Telegram; '
        'получатели, ещё не открывшие бот, не получат сообщение.'
        if audience == 'trip' else
        f'Аудитория: {URGENT_AUDIENCES[audience]}.\n'
        f'Получателей: {len(recipients)}.\n'
        'Пришли ОДНО сообщение: текст, фото, видео, видеокружочек или документ. '
        'Для отмены — /cancel.'
    )


@router.message(Input.urgent_content)
async def urgent_content(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id) or message.chat.type != 'private':
        await state.clear()
        return
    if not (message.text or message.photo or message.video or message.video_note or message.document):
        await formatted_answer(message, 'Пришли текст, фото, видео, кружочек или документ. Для отмены — /cancel.')
        return
    data = await state.get_data()
    if data.get('urgent_audience') not in URGENT_AUDIENCES:
        await state.clear()
        await formatted_answer(message, 'Аудитория не выбрана. Открой срочную рассылку заново.')
        return
    await state.update_data(urgent_message_id=message.message_id, urgent_chat_id=message.chat.id)
    await state.set_state(Input.urgent_confirm)
    await formatted_answer(message, 'Предпросмотр рассылки:')
    await message.copy_to(message.chat.id)
    audience = data['urgent_audience']
    total = len(data['urgent_recipient_ids'])
    await formatted_answer(
        message,
        f'Аудитория: {URGENT_AUDIENCES[audience]}\nПолучателей: {total}\n'
        'Это настоящая отправка. Подтвердить?',
        reply_markup=kb.make([
            [('Отправить выбранной аудитории', 'urgent:send')],
            [('Отмена', 'urgent:cancel')],
        ]),
    )


@router.callback_query(F.data.startswith('urgent:'))
async def urgent_confirm(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_admin(call.from_user.id) or call.message.chat.type != 'private':
        await call.answer('Нет доступа', show_alert=True)
        return
    action = call.data.split(':', 1)[1]
    current_state = await state.get_state()
    if action == 'cancel' and current_state in (Input.urgent_audience.state,
                                                 Input.urgent_content.state,
                                                 Input.urgent_confirm.state):
        await state.clear()
        await call.answer()
        await call.message.edit_reply_markup(reply_markup=None)
        await formatted_answer(call.message, 'Рассылка отменена.')
        return
    if action != 'send' or current_state != Input.urgent_confirm.state:
        await call.answer('Рассылка уже обработана или отменена.', show_alert=True)
        return
    data = await state.get_data()
    audience = data.get('urgent_audience')
    if audience not in URGENT_AUDIENCES:
        await state.clear()
        await call.answer('Аудитория не определена.', show_alert=True)
        return
    ids = data.get('urgent_recipient_ids', [])
    await state.clear()  # Prevent ordinary repeat sends.
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    if not ids:
        await formatted_answer(call.message, 'Сейчас нет получателей в выбранной аудитории. Рассылка не отправлена.')
        return
    async with DB.sessions() as session:
        record = UrgentBroadcast(
            actor_id=call.from_user.id,
            source_chat_id=data['urgent_chat_id'],
            source_message_id=data['urgent_message_id'],
            status='sending',
        )
        session.add(record)
        await session.flush()
        record_id = record.id
        # Store audience in existing settings table; no schema changes or migrations.
        session.add(BotSetting(key=f'urgent_audience:{record_id}', value=audience))
        await session.commit()
    await formatted_answer(call.message, f'Запустила рассылку №{record_id} ({URGENT_AUDIENCES[audience]}).')
    ok = failed = 0
    for user_id in ids:
        try:
            await bot.copy_message(
                chat_id=user_id, from_chat_id=data['urgent_chat_id'],
                message_id=data['urgent_message_id'],
            )
            ok += 1
        except Exception:
            failed += 1
            log.warning('Urgent broadcast %s: could not deliver to user %s', record_id, user_id)
        await asyncio.sleep(0.08)
    async with DB.sessions() as session:
        record = await session.get(UrgentBroadcast, record_id)
        record.sent_count, record.failed_count, record.status = ok, failed, 'complete'
        await session.commit()
    notice = (
        f'Срочная рассылка №{record_id} завершена. Аудитория: {URGENT_AUDIENCES[audience]}. '
        f'Получателей: {len(ids)}. Доставлено: {ok}. Не доставлено: {failed}. '
        f'Отправитель: {call.from_user.id}.'
    )
    await formatted_answer(call.message, notice)
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
