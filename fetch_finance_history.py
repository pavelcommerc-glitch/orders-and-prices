"""
Тянет детализированный отчёт о реализации (полная финансовая разбивка —
комиссии, эквайринг, логистика, хранение, штрафы, удержания и т.д.) в лист
'finance'.

ОБНОВЛЕНО: старый метод GET /api/v5/supplier/reportDetailByPeriod отключён
Wildberries (объявлено к отключению 15 июля 2026, отключали постепенно —
сначала жёсткие 429, затем 404). Новый метод:

  POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed
  Категория токена: "Финансы" (НЕ Statistics — проверь в личном кабинете WB,
  что она включена, иначе 401/403)

Поля в ответе теперь camelCase, суммы приходят СТРОКАМИ. В сам лист 'finance'
пишем те же русские названия колонок, что и раньше — старые формулы в
Apps Script ("по_артикулам", "расчет") трогать не нужно, меняется только
здесь маппинг JSON-полей.

Ключевые переименования по сравнению со старым API:
  supplier_oper_name → sellerOperName (Обоснование для оплаты)
  sa_name            → vendorCode     (Артикул поставщика)
  ppvz_vw            → ppvzReward     (Вознаграждение ВВ)
  acquiring_bank_commission → acquiringFee (Эквайринг — раньше было всегда
                                             пусто в старом API, в новом реально
                                             заполняется)
  storage_fee        → paidStorage
  acceptance         → paidAcceptance
  rrd_id             → rrdId

ВАЖНО: отчёт о реализации у WB НЕ финальный сразу после публикации — WB ещё
1-2 недели дозаполняет и правит его задним числом. Поэтому:
  - Всё, что СТАРШЕ 14 дней — считаем окончательным, не трогаем (append-only).
  - Последние 14 дней — при КАЖДОМ запуске выбрасываем то, что уже было
    записано за этот период, и выкачиваем этот кусок ЗАНОВО целиком.

Пагинация: по аналогии со старым методом пробуем rrdId как курсор в теле
запроса. Если в какой-то момент WB поменяет схему пагинации — это будет
видно по тому, что limit постоянно возвращает одно и то же (rrdId не растёт).

Запуск:
  export WB_TOKEN='...'              (токен с категорией "Финансы"!)
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_finance_history.py
"""

import os
import time
import requests
import gspread
import json
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
FINANCE_URL = 'https://finance-api.wildberries.ru'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])

FIRST_RUN_DATE_FROM = os.environ.get('FINANCE_DATE_FROM', '').strip() or '2026-05-01'
DATE_TO = datetime.now().strftime('%Y-%m-%d')
REWRITE_WINDOW_DAYS = 14
rewrite_cutoff = (datetime.now() - timedelta(days=REWRITE_WINDOW_DAYS)).strftime('%Y-%m-%d')


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
            if r.status_code == 204:
                return []
            print(f"  Ошибка {r.status_code}: {r.text[:300]}")
            return None
        except Exception as e:
            print(f"  Исключение: {e}")
            time.sleep(10)
    return None


