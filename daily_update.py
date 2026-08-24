"""
Ежедневный скрипт для всех магазинов:
  1. Дописывает новые заказы в лист 'orders' (накопительно)
  2. Дописывает текущие цены в лист 'prices' (история цен)

Запускается для каждого магазина отдельно через переменные STORE_TOKEN и STORE_SPREADSHEET_ID.
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# ── Авторизация ──────────────────────────────────────────────────
WB_TOKEN = os.environ['STORE_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
STATS_URL  = 'https://statistics-api.wildberries.ru'
PRICES_URL = 'https://discounts-prices-api.wildberries.ru'

STORE_NAME = os.environ.get('STORE_NAME', 'unknown')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['STORE_SPREADSHEET_ID'])

TODAY = datetime.now().strftime('%Y-%m-%d')
ORDERS_DATE_FROM = os.environ.get('ORDERS_DATE_FROM', '').strip() or \
    (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

print(f"{'='*50}")
print(f"Магазин: {STORE_NAME} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*50}")

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

ORDERS_HEADERS = ['Дата заказа', 'Артикул поставщика', 'Название', 'Категория',
                  'Бренд', 'Цена, ₽', 'Цена со скидкой, ₽', 'Склад', 'Регион',
                  'Статус отмены', 'srid', 'nmID']

try:
    ws_orders = sh.worksheet('orders')
    existing_orders = ws_orders.get_all_values()
    if not existing_orders:
        ws_orders.append_row(ORDERS_HEADERS)
        existing_srids = set()
    else:
        existing_srids = set(row[10] for row in existing_orders[1:] if len(row) > 10 and row[10])
    print(f"  Уже в таблице: {len(existing_srids)} заказов")
except Exception:
    ws_orders = sh.add_worksheet(title='orders', rows=100000, cols=12)
    ws_orders.append_row(ORDERS_HEADERS)
    ws_orders.format('A1:L1', {
        'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
    })
    ws_orders.freeze(rows=1, cols=2)
    existing_srids = set()
    print("  Лист 'orders' создан")

print(f"  Период загрузки: с {ORDERS_DATE_FROM}")
result = wb_get(f'{STATS_URL}/api/v1/supplier/orders', params={
    'dateFrom': ORDERS_DATE_FROM + 'T00:00:00',
    'flag': 0
})

all_orders = []
if result:
    all_orders = result if isinstance(result, list) else result.get('orders', [])
    print(f"  Получено заказов из API: {len(all_orders)}")

new_orders = []
for order in all_orders:
    srid = str(order.get('srid', '') or order.get('odid', ''))
    if srid not in existing_srids:
        date = str(order.get('date', '') or order.get('dateCreated', ''))[:10]
        vendor = order.get('supplierArticle', '')
        nm_id = order.get('nmId', '')
        name = order.get('subject', '')
        category = order.get('category', '')
        brand = order.get('brand', '')
        price = order.get('totalPrice', 0) or 0
        price_disc = order.get('priceWithDisc', 0) or 0
        warehouse = order.get('warehouseName', '')
        region = order.get('regionName', '') or order.get('oblast', '')
        is_cancel = order.get('isCancel', False)
        cancel_dt = order.get('cancelDt', '')
        status = 'Отменён' if is_cancel or cancel_dt else 'Активен'
        new_orders.append([date, vendor, name, category, brand,
                           price, price_disc, warehouse, region,
                           status, srid, nm_id])

print(f"  Новых заказов: {len(new_orders)}")

if new_orders:
    new_orders.sort(key=lambda x: x[0])
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
    all_goods = []
    limit = 1000
    offset = 0
    while True:
        result = wb_get(f'{PRICES_URL}/api/v2/list/goods/filter', params={
            'limit': limit, 'offset': offset,
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
            price = sizes[0].get('price', 0) if sizes else item.get('price', 0)
            discount = item.get('discount', 0) or 0
            price_after = round((price or 0) * (1 - discount / 100), 2)
            price_rows.append([TODAY, vendor_code, nm_id, round(price or 0, 2), discount, price_after])

        batch_size = 2000
        for i in range(0, len(price_rows), batch_size):
            batch = price_rows[i:i+batch_size]
            ws_prices.append_rows(batch, value_input_option='USER_ENTERED')
            print(f"  Записано строк {i+1}–{i+len(batch)}")
            time.sleep(1)
        print(f"✅ Цены: записано {len(price_rows)} артикулов")
    else:
        print("❌ Цены не получены")

print(f"\n✅ Магазин {STORE_NAME} обновлён!")
