"""
Тянет детализированный отчёт о реализации (полная финансовая разбивка —
комиссии, логистика, хранение, штрафы, удержания и т.д.) и дописывает
в лист 'finance'. Аналог stocks_history/sales_history по духу: список
только РАСТЁТ, ничего не перезаписываем.

Используется:
  GET https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod
  Категория токена: Statistics (та же, что уже используется для orders/sales)

КЛЮЧЕВАЯ ИДЕЯ (в отличие от старой версии в другом репозитории): этот метод
устроен как курсор-пагинация по rrd_id (уникальный, монотонно растущий ID
строки отчёта) — НЕ "все записи, что изменились с даты", а "все записи ПОСЛЕ
этого rrd_id". Строки в этом отчёте никогда не меняются задним числом — это
чистый append-only лог. Поэтому: 1) не нужно ничего обновлять/перезаписывать,
только дописывать; 2) не нужно самому хранить состояние отдельно — просто
берём МАКСИМАЛЬНЫЙ rrd_id, что уже есть в листе 'finance', и продолжаем
пагинацию с него. Старая версия каждый день заново перечитывала ВЕСЬ период
с 1 мая и полностью перезаписывала лист — вот это и стало неподъёмным по
мере роста истории.

Запуск:
  export WB_TOKEN='...'              (токен с категорией Statistics)
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_finance_history.py

Для самого первого запуска (когда листа 'finance' ещё нет или он пуст) —
период отчёта начинается с FINANCE_DATE_FROM (по умолчанию 2026-05-01,
можно переопределить переменной окружения). На всех следующих запусках
эта дата уже не важна — пагинация идёт от последнего rrd_id в самом листе.
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
STATS_URL = 'https://statistics-api.wildberries.ru'

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])

# Точка отсчёта ТОЛЬКО для самого первого запуска (пустой лист) — дальше
# продолжаем от максимального rrd_id, уже сохранённого в листе.
FIRST_RUN_DATE_FROM = os.environ.get('FINANCE_DATE_FROM', '').strip() or '2026-05-01'
DATE_TO = (datetime.now() - timedelta(days=0)).strftime('%Y-%m-%d')

FINANCE_HEADERS = [
    "rrd_id", "Номер поставки", "Предмет", "Код номенклатуры", "Бренд", "Артикул поставщика",
    "Название", "Размер", "Баркод", "Тип документа", "Обоснование для оплаты",
    "Дата заказа покупателем", "Дата продажи", "Кол-во", "Цена розничная",
    "Вайлдберриз реализовал Товар (Пр)", "Согласованный продуктовый дисконт, %",
    "Промокод, %", "Итоговая согласованная скидка, %",
    "Цена розничная с учетом согласованной скидки",
    "Размер снижения кВВ из-за рейтинга, %", "Размер изменения кВВ из-за акции, %",
    "Платформенные скидки, %", "Размер кВВ, %", "Размер кВВ без НДС, % Базовый",
    "Итоговый кВВ без НДС, %",
    "Вознаграждение с продаж до вычета услуг поверенного, без НДС",
    "Возмещение за выдачу и возврат товаров на ПВЗ",
    "Компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов",
    "Размер компенсации платёжных услуг/Комиссии за интеграцию платёжных сервисов, %",
    "Тип платежа: компенсация платёжных услуг/Комиссия за интеграцию платёжных сервисов",
    "Вознаграждение Вайлдберриз (ВВ), без НДС", "НДС с Вознаграждения Вайлдберриз",
    "К перечислению Продавцу за реализованный Товар", "Количество доставок",
    "Количество возврата", "Услуги по доставке товара покупателю",
    "Дата начала действия фиксации", "Дата конца действия фиксации",
    "Признак услуги платной доставки", "Общая сумма штрафов",
    "Корректировка Вознаграждения Вайлдберриз (ВВ)",
    "Виды логистики, штрафов и корректировок ВВ",
    "Стикер МП", "Наименование банка-эквайера", "Номер офиса",
    "Наименование офиса доставки", "ИНН партнера", "Партнер", "Склад",
    "Страна", "Тип коробов", "Номер таможенной декларации",
    "Номер сборочного задания", "Код маркировки", "ШК", "Srid",
    "Возмещение издержек по перевозке/по складским операциям с товаром",
    "Организатор перевозки", "Хранение", "Удержания", "Операции на приемке",
    "Фиксированный коэффициент склада по поставке",
    "Признак продажи юридическому лицу", "Номер короба для обработки товара",
    "Скидка по программе софинансирования", "Скидка Wibes, %",
    "Компенсация скидки по программе лояльности",
    "Стоимость участия в программе лояльности",
    "Сумма баллов, удержанных по программе лояльности", "Id корзины заказа",
    "Разовое изменение срока перечисления денежных средств",
    "Id собственной акции продавца с дополнительной скидкой",
    "Размер дополнительной скидки по собственной акции продавца, %",
    "Способы продажи и тип товара",
    "Уникальный идентификатор скидки лояльности от продавца",
    "Размер скидки лояльности от продавца,%", "Id промокода",
    "Скидка за промокод, %", "Id подменного артикула",
    "Скидка по подменному артикулу, %", "Оптовая скидка для бизнеса, %",
]


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


def row_from_item(item):
    return [
        item.get("rrd_id", ""),
        item.get("gi_id", ""),
        item.get("subject_name", ""),
        item.get("nm_id", ""),
        item.get("brand_name", ""),
        item.get("sa_name", ""),
        item.get("ts_name", ""),
        item.get("size", ""),
        item.get("barcode", ""),
        item.get("doc_type_name", ""),
        item.get("supplier_oper_name", ""),
        item.get("order_dt", "")[:10] if item.get("order_dt") else "",
        item.get("sale_dt", "")[:10] if item.get("sale_dt") else "",
        item.get("quantity", 0),
        item.get("retail_price", 0),
        item.get("retail_amount", 0),
        item.get("sale_percent", 0),
        item.get("promo_code_discount", 0),
        item.get("total_discount_percent", 0),
        item.get("retail_price_withdisc_rub", 0),
        item.get("for_pay_initial", 0),
        item.get("for_pay_wb_offset", 0),
        item.get("platform_user_discount", 0),
        item.get("commission_percent", 0),
        item.get("commission_percent_base", 0),
        item.get("for_pay_withdisc", 0),
        item.get("ppvz_sales_commission", 0),
        item.get("ppvz_for_pay_nds", 0),
        item.get("acquiring_bank_commission", 0),
        item.get("acquiring_bank_commission_percent", 0),
        item.get("acquiring_bank_commission_type", ""),
        item.get("ppvz_vw", 0),
        item.get("ppvz_vw_nds", 0),
        item.get("ppvz_for_pay", 0),
        item.get("delivery_amount", 0),
        item.get("return_amount", 0),
        item.get("delivery_rub", 0),
        item.get("fix_tariff_date_from", ""),
        item.get("fix_tariff_date_to", ""),
        item.get("is_kgvp_v2", ""),
        item.get("penalty", 0),
        item.get("additional_payment", 0),
        item.get("rebill_logistic_cost_type", ""),
        item.get("sticker_id", ""),
        item.get("acquiring_bank", ""),
        item.get("office_id", ""),
        item.get("office_name", ""),
        item.get("supplier_inn", ""),
        item.get("partner_name", ""),
        item.get("site_country", ""),
        item.get("country_name", ""),
        item.get("box_type_name", ""),
        item.get("declaration_number", ""),
        item.get("assembly_task_id", ""),
        item.get("marking_code", ""),
        item.get("shk_id", ""),
        item.get("srid", ""),
        item.get("rebill_logistic_cost", 0),
        item.get("kiz", ""),
        item.get("storage_fee", 0),
        item.get("deduction", 0),
        item.get("acceptance", 0),
        item.get("supplier_promo", 0),
        item.get("is_legal_entity", ""),
        item.get("trbx_id", ""),
        item.get("cofinance_price", 0),
        item.get("wibes_discount", 0),
        item.get("loyalty_discount_compensation", 0),
        item.get("loyalty_price", 0),
        item.get("loyalty_bonus_payment", 0),
        item.get("basket_id", ""),
        item.get("one_time_change_of_transfer_deadline", ""),
        item.get("promo_id", ""),
        item.get("promo_discount_percent", 0),
        item.get("sales_method", ""),
        item.get("unique_loyalty_discount_id", ""),
        item.get("loyalty_discount_percent", 0),
        item.get("promo_code_id", ""),
        item.get("promo_code_discount_percent", 0),
        item.get("substitute_article_id", ""),
        item.get("substitute_article_discount_percent", 0),
        item.get("wholesale_discount", 0),
    ]


# ── 1. Лист 'finance' + определяем, откуда продолжать ─────────────
print("\n→ Шаг 1: Проверяем лист 'finance'...")

try:
    ws = sh.worksheet('finance')
except Exception:
    ws = None

if ws is None or ws.acell('A1').value is None:
    if ws is None:
        ws = sh.add_worksheet(title='finance', rows=200000, cols=len(FINANCE_HEADERS))
    ws.append_row(FINANCE_HEADERS)
    max_rrd_id = 0
    date_from = FIRST_RUN_DATE_FROM
    print(f"  Лист новый/пустой — первый запуск, период с {date_from}")
else:
    # Берём только колонку A (rrd_id) — не тащим весь лист целиком
    col_a = ws.col_values(1)[1:]  # без заголовка
    ids = [int(v) for v in col_a if str(v).strip().isdigit()]
    max_rrd_id = max(ids) if ids else 0
    date_from = FIRST_RUN_DATE_FROM  # WB всё равно требует dateFrom/dateTo, но rrdid решает, что реально новое
    print(f"  Уже есть {len(ids)} строк, последний rrd_id: {max_rrd_id} — продолжаем с него")

# ── 2. Пагинация по rrd_id ─────────────────────────────────────────
print(f"\n→ Шаг 2: Забираем новые строки (dateFrom={date_from}, dateTo={DATE_TO})...")

new_rows = []
rrdid = max_rrd_id
seen_ids = set()

while True:
    data = wb_get(f'{STATS_URL}/api/v5/supplier/reportDetailByPeriod', params={
        'dateFrom': date_from,
        'dateTo': DATE_TO,
        'limit': 100000,
        'rrdid': rrdid,
    })
    if data is None:
        print("❌ Нет ответа от API — прерываем на том, что уже собрали")
        break
    if not data:
        print("  Новых строк больше нет")
        break

    new_in_batch = 0
    batch_max_rrd = rrdid
    for item in data:
        rid = item.get('rrd_id')
        if rid is None or rid <= max_rrd_id or rid in seen_ids:
            continue
        seen_ids.add(rid)
        new_rows.append(row_from_item(item))
        new_in_batch += 1
        if rid > batch_max_rrd:
            batch_max_rrd = rid

    print(f"  Получено {len(data)} строк, из них новых: {new_in_batch} (всего новых: {len(new_rows)})")

    if new_in_batch == 0 or len(data) < 100000:
        break
    rrdid = batch_max_rrd
    time.sleep(2)

print(f"\nИтого новых строк: {len(new_rows)}")

# ── 3. Дописываем (только append, никогда не переписываем старое) ──
if not new_rows:
    print("Нечего дописывать — всё уже актуально")
    exit(0)

print("\n→ Шаг 3: Дописываем в Google Sheets...")
batch_size = 2000
for i in range(0, len(new_rows), batch_size):
    batch = new_rows[i:i + batch_size]
    ws.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Записано строк {i+1}–{i+len(batch)}")
    time.sleep(1)

print(f"\n✅ Готово! Дописано {len(new_rows)} новых строк в 'finance'")
