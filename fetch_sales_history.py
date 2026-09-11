"""
Тянет фактические продажи (выкупы, не заказы) по артикулам через WB Sales API
и дописывает в лист 'sales_history'. Простая версия — только сумма продаж,
без разбивки на комиссии/логистику/хранение (это отдельная задача на потом).

Используется:
  GET https://statistics-api.wildberries.ru/api/v1/supplier/sales
  Категория токена: Statistics (та же, что уже используется для 'orders')

ВАЖНО про dateFrom: этот метод отдаёт все продажи/возвраты, у которых
lastChangeDate >= dateFrom — то есть не только НОВЫЕ записи, но и СТАРЫЕ,
если WB их задним числом поменял (например, статус сменился). Поэтому для
ежедневного крона берём dateFrom = 3 дня назад (небольшой нахлёст), а не
строго "сегодня" — так подхватываются поздние изменения. Дедупликация — по
уникальному saleID, старая запись просто перезатирается новой при повторном
запуске (не задваивается).

Как отличить продажу от возврата: WB кодирует это в самом saleID — если
начинается с 'S' это продажа, если начинается с 'R' — возврат. Это
официальная договорённость по всем интеграциям WB (используется всеми
известными кейсами работы с этим методом), а не догадка.

Для разового бэкафилла (например, 2 недели) задай:
  export SALES_DATE_FROM='2026-08-11'
  python fetch_sales_history.py

Лист 'sales_history':
  A = Дата продажи
  B = Артикул поставщика
  C = nmID
  D = saleID (уникальный ключ строки — для дедупликации)
  E = Тип (Продажа / Возврат)
  F = Кол-во (всегда 1 на строку, WB отдаёт по штучно)
  G = Цена со скидкой продавца, ₽ (priceWithDisc)
  H = К перечислению, ₽ (forPay)

Запуск:
  export WB_TOKEN='...'              (токен с категорией Statistics)
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_sales_history.py
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
STATS_URL = 'https://statistics-api.wildberries.ru'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])

TODAY = datetime.now()
DATE_FROM = os.environ.get('SALES_DATE_FROM', '').strip() or \
    (TODAY - timedelta(days=3)).strftime('%Y-%m-%d')
print(f"dateFrom для запроса: {DATE_FROM} (нахлёст 3 дня для ежедневного крона, "
      f"если не задан SALES_DATE_FROM вручную)")


def wb_get(url, params=None, retries=5):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=60)
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


# ── 1. Получаем продажи/возвраты ──────────────────────────────────
print("\n→ Шаг 1: Получаем данные Sales API...")

result = wb_get(f'{STATS_URL}/api/v1/supplier/sales', params={
    'dateFrom': DATE_FROM,
    'flag': 0,  # 0 = все продажи с lastChangeDate >= dateFrom (не только за конкретный день)
})

if result is None:
    print("❌ Нет ответа от API — выходим")
    exit(1)

print(f"Итого записей: {len(result)}")

if not result:
    print("⚠️  Пусто за этот период — писать нечего, выходим без ошибки")
    exit(0)

# ── 2. Формируем строки ───────────────────────────────────────────
print("\n→ Шаг 2: Формируем строки...")

rows = []
for item in result:
    sale_id = item.get('saleID', '')
    if not sale_id:
        continue
    tip = 'Возврат' if sale_id.upper().startswith('R') else 'Продажа'
    date = str(item.get('date', ''))[:10]
    article = (item.get('supplierArticle') or '').strip()
    nm_id = item.get('nmId', '')
    price_with_disc = item.get('priceWithDisc', 0) or 0
    for_pay = item.get('forPay', 0) or 0

    rows.append([
        date, article, nm_id, sale_id, tip, 1,
        round(price_with_disc, 2), round(for_pay, 2),
    ])

print(f"Строк для записи: {len(rows)}")
print(f"  Продаж: {sum(1 for r in rows if r[4] == 'Продажа')}")
print(f"  Возвратов: {sum(1 for r in rows if r[4] == 'Возврат')}")

# ── 3. Дописываем в лист sales_history (дедуп по saleID) ──────────
print("\n→ Шаг 3: Записываем в Google Sheets (дедупликация по saleID)...")

HEADERS_ROW = ['Дата', 'Артикул поставщика', 'nmID', 'saleID', 'Тип',
               'Кол-во', 'Цена со скидкой, ₽', 'К перечислению, ₽']

try:
    ws = sh.worksheet('sales_history')
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(HEADERS_ROW)
        existing_sale_ids = {}
    else:
        # saleID -> номер строки (для перезаписи, если запись изменилась)
        existing_sale_ids = {row[3]: i + 2 for i, row in enumerate(existing[1:]) if len(row) > 3 and row[3]}
    print(f"  Уже было записей: {len(existing_sale_ids)}")
except Exception:
    ws = sh.add_worksheet(title='sales_history', rows=300000, cols=8)
    ws.append_row(HEADERS_ROW)
    existing_sale_ids = {}
    print("  Лист 'sales_history' создан")

new_rows = []
updates = []  # (row_number, row_values) — для записей, которые уже были, но изменились
for r in rows:
    sale_id = r[3]
    if sale_id in existing_sale_ids:
        updates.append((existing_sale_ids[sale_id], r))
    else:
        new_rows.append(r)

print(f"  Новых записей: {len(new_rows)}, обновлений существующих: {len(updates)}")

if new_rows:
    batch_size = 2000
    for i in range(0, len(new_rows), batch_size):
        batch = new_rows[i:i + batch_size]
        ws.append_rows(batch, value_input_option='USER_ENTERED')
        print(f"  Дописано новых строк {i+1}–{i+len(batch)}")
        time.sleep(1)

# Обновления существующих записей — точечно, по номеру строки
for row_num, row_values in updates:
    ws.update(f'A{row_num}:H{row_num}', [row_values], value_input_option='USER_ENTERED')
    time.sleep(0.2)
if updates:
    print(f"  Обновлено существующих строк: {len(updates)}")

if not existing_sale_ids and not new_rows == []:
    ws.format('A1:H1', {
        'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
    })
    ws.freeze(rows=1, cols=2)

print(f"\n✅ Готово!")
print(f"   dateFrom: {DATE_FROM}")
print(f"   Новых строк: {len(new_rows)}, обновлено: {len(updates)}")
