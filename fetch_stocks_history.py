"""
Каждый день снимает текущие остатки ФБО по всем артикулам и дописывает
в лист 'stocks_history'. Так накапливается история остатков по дням.

ОБНОВЛЕНО: старый метод GET /api/v1/supplier/stocks отключён Wildberries
(https://dev.wildberries.ru/release-notes?id=494). Теперь используется:

  POST https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses
  Категория токена: Analytics (Personal или Service)

ВАЖНО: новый метод возвращает только nmId (числовой ID WB) и chrtId —
БЕЗ артикула поставщика и названия. Поэтому скрипт сначала выкачивает
АКТУАЛЬНЫЙ справочник nmId → (артикул поставщика, название) через
Content API (список карточек товаров, все карточки, не только те,
что недавно заказывали):

  POST https://content-api.wildberries.ru/content/v2/get/cards/list
  Категория токена: Content

ДОБАВЛЕНО: остатки ФБС (свой склад, зарегистрированный в WB) —
дописываются в тот же лист, отдельными строками со Складом = "ФБС":

  GET  https://marketplace-api.wildberries.ru/api/v3/warehouses          — список складов ФБС
  POST https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouseId} — остатки по баркодам
  Категория токена: Marketplace

Лист 'stocks_history' (структура не менялась, чтобы старая история осталась совместима):
  A = Дата снятия
  B = Артикул поставщика
  C = nmID
  D = Название
  E = Склад
  F = Количество
  G = В пути к клиенту
  H = В пути от клиента

Запуск:
  export WB_TOKEN='...'              (токен с категориями Analytics, Content И Marketplace!)
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_stocks_history.py
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# ── Авторизация ──────────────────────────────────────────────────
WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
ANALYTICS_URL = 'https://seller-analytics-api.wildberries.ru'
CONTENT_URL = 'https://content-api.wildberries.ru'
MARKETPLACE_URL = 'https://marketplace-api.wildberries.ru'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])

TODAY = datetime.now().strftime('%Y-%m-%d')
print(f"Дата снятия остатков: {TODAY}")


# ── Вспомогательная функция для GET с ретраями ────────────────────
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


# ── Вспомогательная функция для POST с ретраями ──────────────────
def wb_post(url, body, retries=5):
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, json=body, timeout=60)
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


# ── 1. Справочник nmId -> (артикул, название) через Content API ──
print("\n→ Шаг 1: Выкачиваем актуальный список карточек товаров (Content API)...")

nm_to_article = {}
barcode_to_nm = {}  # для сопоставления остатков ФБС (по баркоду) обратно к артикулу
cursor = {"limit": 100}
page = 0
while True:
    page += 1
    body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
    resp = wb_post(f'{CONTENT_URL}/content/v2/get/cards/list', body)
    if not resp:
        print(f"❌ Не удалось получить список карточек (страница {page}) — выходим")
        exit(1)

    cards = resp.get('cards', [])
    for c in cards:
        nm_id = str(c.get('nmID', ''))
        vendor_code = (c.get('vendorCode') or '').strip()
        title = (c.get('title') or c.get('subjectName') or '').strip()
        if nm_id:
            nm_to_article[nm_id] = (vendor_code, title)
            for size in c.get('sizes', []):
                for sku in size.get('skus', []):
                    barcode_to_nm[sku] = nm_id

    next_cursor = resp.get('cursor', {})
    total = next_cursor.get('total', 0)
    print(f"  Страница {page}: получено {len(cards)} карточек (total={total})")

    if total < cursor['limit']:
        break
    cursor = {
        "limit": 100,
        "updatedAt": next_cursor.get('updatedAt'),
        "nmID": next_cursor.get('nmID'),
    }
    time.sleep(0.3)

print(f"  Всего карточек в справочнике: {len(nm_to_article)}")

# ── 2. Получаем остатки ФБО по новому методу ─────────────────────
print("\n→ Шаг 2: Получаем текущие остатки ФБО (новый Analytics-метод)...")

endpoint = f'{ANALYTICS_URL}/api/analytics/v1/stocks-report/wb-warehouses'
result = wb_post(endpoint, body={})

if not result:
    print("❌ Нет ответа от API — выходим")
    exit(1)

items = result.get('data', {}).get('items', [])
print(f"Итого строк остатков (по складам): {len(items)}")

if not items:
    print("❌ Остатки не получены — выходим")
    exit(1)

# ПРЕДУПРЕЖДЕНИЕ: если каталог сильно вырастет и WB введёт пагинацию
# в этом методе (курсор/лимит), этот блок придётся дополнить постраничным сбором.
# На момент написания (1128 строк) ответ пришёл одним пакетом без курсора.

# ── 3. Получаем остатки ФБС (свой склад) ──────────────────────────
print("\n→ Шаг 3: Получаем остатки ФБС (свой склад)...")

fbs_rows_extra = []
warehouses = wb_get(f'{MARKETPLACE_URL}/api/v3/warehouses')

if not warehouses:
    print("⚠️  Не удалось получить список складов ФБС (нет доступа Marketplace API "
          "или складов ФБС не заведено) — пропускаем блок ФБС")
else:
    print(f"  Складов ФБС найдено: {len(warehouses)}")
    all_barcodes = list(barcode_to_nm.keys())
    print(f"  Баркодов для проверки остатков: {len(all_barcodes)}")

    for wh in warehouses:
        wh_id = wh.get('id')
        wh_name = wh.get('name', f'ФБС {wh_id}')
        print(f"  → Склад ФБС '{wh_name}' (id={wh_id})")

        nm_quantity = {}  # nmId -> суммарный остаток по этому складу ФБС
        batch_size = 1000
        for i in range(0, len(all_barcodes), batch_size):
            batch = all_barcodes[i:i + batch_size]
            resp = wb_post(f'{MARKETPLACE_URL}/api/v3/stocks/{wh_id}', body={"skus": batch})
            if not resp:
                print(f"    ⚠️ Не удалось получить остатки для батча {i}-{i+len(batch)}")
                continue
            for s in resp.get('stocks', []):
                sku = s.get('sku', '')
                amount = s.get('amount', 0) or 0
                nm_id = barcode_to_nm.get(sku)
                if nm_id and amount:
                    nm_quantity[nm_id] = nm_quantity.get(nm_id, 0) + amount
            time.sleep(0.3)

        print(f"    Артикулов с остатком на этом складе: {len(nm_quantity)}")
        for nm_id, qty in nm_quantity.items():
            article, name = nm_to_article.get(nm_id, ('', ''))
            fbs_rows_extra.append([
                TODAY, article, nm_id, name, 'ФБС', qty, 0, 0,
            ])

print(f"  Строк ФБС для добавления: {len(fbs_rows_extra)}")

# ── 4. Формируем строки для записи ───────────────────────────────
print("\n→ Шаг 4: Формируем строки...")

rows = []
unmatched_nm = set()
for item in items:
    nm_id = str(item.get('nmId', ''))
    warehouse = item.get('warehouseName', '')
    quantity = item.get('quantity', 0) or 0
    in_way_to = item.get('inWayToClient', 0) or 0
    in_way_from = item.get('inWayFromClient', 0) or 0

    article, name = nm_to_article.get(nm_id, ('', ''))
    if not article:
        unmatched_nm.add(nm_id)

    rows.append([
        TODAY,
        article,
        nm_id,
        name,
        warehouse,
        quantity,
        in_way_to,
        in_way_from,
    ])

rows.extend(fbs_rows_extra)

print(f"Строк для записи (ФБО + ФБС): {len(rows)}")
if unmatched_nm:
    print(f"⚠️  Не нашли артикул для {len(unmatched_nm)} nmId "
          f"(нет такой карточки в Content API — возможно, товар удалён/в корзине). "
          f"Примеры: {list(unmatched_nm)[:10]}")

# ── 5. Дописываем в лист stocks_history ───────────────────────────
print("\n→ Шаг 5: Дописываем в Google Sheets...")

HEADERS_ROW = ['Дата', 'Артикул поставщика', 'nmID', 'Название',
               'Склад', 'Количество', 'В пути к клиенту', 'В пути от клиента']

try:
    ws = sh.worksheet('stocks_history')
    existing = ws.get_all_values()

    if not existing:
        ws.append_row(HEADERS_ROW)
        print("  Заголовок добавлен")
        existing_dates = set()
    else:
        existing_dates = set(row[0] for row in existing[1:] if row)

    print(f"  Дат в истории: {len(existing_dates)}")

except Exception:
    ws = sh.add_worksheet(title='stocks_history', rows=200000, cols=8)
    ws.append_row(HEADERS_ROW)
    existing_dates = set()
    print("  Лист 'stocks_history' создан")

if TODAY in existing_dates:
    print(f"⚠️  Остатки за {TODAY} уже записаны — пропускаем")
    exit(0)

batch_size = 2000
for i in range(0, len(rows), batch_size):
    batch = rows[i:i + batch_size]
    ws.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Записано строк {i+1}–{i+len(batch)}")
    time.sleep(1)

if not existing_dates:
    ws.format('A1:H1', {
        'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
    })
    ws.freeze(rows=1, cols=2)

print(f"\n✅ Готово!")
print(f"   Дата: {TODAY}")
print(f"   Строк остатков: {len(rows)}")
print(f"   Всего дней в истории: {len(existing_dates) + 1}")
