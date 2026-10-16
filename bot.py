"""Health Tracker JSON MVP: per-user atomic JSON files, single-process bot."""
import asyncio
import io
import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '')
DEFAULT_TZ = os.getenv('DEFAULT_TIMEZONE', 'Asia/Yekaterinburg')
DATA_DIR = Path(os.getenv('DATA_DIR', '/app/data'))
router = Router()
locks = defaultdict(asyncio.Lock)


def now():
    return datetime.now(timezone.utc)


def fresh():
    return {'version': 1, 'timezone': DEFAULT_TZ, 'next_id': 1,
            'metrics': [], 'foods': [], 'workouts': [], 'reminders': []}


def path_for(uid):
    return DATA_DIR / f'user_{int(uid)}.json'


def load(uid):
    p = path_for(uid)
    if not p.exists():
        return fresh()
    with p.open('r', encoding='utf-8') as f:
        return json.load(f)


def save(uid, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = path_for(uid)
    # Atomic replace avoids half-written files on container interruption.
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, prefix='.health_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def modify(uid, callback):
    async with locks[int(uid)]:
        data = await asyncio.to_thread(load, uid)
        result = callback(data)
        await asyncio.to_thread(save, uid, data)
        return result


async def read(uid):
    async with locks[int(uid)]:
        return await asyncio.to_thread(load, uid)


def new_record(data, group, **fields):
    rid = data['next_id']
    data['next_id'] += 1
    item = {'id': rid, 'at': now().isoformat(), **fields}
    data[group].append(item)
    return rid


def tz_for(data):
    try:
        return ZoneInfo(data.get('timezone', DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def format_at(record, tz):
    return datetime.fromisoformat(record['at']).astimezone(tz).strftime('%d.%m %H:%M')


def tail(m):
    parts = (m.text or '').split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ''


def number(s):
    value = float(s.replace(',', '.'))
    if not (-1e7 < value < 1e7):
        raise ValueError('Недопустимое число')
    return value


def main_menu():
    app_url = os.getenv('WEBAPP_URL', '').strip()
    items = ([InlineKeyboardButton(text='📱 Открыть Mini App', web_app=WebAppInfo(url=app_url))] if app_url.startswith('https://') else [])
    return InlineKeyboardMarkup(inline_keyboard=([items] if items else []) + [
        [InlineKeyboardButton(text='📅 Сегодня', callback_data='view:today'),
         InlineKeyboardButton(text='📈 7 дней', callback_data='view:week')],
        [InlineKeyboardButton(text='🩺 Измерения', callback_data='view:history'),
         InlineKeyboardButton(text='🥗 Питание', callback_data='view:foodlog')],
        [InlineKeyboardButton(text='🏃 Тренировки', callback_data='view:workouts'),
         InlineKeyboardButton(text='🔔 Напоминания', callback_data='view:reminders')],
        [InlineKeyboardButton(text='❔ Команды', callback_data='view:help')],
    ])


def fmt_number(value):
    return f'{value:g}'


def summarize(data, days=1):
    tz = tz_for(data)
    today = now().astimezone(tz).date()
    start = today - timedelta(days=days-1)
    def selected(group):
        return [r for r in data[group] if start <= datetime.fromisoformat(r['at']).astimezone(tz).date() <= today]
    foods = selected('foods')
    workouts = selected('workouts')
    metrics = selected('metrics')
    sums = {key: sum(r.get(key, 0) or 0 for r in foods) for key in ('calories', 'protein', 'fat', 'carbs')}
    exercise_minutes = sum(r['minutes'] for r in workouts)
    exercise_kcal = sum(r.get('calories') or 0 for r in workouts)
    weight = [r for r in metrics if r['kind'] == 'weight']
    pressure = [r for r in metrics if r['kind'] == 'pressure']
    pulse = [r for r in metrics if r['kind'] == 'pulse']
    header = '📅 Сегодня' if days == 1 else f'📈 Последние {days} дней'
    result = [header, '', f'🥗 Питание: {len(foods)} записей',
              f'🔥 {sums["calories"]:.0f} ккал · Б {sums["protein"]:.1f} · Ж {sums["fat"]:.1f} · У {sums["carbs"]:.1f}',
              f'🏃 Тренировки: {len(workouts)} · {exercise_minutes} минут']
    if exercise_kcal:
        result.append(f'⚡ Записанный расход: {exercise_kcal:.0f} ккал (оценка)')
    result.append(f'🩺 Измерений: {len(metrics)}')
    if weight:
        last = max(weight, key=lambda r:r['at'])
        result.append(f'⚖️ Последний вес: {fmt_number(last["v1"])} кг')
        if days > 1 and len(weight) > 1:
            first = min(weight, key=lambda r:r['at'])
            result.append(f'↔️ Изменение между первой и последней записями: {last["v1"]-first["v1"]:+.1f} кг')
    if pressure:
        last = max(pressure, key=lambda r:r['at'])
        result.append(f'❤️ Последнее давление: {fmt_number(last["v1"])} / {fmt_number(last["v2"])}')
    if pulse:
        last = max(pulse, key=lambda r:r['at'])
        result.append(f'💓 Последний пульс: {fmt_number(last["v1"])}')
    result.append('')
    result.append('Данные только по внесённым записям; неполный дневник не означает нулевое потребление.')
    return '\n'.join(result)


def history_text(d):
    rows = sorted(d['metrics'], key=lambda r:r['at'], reverse=True)[:15]
    return '📋 Последние измерения:\n' + '\n'.join(
        f"#{r['id']} · {format_at(r, tz_for(d))} · {r['kind']}: {r['v1']:g}" +
        (f"/{r['v2']:g}" if r['v2'] is not None else '') for r in rows) if rows else 'Измерений пока нет.'


def foodlog_text(d):
    rows = sorted(d['foods'], key=lambda r:r['at'], reverse=True)[:15]
    return '🥗 Питание:\n' + '\n'.join(
        f"#{r['id']} · {format_at(r, tz_for(d))} · {r['title']}, {r['grams']:g} г · {r['calories']:g} ккал" for r in rows) if rows else 'Питание ещё не записывали.'


def workouts_text(d):
    rows = sorted(d['workouts'], key=lambda r:r['at'], reverse=True)[:15]
    return '🏋️ Тренировки:\n' + '\n'.join(
        f"#{r['id']} · {format_at(r, tz_for(d))} · {r['title']} · {r['minutes']} мин" +
        (f" · {r['calories']:g} ккал" if r['calories'] is not None else '') for r in rows) if rows else 'Тренировок пока нет.'


def reminders_text(d):
    rows = sorted((r for r in d['reminders'] if r['active']), key=lambda r:r['local_time'])
    return '🔔 Напоминания:\n' + '\n'.join(
        f"#{r['id']} · {r['local_time']} · {r['title']}" for r in rows) if rows else 'Напоминаний нет.'


@router.message(Command('app'))
async def app_cmd(m: Message):
    url = os.getenv('WEBAPP_URL','').strip()
    if not url.startswith('https://'):
        return await m.answer('Укажите WEBAPP_URL с HTTPS в переменных окружения и перезапустите бота.')
    await m.answer('📱 Откройте дневник здоровья:', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть Mini App', web_app=WebAppInfo(url=url))]]))

@router.message(Command('menu'))
async def menu_cmd(m: Message):
    await m.answer('🩺 Health Tracker\nВыберите раздел:', reply_markup=main_menu())


@router.message(Command('today', 'week'))
async def summary_cmd(m: Message):
    cmd = m.text.split()[0].split('@')[0][1:]
    d = await read(m.from_user.id)
    await m.answer(summarize(d, 7 if cmd == 'week' else 1), reply_markup=main_menu())


@router.callback_query(lambda c: bool(c.data and c.data.startswith('view:')))
async def view_callback(c: CallbackQuery):
    key = c.data.partition(':')[2]
    d = await read(c.from_user.id)
    views = {'today': lambda: summarize(d, 1), 'week': lambda: summarize(d, 7),
             'history': lambda: history_text(d), 'foodlog': lambda: foodlog_text(d),
             'workouts': lambda: workouts_text(d), 'reminders': lambda: reminders_text(d),
             'help': lambda: HELP}
    if key not in views:
        return await c.answer('Неизвестная команда')
    await c.message.answer(views[key](), reply_markup=main_menu())
    await c.answer()


@router.message(Command('delete'))
async def delete_record(m: Message):
    try:
        rid = int(tail(m))
        if rid <= 0: raise ValueError
    except ValueError:
        return await m.answer('Формат: /delete 15 — удалить запись по номеру #15. Для напоминаний используйте /unremind.')
    def remove(d):
        for group in ('metrics', 'foods', 'workouts'):
            for i, record in enumerate(d[group]):
                if record['id'] == rid:
                    d[group].pop(i)
                    return group
        return None
    group = await modify(m.from_user.id, remove)
    await m.answer(f'🗑 Запись #{rid} удалена ({group}).' if group else 'Запись не найдена. Напоминания отключаются через /unremind.')


HELP = '''🩺 Health Tracker — дневник здоровья

📊 Показатели:
/pressure 125 80 72 — давление, пульс необязательно
/weight 67.8 — вес
/pulse 72 — пульс
/metric сахар 5.4 — другой показатель
/history — последние измерения
/chart weight — график (weight, pressure, pulse, сахар)

🥗 Питание:
/food Гречка; 150; 110; 4; 1; 21
Название; граммы; ккал/100 г; Б/100 г; Ж/100 г; У/100 г
/foodlog — последние блюда

🏃 Активность:
/workout Ходьба; 40; 150
Название; минуты; сожжённые ккал (необязательно)
/workouts — история

🔔 Напоминания:
/remind 09:00; Принять назначенное лекарство
/reminders — список
/unremind 3 — отключить напоминание
/timezone Europe/Moscow — часовой пояс

📅 /today — сводка за сегодня; /week — за 7 дней
🧭 /menu — кнопочное меню
🗑 /delete 15 — удалить запись #15
📦 /export — скачать все свои данные в JSON
Все записи сохраняются отдельно для каждого пользователя.'''


@router.message(CommandStart())
@router.message(Command('help'))
async def welcome(m: Message):
    await modify(m.from_user.id, lambda d: None)
    from wizard import menu as wizard_menu
    await m.answer('🩺 Добро пожаловать! Быстрый ввод — кнопками ниже. В /app доступен Mini App.\n\n'+HELP, reply_markup=wizard_menu)


@router.message(Command('timezone'))
async def timezone_cmd(m: Message):
    name = tail(m)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return await m.answer('Пример: /timezone Europe/Moscow')
    await modify(m.from_user.id, lambda d: d.update(timezone=name))
    await m.answer(f'✅ Часовой пояс: {name}')


@router.message(Command('pressure', 'weight', 'pulse', 'metric'))
async def metric_add(m: Message):
    cmd = m.text.split()[0].split('@')[0][1:]
    args = tail(m).split()
    try:
        p = None
        if cmd == 'pressure':
            if len(args) not in (2, 3): raise ValueError
            a, b = map(number, args[:2])
            if not (40 <= a <= 300 and 20 <= b <= 200): raise ValueError
            kind = 'pressure'
            p = number(args[2]) if len(args) == 3 else None
            if p is not None and not (20 <= p <= 250): raise ValueError
        elif cmd in ('weight', 'pulse'):
            if len(args) != 1: raise ValueError
            a, b, kind = number(args[0]), None, cmd
            if not (1 <= a <= 500 if cmd == 'weight' else 20 <= a <= 250): raise ValueError
        else:
            if len(args) < 2: raise ValueError
            kind, a, b = ' '.join(args[:-1]).lower()[:40], number(args[-1]), None
            if not kind or kind in ('weight', 'pulse', 'pressure'): raise ValueError
    except (ValueError, IndexError):
        return await m.answer('Примеры: /pressure 125 80 72, /weight 67.8, /metric сахар 5.4')
    def store(d):
        new_record(d, 'metrics', kind=kind, v1=a, v2=b)
        if p is not None:
            new_record(d, 'metrics', kind='pulse', v1=p, v2=None)
    await modify(m.from_user.id, store)
    await m.answer('✅ Измерение сохранено.' + (' Пульс тоже записан.' if p is not None else ''))


@router.message(Command('history'))
async def history(m: Message):
    await m.answer(history_text(await read(m.from_user.id)))


@router.message(Command('chart'))
async def chart(m: Message):
    kind = tail(m).lower() or 'weight'
    d = await read(m.from_user.id)
    rows = sorted((r for r in d['metrics'] if r['kind'] == kind), key=lambda r:r['at'])[-100:]
    if len(rows) < 2:
        return await m.answer('Нужно минимум два измерения одного показателя.')
    dates = [datetime.fromisoformat(r['at']).astimezone(tz_for(d)) for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(dates, [r['v1'] for r in rows], marker='o', label=kind)
    if kind == 'pressure' and all(r['v2'] is not None for r in rows):
        ax.plot(dates, [r['v2'] for r in rows], marker='o', label='diastolic')
    ax.set_title(kind)
    ax.grid(alpha=.2)
    ax.legend()
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    plt.close(fig)
    await m.answer_photo(BufferedInputFile(buf.getvalue(), filename='chart.png'),
                         caption=f'📈 {kind} · {len(rows)} записей')


@router.message(Command('food'))
async def food(m: Message):
    try:
        a = [v.strip() for v in tail(m).split(';')]
        if len(a) != 6: raise ValueError
        title = a[0][:150]
        grams, kcal, protein, fat, carbs = map(number, a[1:])
        if not title or not (0 < grams <= 10000) or any(not 0 <= x <= 1000 for x in (kcal, protein, fat, carbs)):
            raise ValueError
    except ValueError:
        return await m.answer('Формат: /food Гречка; 150; 110; 4; 1; 21\nКБЖУ указываются на 100 г.')
    vals = [round(x * grams / 100, 1) for x in (kcal, protein, fat, carbs)]
    await modify(m.from_user.id, lambda d: new_record(d, 'foods', title=title, grams=grams,
                 calories=vals[0], protein=vals[1], fat=vals[2], carbs=vals[3]))
    await m.answer(f'✅ {title}, {grams:g} г\n{vals[0]:g} ккал · Б {vals[1]:g} · Ж {vals[2]:g} · У {vals[3]:g}')


@router.message(Command('foodlog'))
async def foodlog(m: Message):
    await m.answer(foodlog_text(await read(m.from_user.id)))


@router.message(Command('workout'))
async def workout(m: Message):
    try:
        a = [v.strip() for v in tail(m).split(';')]
        if len(a) not in (2, 3): raise ValueError
        title, minutes = a[0][:150], int(a[1])
        calories = number(a[2]) if len(a) == 3 and a[2] else None
        if not title or not 1 <= minutes <= 1440 or (calories is not None and not 0 <= calories <= 10000): raise ValueError
    except ValueError:
        return await m.answer('Формат: /workout Ходьба; 40; 150\nКкал необязательны.')
    await modify(m.from_user.id, lambda d: new_record(d, 'workouts', title=title, minutes=minutes, calories=calories))
    await m.answer('✅ Тренировка записана.')


@router.message(Command('workouts'))
async def workouts(m: Message):
    await m.answer(workouts_text(await read(m.from_user.id)))


@router.message(Command('remind'))
async def remind(m: Message):
    a = [v.strip() for v in tail(m).split(';', 1)]
    if len(a) != 2 or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', a[0]) or not a[1] or len(a[1]) > 200:
        return await m.answer('Формат: /remind 09:00; Принять назначенное лекарство')
    rid = await modify(m.from_user.id, lambda d: new_record(d, 'reminders',
                       title=a[1], local_time=a[0], active=True, last_sent_date=None))
    await m.answer(f"🔔 Напоминание #{rid}: ежедневно в {a[0]} — {a[1]}")


@router.message(Command('reminders'))
async def reminders(m: Message):
    await m.answer(reminders_text(await read(m.from_user.id)))


@router.message(Command('unremind'))
async def unremind(m: Message):
    try: rid = int(tail(m))
    except ValueError: return await m.answer('Пример: /unremind 3')
    def disable(d):
        for r in d['reminders']:
            if r['id'] == rid:
                r['active'] = False
                return True
        return False
    ok = await modify(m.from_user.id, disable)
    await m.answer('✅ Отключено.' if ok else 'Не найдено.')


@router.message(Command('export'))
async def export(m: Message):
    d = await read(m.from_user.id)
    contents = json.dumps(d, ensure_ascii=False, indent=2).encode('utf-8')
    await m.answer_document(BufferedInputFile(contents, filename='health_export.json'),
                            caption='🔐 Личная история. Не пересылайте файл посторонним.')


async def reminders_worker(bot: Bot):
    while True:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            for p in DATA_DIR.glob('user_*.json'):
                try:
                    uid = int(p.stem.removeprefix('user_'))
                    d = await read(uid)
                    local = now().astimezone(tz_for(d))
                    day = local.date().isoformat()
                    due = [r for r in d['reminders'] if r['active'] and
                           r['local_time'] == local.strftime('%H:%M') and r.get('last_sent_date') != day]
                    for r in due:
                        # Single process only; mark after send, so duplicates possible after crash.
                        try:
                            await bot.send_message(uid, f"🔔 {r['title']}\n{r['local_time']}")
                            def mark(data):
                                for item in data['reminders']:
                                    if item['id'] == r['id']:
                                        item['last_sent_date'] = day
                            await modify(uid, mark)
                        except Exception as ex:
                            print(f'Reminder #{r["id"]}: {type(ex).__name__}: {ex}')
                except Exception as ex:
                    print(f'User file {p.name}: {type(ex).__name__}: {ex}')
        except Exception as ex:
            print(f'Worker: {type(ex).__name__}: {ex}')
        await asyncio.sleep(20)


async def main():
    if not TOKEN or TOKEN == 'put_token_here':
        raise RuntimeError('BOT_TOKEN is missing')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    from wizard import router as wizard_router, menu as wizard_menu
    dp.include_router(wizard_router)
    from aiogram.types import BotCommand
    await bot.set_my_commands([BotCommand(command='start',description='Начать'), BotCommand(command='app',description='Открыть Mini App'), BotCommand(command='menu',description='Меню'), BotCommand(command='today',description='Сегодня'), BotCommand(command='help',description='Помощь')])
    task = asyncio.create_task(reminders_worker(bot))
    from miniapp_api import app
    import uvicorn
    config=uvicorn.Config(app,host='0.0.0.0',port=int(os.getenv('PORT','8080')),log_level='info',access_log=False)
    server=uvicorn.Server(config)
    api_task=asyncio.create_task(server.serve())
    try:
        await dp.start_polling(bot)
    finally:
        server.should_exit=True
        await api_task
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
