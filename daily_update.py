"""
Ежедневный скрипт для одного магазина:
  1. Дописывает новые заказы в лист 'orders' (накопительно)
  2. Дописывает текущие цены в лист 'prices' (история цен)

Лист 'orders':
  A = Дата заказа
  B = Артикул поставщика
  C = nmID
  D = Название
  E = Цена, ₽
  F = Склад
  G = Регион
  H = srid (уникальный ID заказа)

Лист 'prices':
  A = Дата снятия
  B = Артикул поставщика
  C = nmID
  D = Цена до скидки, ₽
  E = Скидка продавца, %
  F = Цена после скидки, ₽

Запуск:
  export WB_TOKEN='...'
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  export ORDERS_DATE_FROM='2026-06-25'  # опционально, иначе последние 7 дней
  pip install gspread google-auth requests
  python daily_update.py
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# ── Авторизация ──────────────────────────────────────────────────
WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
STATS_URL  = 'https://statistics-api.wildberries.ru'
PRICES_URL = 'https://discounts-prices-api.wildberries.ru'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])

TODAY = datetime.now().strftime('%Y-%m-%d')
ORDERS_DATE_FROM = os.environ.get('ORDERS_DATE_FROM', '').strip() or \
    (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

print(f"{'='*50}")
print(f"Daily Update | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*50}")

# ── Вспомогательные функции ──────────────────────────────────────
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
            print(f"  Ошибка {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"  Исключение: {e}")
            time.sleep(10)
    return None


# ================================================================
# 1. ЗАКАЗЫ
# ================================================================
print("\n→ Шаг 1: Загружаем заказы...")

ORDERS_HEADERS = ['Дата заказа', 'Артикул поставщика', 'nmID',
                  'Название', 'Цена, ₽', 'Склад', 'Регион', 'srid']

# Открываем или создаём лист orders
try:
    ws_orders = sh.worksheet('orders')
    existing_orders = ws_orders.get_all_values()
    if not existing_orders:
        ws_orders.append_row(ORDERS_HEADERS)
        existing_srids = set()
    else:
        # Собираем уже загруженные srid (колонка H = индекс 7)
        existing_srids = set(row[7] for row in existing_orders[1:] if len(row) > 7 and row[7])
    print(f"  Уже в таблице: {len(existing_srids)} заказов")
except Exception:
    ws_orders = sh.add_worksheet(title='orders', rows=100000, cols=8)
    ws_orders.append_row(ORDERS_HEADERS)
    ws_orders.format('A1:H1', {
        'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
    })
    ws_orders.freeze(rows=1, cols=2)
    existing_srids = set()
    print("  Лист 'orders' создан")

# Загружаем заказы из API
print(f"  Период загрузки: с {ORDERS_DATE_FROM}")
all_orders = []
date_from_dt = ORDERS_DATE_FROM + 'T00:00:00'

result = wb_get(f'{STATS_URL}/api/v1/supplier/orders', params={
    'dateFrom': date_from_dt,
    'flag': 0
})

if result:
    all_orders = result if isinstance(result, list) else result.get('orders', [])
    print(f"  Получено заказов из API: {len(all_orders)}")

# Фильтруем только новые (которых нет в таблице)
new_orders = []
for order in all_orders:
    srid = order.get('srid', '') or order.get('odid', '')
    if str(srid) not in existing_srids:
        date = str(order.get('date', '') or order.get('dateCreated', ''))[:10]
        vendor = order.get('supplierArticle', '')
        nm_id = order.get('nmId', '')
        name = order.get('subject', '') or order.get('category', '')
        price = order.get('totalPrice', 0) or order.get('priceWithDisc', 0) or 0
        warehouse = order.get('warehouseName', '')
        region = order.get('regionName', '') or order.get('oblast', '')
        new_orders.append([date, vendor, nm_id, name, price, warehouse, region, str(srid)])

print(f"  Новых заказов для записи: {len(new_orders)}")

if new_orders:
    # Сортируем по дате
    new_orders.sort(key=lambda x: x[0])
    # Дописываем батчами
    batch_size = 2000
    for i in range(0, len(new_orders), batch_size):
        batch = new_orders[i:i+batch_size]
        ws_orders.append_rows(batch, value_input_option='USER_ENTERED')
        print(f"  Записано строк {i+1}–{i+len(batch)}")
        time.sleep(1)
    print(f"✅ Заказы: добавлено {len(new_orders)} новых записей")
else:
    print("✅ Заказы: новых нет")


# ================================================================
# 2. ЦЕНЫ
# ================================================================
print("\n→ Шаг 2: Снимаем текущие цены...")

PRICES_HEADERS = ['Дата', 'Артикул поставщика', 'nmID',
                  'Цена до скидки, ₽', 'Скидка, %', 'Цена после скидки, ₽']

# Открываем или создаём лист prices
try:
    ws_prices = sh.worksheet('prices')
    existing_prices = ws_prices.get_all_values()
    if not existing_prices:
        ws_prices.append_row(PRICES_HEADERS)
        existing_dates = set()
    else:
        existing_dates = set(row[0] for row in existing_prices[1:] if row)
    print(f"  Дат в истории: {len(existing_dates)}")
except Exception:
    ws_prices = sh.add_worksheet(title='prices', rows=100000, cols=6)
    ws_prices.append_row(PRICES_HEADERS)
    ws_prices.format('A1:F1', {
        'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
    })
    ws_prices.freeze(rows=1, cols=2)
    existing_dates = set()
    print("  Лист 'prices' создан")

if TODAY in existing_dates:
    print(f"⚠️  Цены за {TODAY} уже записаны — пропускаем")
else:
    # Загружаем цены из API
    all_goods = []
    limit = 1000
    offset = 0

    while True:
        result = wb_get(f'{PRICES_URL}/api/v2/list/goods/filter', params={
            'limit': limit,
            'offset': offset,
        })
        if not result:
            break
        goods = result.get('data', {}).get('listGoods', [])
        if not goods:
            break
        all_goods.extend(goods)
        print(f"  offset={offset}: получено {len(goods)}, всего: {len(all_goods)}")
        if len(goods) < limit:
            break
        offset += limit
        time.sleep(0.5)

    print(f"  Итого артикулов: {len(all_goods)}")

    if all_goods:
        price_rows = []
        for item in all_goods:
            nm_id = item.get('nmID', '')
            vendor_code = item.get('vendorCode', '')
            sizes = item.get('sizes', [])
            if sizes:
                price = sizes[0].get('price', 0) or 0
                discount = item.get('discount', 0) or 0
            else:
                price = item.get('price', 0) or 0
                discount = item.get('discount', 0) or 0
            price_after = round(price * (1 - discount / 100), 2)
            price_rows.append([TODAY, vendor_code, nm_id, round(price, 2), discount, price_after])

        batch_size = 2000
        for i in range(0, len(price_rows), batch_size):
            batch = price_rows[i:i+batch_size]
            ws_prices.append_rows(batch, value_input_option='USER_ENTERED')
            print(f"  Записано строк {i+1}–{i+len(batch)}")
            time.sleep(1)

        print(f"✅ Цены: записано {len(price_rows)} артикулов")
    else:
        print("❌ Цены не получены")

print(f"\n{'='*50}")
print(f"✅ Готово! {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*50}")
