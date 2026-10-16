"""Authenticated Telegram Mini App HTTP API. Single-process JSON store shared with bot."""
import hashlib, hmac, json, os, time
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from bot import read, modify, new_record, now, tz_for

BASE = Path(__file__).resolve().parent
app = FastAPI(title='Health Tracker Mini App API', docs_url=None, redoc_url=None)
PRODUCTS = json.loads((BASE/'products.json').read_text(encoding='utf-8'))


def signed_user(init_data: str) -> int:
    if not init_data or len(init_data) > 16384:
        raise HTTPException(401, 'Откройте приложение из Telegram')
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received = pairs.pop('hash')
        auth_date = int(pairs['auth_date'])
        current = int(time.time())
        if not (0 <= current-auth_date <= 86400):
            raise ValueError('Expired')
        msg = '\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs))
        token = os.environ['BOT_TOKEN']
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received, expected):
            raise ValueError('Invalid signature')
        uid = int(json.loads(pairs['user'])['id'])
        if uid <= 0:
            raise ValueError('Invalid user')
        return uid
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(401, 'Недействительная авторизация Telegram')


def auth(x_telegram_init_data: str = Header(default='')):
    return signed_user(x_telegram_init_data)

from fastapi import Depends

class Entry(BaseModel):
    kind: str
    at: datetime | None = None
    v1: float | None = Field(default=None, ge=0, le=10000)
    v2: float | None = Field(default=None, ge=0, le=10000)
    product_id: str | None = None
    title: str | None = Field(default=None, max_length=150)
    grams: float | None = Field(default=None, gt=0, le=10000)
    calories: float | None = Field(default=None, ge=0, le=10000)
    protein: float | None = Field(default=None, ge=0, le=1000)
    fat: float | None = Field(default=None, ge=0, le=1000)
    carbs: float | None = Field(default=None, ge=0, le=1000)
    minutes: int | None = Field(default=None, ge=1, le=1440)

class Reminder(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    local_time: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')

@app.get('/')
async def index():
    return FileResponse(BASE/'web'/'index.html')

@app.get('/api/products')
async def products(uid: int = Depends(auth)):
    return PRODUCTS

@app.get('/api/me')
async def me(uid: int = Depends(auth)):
    d = await read(uid)
    return {'timezone':d.get('timezone'), 'metrics':d['metrics'], 'foods':d['foods'],
            'workouts':d['workouts'], 'reminders':d['reminders'], 'now':now().isoformat()}

@app.post('/api/entry')
async def entry(e: Entry, uid: int = Depends(auth)):
    timestamp = e.at.astimezone(timezone.utc).isoformat() if e.at else now().isoformat()
    if e.at and (e.at > now() or e.at.year < 2000):
        raise HTTPException(400, 'Дата должна быть в прошлом, начиная с 2000 года')
    if e.kind in ('weight', 'pulse', 'pressure'):
        if e.v1 is None or (e.kind == 'pressure' and e.v2 is None):
            raise HTTPException(400, 'Укажите показатели')
        bounds = {'weight':(1,500), 'pulse':(20,250),'pressure':(40,300)}
        lo,hi=bounds[e.kind]
        if not (lo <= e.v1 <= hi) or (e.kind=='pressure' and not 20<=e.v2<=200):
            raise HTTPException(400,'Значение вне допустимого диапазона')
        if e.kind=='pressure' and e.v1<=e.v2:
            raise HTTPException(400,'Верхнее давление должно быть больше нижнего')
        group,fields='metrics',dict(kind=e.kind,v1=e.v1,v2=e.v2)
    elif e.kind=='food':
        if e.grams is None: raise HTTPException(400,'Укажите граммы')
        product = next((p for p in PRODUCTS if p['id']==e.product_id),None)
        if product:
            vals={k: round(product[k]*e.grams/100,1) for k in ('calories','protein','fat','carbs')}
            title=product['name']
        else:
            if not e.title or any(getattr(e,k) is None for k in ('calories','protein','fat','carbs')):
                raise HTTPException(400,'Выберите продукт или заполните КБЖУ на 100 г')
            title=e.title
            vals={k:round(getattr(e,k)*e.grams/100,1) for k in ('calories','protein','fat','carbs')}
        group,fields='foods',dict(title=title,grams=e.grams,**vals)
    elif e.kind=='workout':
        if not e.title or e.minutes is None:
            raise HTTPException(400,'Укажите занятие и длительность')
        group,fields='workouts',dict(title=e.title,minutes=e.minutes,calories=e.calories)
    else:
        raise HTTPException(400,'Неизвестный тип записи')
    def store(d):
        rid=new_record(d,group,**fields)
        d[group][-1]['at']=timestamp
        return rid
    rid=await modify(uid,store)
    return {'id':rid, 'ok':True}

@app.post('/api/reminder')
async def create_reminder(r: Reminder, uid: int = Depends(auth)):
    rid=await modify(uid,lambda d:new_record(d,'reminders', title=r.title,local_time=r.local_time,active=True,last_sent_date=None))
    return {'id':rid,'ok':True}

@app.delete('/api/entry/{group}/{rid}')
async def delete_entry(group: str, rid: int, uid: int = Depends(auth)):
    if group not in ('metrics','foods','workouts','reminders'):
        raise HTTPException(400, 'Неизвестный раздел')
    def remove(d):
        for i,row in enumerate(d[group]):
            if row['id']==rid:
                d[group].pop(i)
                return True
        return False
    if not await modify(uid,remove):
        raise HTTPException(404,'Запись не найдена')
    return {'ok':True}
