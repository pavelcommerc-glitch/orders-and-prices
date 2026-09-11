"""
Каждый день снимает статистику рекламных кампаний (показы, клики, CTR, CPC,
расход, заказы из рекламы) и дописывает в лист 'ads_history'. Аналог
fetch_stocks_history.py, только источник — WB Promotion (Advertising) API.

Используется:
  GET  https://advert-api.wildberries.ru/adv/v1/promotion/count   — список кампаний
  GET  https://advert-api.wildberries.ru/adv/v3/fullstats          — статистика по дням/артикулам
  Категория токена: Promotion (Продвижение)

ВАЖНО про лимиты: /adv/v3/fullstats — всего 3 запроса в минуту, с интервалом
не чаще 1 раза в 20 секунд (burst 1). Поэтому кампании собираются пачками по
50 (максимум за раз), и между пачками скрипт специально ждёт.

nmId -> артикул сопоставляется через те же кэш-листы 'nomenclature'/'barcodes',
что пишет fetch_stocks_history.py — этот скрипт их только ЧИТАЕТ, не трогает.
Так что fetch_stocks_history.py должен был отработать хотя бы раз раньше.

Лист 'ads_history':
  A = Дата
  B = Артикул поставщика
  C = nmID
  D = Название
  E = Показы
  F = Клики
  G = CTR, %
  H = CPC, ₽
  I = Заказы (из рекламы)
  J = Сумма заказов из рекламы, ₽
  K = Расход на рекламу, ₽

Запуск:
  export WB_TOKEN='...'              (токен с категорией Promotion!)
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_ads_history.py
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
ADVERT_URL = 'https://advert-api.wildberries.ru'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])

TODAY = datetime.now().strftime('%Y-%m-%d')
print(f"Дата снятия рекламной статистики: {TODAY}")


def wb_get(url, params=None, retries=5):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  ⏳ 429 — жду {wait}с (попытка {attempt+1}/{retries})...")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                return r.json()
            print(f"  Ошибка {r.status_code}: {r.text[:300]}")
            return None
        except Exception as e:
            print(f"  Исключение: {e}")
            time.sleep(10)
    return None


# ── 0. Справочник nmId -> (артикул, название) из кэша nomenclature ──
print("\n→ Шаг 0: Читаем справочник 'nomenclature' (пишет fetch_stocks_history.py)...")

nm_to_article = {}
try:
    nom_ws = sh.worksheet('nomenclature')
    nom_data = nom_ws.get_all_values()[1:]
    for row in nom_data:
        if len(row) >= 3 and row[1]:
            article, nm_id, name = row[0], row[1], row[2]
            nm_to_article[nm_id] = (article, name)
    print(f"  Артикулов в справочнике: {len(nm_to_article)}")
except Exception as e:
    print(f"⚠️  Не удалось прочитать 'nomenclature': {e} — "
          f"названия/артикулы будут пустыми там, где не найдём по nmId")

# ── 1. Список кампаний ────────────────────────────────────────────
print("\n→ Шаг 1: Получаем список рекламных кампаний...")

campaigns_resp = wb_get(f'{ADVERT_URL}/adv/v1/promotion/count')
if not campaigns_resp:
    print("❌ Не удалось получить список кампаний — выходим")
    exit(1)

all_advert_ids = []
# fullstats поддерживает только кампании в статусах 7 (завершена), 9 (активна), 11 (на паузе)
RELEVANT_STATUSES = {7, 9, 11}
for group in campaigns_resp.get('adverts', []):
    status = group.get('status')
    for adv in group.get('advert_list', []):
        advert_id = adv.get('advertId')
        if advert_id and (status in RELEVANT_STATUSES):
            all_advert_ids.append(advert_id)

all_advert_ids = list(set(all_advert_ids))
print(f"  Кампаний для снятия статистики (статусы 7/9/11): {len(all_advert_ids)}")

if not all_advert_ids:
    print("⚠️  Нет кампаний в подходящем статусе — писать нечего, выходим без ошибки")
    exit(0)

# ── 2. Статистика по кампаниям (пачками по 50, с учётом лимита 3/мин) ──
print("\n→ Шаг 2: Получаем статистику (fullstats)...")

BATCH_SIZE = 50
SLEEP_BETWEEN_BATCHES = 22  # секунд — лимит 3 запроса/мин, интервал не чаще раза в 20с

# nm_id (str) -> агрегированные показатели за TODAY
agg = {}  # nm_id -> {views, clicks, orders, sum, sum_price}

batches = [all_advert_ids[i:i + BATCH_SIZE] for i in range(0, len(all_advert_ids), BATCH_SIZE)]
for bi, batch in enumerate(batches):
    ids_param = ','.join(str(x) for x in batch)
    print(f"  Пачка {bi+1}/{len(batches)}: {len(batch)} кампаний")
    resp = wb_get(f'{ADVERT_URL}/adv/v3/fullstats', params={
        'ids': ids_param,
        'beginDate': TODAY,
        'endDate': TODAY,
    })
    if resp:
        for campaign in resp:
            for day in campaign.get('days', []):
                for app in day.get('apps', []):
                    for nm in app.get('nms', []):
                        nm_id = str(nm.get('nmId', ''))
                        if not nm_id:
                            continue
                        if nm_id not in agg:
                            agg[nm_id] = {'views': 0, 'clicks': 0, 'orders': 0, 'sum': 0.0, 'sum_price': 0.0}
                        agg[nm_id]['views'] += nm.get('views', 0) or 0
                        agg[nm_id]['clicks'] += nm.get('clicks', 0) or 0
                        agg[nm_id]['orders'] += nm.get('orders', 0) or 0
                        agg[nm_id]['sum'] += nm.get('sum', 0) or 0
                        agg[nm_id]['sum_price'] += nm.get('sum_price', 0) or 0
    else:
        print(f"    ⚠️ Пачка {bi+1} не отдала данные (см. ошибку выше)")

    if bi < len(batches) - 1:
        print(f"    ждём {SLEEP_BETWEEN_BATCHES}с (лимит API)...")
        time.sleep(SLEEP_BETWEEN_BATCHES)

print(f"  Артикулов с рекламной статистикой за {TODAY}: {len(agg)}")

# ── 3. Формируем строки ───────────────────────────────────────────
print("\n→ Шаг 3: Формируем строки...")

rows = []
unmatched = set()
for nm_id, m in agg.items():
    article, name = nm_to_article.get(nm_id, ('', ''))
    if not article:
        unmatched.add(nm_id)
    ctr = round(m['clicks'] / m['views'] * 100, 2) if m['views'] else 0
    cpc = round(m['sum'] / m['clicks'], 2) if m['clicks'] else 0
    rows.append([
        TODAY, article, nm_id, name,
        m['views'], m['clicks'], ctr, cpc,
        m['orders'], round(m['sum_price'], 2), round(m['sum'], 2),
    ])

print(f"Строк для записи: {len(rows)}")
if unmatched:
    print(f"⚠️  Не нашли артикул для {len(unmatched)} nmId "
          f"(нет в кэше 'nomenclature' — запусти fetch_stocks_history.py заново). "
          f"Примеры: {list(unmatched)[:10]}")

# ── 4. Дописываем в лист ads_history ──────────────────────────────
print("\n→ Шаг 4: Дописываем в Google Sheets...")

HEADERS_ROW = ['Дата', 'Артикул поставщика', 'nmID', 'Название',
               'Показы', 'Клики', 'CTR, %', 'CPC, ₽',
               'Заказы (из рекламы)', 'Сумма заказов из рекламы, ₽', 'Расход на рекламу, ₽']

try:
    ws = sh.worksheet('ads_history')
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(HEADERS_ROW)
        existing_dates = set()
    else:
        existing_dates = set(row[0] for row in existing[1:] if row)
except Exception:
    ws = sh.add_worksheet(title='ads_history', rows=200000, cols=len(HEADERS_ROW))
    ws.append_row(HEADERS_ROW)
    existing_dates = set()
    print("  Лист 'ads_history' создан")

if TODAY in existing_dates:
    print(f"⚠️  Реклама за {TODAY} уже записана — пропускаем")
    exit(0)

batch_size = 2000
for i in range(0, len(rows), batch_size):
    batch = rows[i:i + batch_size]
    ws.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Записано строк {i+1}–{i+len(batch)}")
    time.sleep(1)

if not existing_dates:
    ws.format('A1:K1', {
        'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
    })
    ws.freeze(rows=1, cols=2)

print(f"\n✅ Готово!")
print(f"   Дата: {TODAY}")
print(f"   Строк рекламной статистики: {len(rows)}")