def num(v):
    """Деньги приходят строками — аккуратно приводим к числу."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


# Те же русские заголовки, что были раньше — старые формулы их не заметят
FINANCE_HEADERS = [
    "rrd_id", "Номер поставки", "Предмет", "Код номенклатуры", "Бренд", "Артикул поставщика",
    "Название", "Размер", "Баркод", "Тип документа", "Обоснование для оплаты",
    "Дата заказа покупателем", "Дата продажи", "Кол-во", "Цена розничная",
    "Вайлдберриз реализовал Товар (Пр)", "Размер кВВ, %", "Итоговый кВВ без НДС, %",
    "Вознаграждение с продаж до вычета услуг поверенного, без НДС",
    "Вознаграждение Вайлдберриз (ВВ), без НДС", "НДС с Вознаграждения Вайлдберриз",
    "К перечислению Продавцу за реализованный Товар",
    "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов",
    "Размер компенсации платёжных услуг/Комиссии за интеграцию платёжных сервисов, %",
    "Услуги по доставке товара покупателю", "Общая сумма штрафов",
    "Корректировка Вознаграждения Вайлдберриз (ВВ)",
    "Хранение", "Удержания", "Операции на приемке",
    "Склад", "Страна", "Номер таможенной декларации", "Srid", "Номер отчёта",
    "Период отчёта — с", "Период отчёта — по",
]


def row_from_item(item):
    return [
        item.get("rrdId", ""),
        item.get("giId", ""),
        item.get("subjectName", ""),
        item.get("nmId", ""),
        item.get("brandName", ""),
        item.get("vendorCode", ""),
        item.get("title", ""),
        item.get("techSize", ""),
        item.get("sku", ""),
        item.get("docTypeName", ""),
        item.get("sellerOperName", ""),
        item.get("orderDt", "")[:10] if item.get("orderDt") else "",
        item.get("saleDt", "")[:10] if item.get("saleDt") else "",
        item.get("quantity", 0),
        num(item.get("retailPrice", 0)),
        num(item.get("retailAmount", 0)),
        item.get("kvw", 0),  # Размер кВВ, %
        item.get("kvwBase", 0),  # используем как "Итоговый кВВ" пока не проверим точнее
        num(item.get("ppvzSalesCommission", 0)),
        num(item.get("ppvzReward", 0)),
        num(item.get("vwNds", 0)),
        num(item.get("forPay", 0)),
        num(item.get("acquiringFee", 0)),
        item.get("acquiringPercent", 0),
        num(item.get("deliveryAmount", 0)),
        num(item.get("penalty", 0)),
        num(item.get("additionalPayment", 0)),
        num(item.get("paidStorage", 0)),
        num(item.get("deduction", 0)),
        num(item.get("paidAcceptance", 0)),
        item.get("officeName", ""),
        item.get("country", ""),
        item.get("declarationNumber", ""),
        item.get("orderId", ""),
        item.get("reportId", ""),
        item.get("dateFrom", ""),
        item.get("dateTo", ""),
    ]


# ── 1. Лист 'finance' + разделяем на "старое" и "окно последних 14 дней" ──
print("\n→ Шаг 1: Проверяем лист 'finance'...")
print(f"  Окно перезаписи: последние {REWRITE_WINDOW_DAYS} дней (с {rewrite_cutoff})")

try:
    ws = sh.worksheet('finance')
except Exception:
    ws = None

is_first_run = ws is None or ws.acell('A1').value is None

if is_first_run:
    if ws is None:
        ws = sh.add_worksheet(title='finance', rows=100, cols=len(FINANCE_HEADERS))
    ws.append_row(FINANCE_HEADERS)
    keep_rows = []
    date_from = FIRST_RUN_DATE_FROM
    print(f"  Лист новый/пустой — первый запуск, период с {date_from}")
else:
    all_values = ws.get_all_values()
    existing_headers = all_values[0]
    data_rows = all_values[1:]
    sale_date_idx = existing_headers.index('Дата продажи') if 'Дата продажи' in existing_headers else 12
    old_kept = [r for r in data_rows if len(r) > sale_date_idx and r[sale_date_idx] < rewrite_cutoff]
    dropped = len(data_rows) - len(old_kept)

    # ВАЖНО: старые строки могли быть записаны ещё старым скриптом (82 колонки,
    # другой порядок), а сейчас пишем 36 колонок. Просто взять старые строки
    # "как есть" и положить их под новые заголовки — колонки разъедутся
    # (например, "Хранение" у старых строк окажется не там, где у новых).
    # Поэтому переупаковываем КАЖДУЮ старую строку под НОВУЮ схему — по
    # названию колонки, а не по позиции. Того, чего нет в старой схеме
    # (например, "Итоговый кВВ без НДС, %" мог называться иначе или вообще
    # отсутствовать) — оставляем пустым, не подставляем наугад.
    if existing_headers == FINANCE_HEADERS:
        # Уже в актуальной схеме (например, дозапись после сегодняшнего
        # запуска) — переупаковывать не нужно.
        keep_rows = old_kept
    else:
        old_idx = {name: i for i, name in enumerate(existing_headers)}
        keep_rows = []
        for r in old_kept:
            new_row = []
            for col_name in FINANCE_HEADERS:
                i = old_idx.get(col_name)
                new_row.append(r[i] if i is not None and i < len(r) else '')
            keep_rows.append(new_row)
        print(f"  Заголовки листа отличаются от текущей схемы — переупаковал "
              f"{len(keep_rows)} старых строк по названиям колонок")

    print(f"  Было строк: {len(data_rows)}. Оставляем (дата продажи < {rewrite_cutoff}): {len(keep_rows)}. "
          f"Убираем на переперезабор: {dropped}")
    date_from = rewrite_cutoff

# ── 2. Тянем данные через НОВЫЙ метод (POST, camelCase, sales-reports) ──
print(f"\n→ Шаг 2: Забираем строки за окно (dateFrom={date_from}, dateTo={DATE_TO})...")

all_fetched_rows = []
rrd_id_cursor = 0
seen_ids = set()
api_failed = False

while True:
    body = {'dateFrom': date_from, 'dateTo': DATE_TO, 'limit': 100000}
    if rrd_id_cursor:
        body['rrdId'] = rrd_id_cursor

    data = wb_post(f'{FINANCE_URL}/api/finance/v1/sales-reports/detailed', body)
    if data is None:
        print("❌ Нет ответа от API — прерываем на том, что уже собрали")
        api_failed = True
        break
    if not data:
        print("  Строк больше нет")
        break

    new_in_batch = 0
    batch_max_rrd = rrd_id_cursor
    for item in data:
        rid = item.get('rrdId')
        if rid is None or rid in seen_ids:
            continue
        seen_ids.add(rid)
        all_fetched_rows.append(row_from_item(item))
        new_in_batch += 1
        if isinstance(rid, (int, float)) and rid > batch_max_rrd:
            batch_max_rrd = rid

    print(f"  Получено {len(data)} строк, из них новых в этой пачке: {new_in_batch} "
          f"(всего собрано: {len(all_fetched_rows)})")

    if new_in_batch == 0 or len(data) < 100000:
        break
    if batch_max_rrd <= rrd_id_cursor:
        print("  ⚠️ Курсор не сдвинулся — останавливаемся, чтобы не зациклиться")
        break
    rrd_id_cursor = batch_max_rrd
    time.sleep(2)

print(f"\nИтого свежих строк за окно: {len(all_fetched_rows)}")

# ── ЗАЩИТА ОТ ПОТЕРИ ДАННЫХ ──────────────────────────────────────
if api_failed and not is_first_run:
    print("\n⛔ API не ответил (или ответил не полностью) за окно перезаписи.")
    print(f"   Собрано частично: {len(all_fetched_rows)} строк — недостаточно, чтобы быть")
    print("   уверенным, что окно забрано целиком. Лист НЕ трогаем. Запусти скрипт позже.")
    exit(1)

# ── 3. Полная перезапись: старое (что оставили) + свежее окно ──────
print("\n→ Шаг 3: Перезаписываем лист (старое без изменений + свежее окно)...")

final_rows = all_fetched_rows if is_first_run else (keep_rows + all_fetched_rows)

ws.clear()
ws.append_row(FINANCE_HEADERS)
batch_size = 2000
for i in range(0, len(final_rows), batch_size):
    batch = final_rows[i:i + batch_size]
    ws.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Записано строк {i+1}–{i+len(batch)}")
    time.sleep(1)

print(f"\n✅ Готово! Итого строк в 'finance': {len(final_rows)}")
