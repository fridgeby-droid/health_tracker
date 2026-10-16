"""Reply-keyboard guided data entry, reusing existing JSON record schema."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from datetime import datetime, timezone
from bot import modify, new_record, tz_for, read, number

router=Router()
class Step(StatesGroup):
    value=State()
    date=State()
    food=State()
    grams=State()
    workout=State()
    reminder=State()

menu=ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text='❤️ Давление'),KeyboardButton(text='⚖️ Вес'),KeyboardButton(text='💓 Пульс')],
    [KeyboardButton(text='🥗 Продукт'),KeyboardButton(text='🏃 Тренировка')],
    [KeyboardButton(text='🔔 Напоминание'),KeyboardButton(text='📅 Сегодня'),KeyboardButton(text='📊 История')],
],resize_keyboard=True)

@router.message(F.text.in_({'❤️ Давление','⚖️ Вес','💓 Пульс','🥗 Продукт','🏃 Тренировка','🔔 Напоминание','📅 Сегодня','📊 История'}))
async def choose(m:Message,state:FSMContext):
    text=m.text
    if text in ('📅 Сегодня','📊 История'):
        from bot import summarize,history_text
        data=await read(m.from_user.id)
        return await m.answer(summarize(data) if text=='📅 Сегодня' else history_text(data),reply_markup=menu)
    await state.clear()
    kinds={'❤️ Давление':'pressure','⚖️ Вес':'weight','💓 Пульс':'pulse'}
    if text in kinds:
        await state.update_data(kind=kinds[text])
        await state.set_state(Step.value)
        return await m.answer({'pressure':'Введите верхнее и нижнее давление через пробел, например 120 80','weight':'Введите вес в кг, например 67.5','pulse':'Введите пульс, например 72'}[kinds[text]]+'\nДля отмены: /cancel')
    if text=='🥗 Продукт':
        await state.set_state(Step.food)
        return await m.answer('Для быстрого выбора продукта и граммов используйте Mini App.\nИли введите: Название; ккал; белки; жиры; углеводы (на 100 г). Например: Творог; 121; 17; 5; 2')
    if text=='🏃 Тренировка':
        await state.set_state(Step.workout)
        return await m.answer('Введите: Название; минуты; сожжённые ккал (необязательно)')
    await state.set_state(Step.reminder)
    await m.answer('Введите: ЧЧ:ММ; Текст напоминания')

@router.message(F.text=='/cancel')
async def cancel(m:Message,state:FSMContext):
    await state.clear()
    await m.answer('Ввод отменён.',reply_markup=menu)

@router.message(Step.value)
async def value(m:Message,state:FSMContext):
    d=await state.get_data()
    kind=d['kind']
    try:
        nums=[number(v) for v in (m.text or '').split()]
        if len(nums)!=(2 if kind=='pressure' else 1):raise ValueError
        a=nums[0];b=nums[1] if kind=='pressure' else None
        limits={'pressure':(40,300),'pulse':(20,250),'weight':(1,500)}
        lo,hi=limits[kind]
        if not lo<=a<=hi or (kind=='pressure' and not (20<=b<=200 and a>b)):raise ValueError
    except ValueError:
        return await m.answer('Проверьте формат и допустимые значения. Попробуйте ещё раз или /cancel.')
    await state.update_data(v1=a,v2=b)
    await state.set_state(Step.date)
    await m.answer('Когда измеряли? Отправьте «сейчас» или дату и время ДД.ММ.ГГГГ ЧЧ:ММ')

@router.message(Step.date)
async def date(m:Message,state:FSMContext):
    from bot import now
    d=await state.get_data()
    profile=await read(m.from_user.id)
    try:
        when=now() if (m.text or '').lower()=='сейчас' else datetime.strptime(m.text.strip(),'%d.%m.%Y %H:%M').replace(tzinfo=tz_for(profile)).astimezone(timezone.utc)
        if when>now() or when.year<2000:raise ValueError
    except (ValueError,AttributeError):
        return await m.answer('Формат: 13.10.2026 08:30 или «сейчас».')
    def add(db):
        new_record(db,'metrics',kind=d['kind'],v1=d['v1'],v2=d['v2'])
        db['metrics'][-1]['at']=when.isoformat()
    await modify(m.from_user.id,add)
    await state.clear()
    await m.answer('✅ Показатель записан.',reply_markup=menu)

@router.message(Step.food)
async def food(m:Message,state:FSMContext):
    try:
        pieces=[x.strip() for x in m.text.split(';')]
        if len(pieces)!=5 or not pieces[0]:raise ValueError
        vals=list(map(number,pieces[1:]))
        if any(not 0<=n<=1000 for n in vals):raise ValueError
    except (ValueError,AttributeError):return await m.answer('Формат: Название; ккал; Б; Ж; У (на 100 г)')
    await state.update_data(title=pieces[0][:150],vals=vals)
    await state.set_state(Step.grams)
    await m.answer('Сколько граммов съели?')

@router.message(Step.grams)
async def grams(m:Message,state:FSMContext):
    try:
        g=number(m.text)
        if not 0<g<=10000:raise ValueError
    except (ValueError,AttributeError):return await m.answer('Введите количество граммов, например 150')
    data=await state.get_data()
    v=[round(x*g/100,1) for x in data['vals']]
    await modify(m.from_user.id,lambda d:new_record(d,'foods',title=data['title'],grams=g,calories=v[0],protein=v[1],fat=v[2],carbs=v[3]))
    await state.clear()
    await m.answer(f'✅ Сохранено: {v[0]} ккал · Б {v[1]} · Ж {v[2]} · У {v[3]}',reply_markup=menu)

@router.message(Step.workout)
async def workout(m:Message,state:FSMContext):
    try:
        a=[x.strip() for x in m.text.split(';')]
        if len(a) not in (2,3) or not a[0]:raise ValueError
        mins=int(a[1]);kc=number(a[2]) if len(a)==3 and a[2] else None
        if not 1<=mins<=1440 or (kc is not None and not 0<=kc<=10000):raise ValueError
    except (ValueError,AttributeError):return await m.answer('Формат: Ходьба; 40; 150 (ккал можно пропустить)')
    await modify(m.from_user.id,lambda d:new_record(d,'workouts',title=a[0][:150],minutes=mins,calories=kc))
    await state.clear();await m.answer('✅ Тренировка сохранена.',reply_markup=menu)

@router.message(Step.reminder)
async def reminder(m:Message,state:FSMContext):
    import re
    a=[x.strip() for x in (m.text or '').split(';',1)]
    if len(a)!=2 or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',a[0]) or not a[1] or len(a[1])>200:
        return await m.answer('Формат: 09:00; Принять лекарство')
    await modify(m.from_user.id,lambda d:new_record(d,'reminders',title=a[1],local_time=a[0],active=True,last_sent_date=None))
    await state.clear();await m.answer('✅ Напоминание создано.',reply_markup=menu)
