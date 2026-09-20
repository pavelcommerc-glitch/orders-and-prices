"""
Тянет детализированный отчёт о реализации (полная финансовая разбивка —
комиссии, логистика, хранение, штрафы, удержания и т.д.) в лист 'finance'.

Используется:
  GET https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod
  Категория токена: Statistics (та же, что уже используется для orders/sales)

ВАЖНО (обновлено): отчёт о реализации у WB НЕ финальный сразу после публикации —
WB ещё 1-2 недели дозаполняет и правит его задним числом (штрафы, возвраты
и т.д. могут появиться/измениться уже ПОСЛЕ того, как мы его забрали).
Поэтому чистый append-only (только дописывать новые rrd_id) со временем
расходится с кабинетом. Логика теперь такая:

  - Всё, что СТАРШЕ 14 дней — считаем окончательным, не трогаем (как раньше).
  - Последние 14 дней — при КАЖДОМ запуске выбрасываем то, что уже было
    записано за этот период, и выкачиваем этот кусок ЗАНОВО целиком.
    Не самый экономный вариант по объёму запроса, зато данные в этом
    "свежем" окне не расходятся с тем, что реально показывает WB.

Лимит на этот метод у WB — примерно 1 запрос в минуту, общий на все методы
статистики (orders/sales/этот) для одного токена аккаунта — не гоняй этот
скрипт впритык по времени к fetch_sales_history.py на одном токене.

Запуск:
  export WB_TOKEN='...'              (токен с категорией Statistics)
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_finance_history.py

Для самого первого запуска (когда листа 'finance' ещё нет или он пуст) —
период отчёта начинается с FINANCE_DATE_FROM (по умолчанию 2026-05-01,
можно переопределить переменной окружения). На всех следующих запусках
эта дата уже не важна — работает только 14-дневное окно перезаписи.
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


# ── 1. Лист 'finance' + разделяем на "старое" (не трогаем) и "окно
#       последних 14 дней" (полностью перезабираем заново) ──────────
print("\n→ Шаг 1: Проверяем лист 'finance'...")

REWRITE_WINDOW_DAYS = 14
rewrite_cutoff = (datetime.now() - timedelta(days=REWRITE_WINDOW_DAYS)).strftime('%Y-%m-%d')
print(f"  Окно перезаписи: последние {REWRITE_WINDOW_DAYS} дней (с {rewrite_cutoff}) — "
      f"WB дозаполняет/правит отчёт о реализации ещё 1-2 недели после публикации, "
      f"поэтому эти строки каждый раз выкачиваем заново, а не просто дописываем.")

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
    print(f"  Лист новый/пустой — первый запуск, период с {date_from} (весь сразу, окна перезаписи ещё нет)")
else:
    all_values = ws.get_all_values()
    existing_headers = all_values[0]
    data_rows = all_values[1:]
    # индекс колонки "Дата продажи" — по названию заголовка, не по позиции,
    # на случай если порядок колонок когда-то изменится
    sale_date_idx = existing_headers.index('Дата продажи') if 'Дата продажи' in existing_headers else 12

    keep_rows = [r for r in data_rows if len(r) > sale_date_idx and r[sale_date_idx] < rewrite_cutoff]
    dropped = len(data_rows) - len(keep_rows)
    print(f"  Было строк: {len(data_rows)}. Оставляем (дата продажи < {rewrite_cutoff}): {len(keep_rows)}. "
          f"Убираем на переперезабор (дата продажи >= {rewrite_cutoff}): {dropped}")
    date_from = rewrite_cutoff  # забираем окно заново с нуля, rrd_id тут не помогает — нужен полный охват дат

print(f"\n→ Шаг 2: Забираем строки за окно (dateFrom={date_from}, dateTo={DATE_TO})...")

all_fetched_rows = []
rrdid = 0
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
        print("  Строк больше нет")
        break

    new_in_batch = 0
    batch_max_rrd = rrdid
    for item in data:
        rid = item.get('rrd_id')
        if rid is None or rid in seen_ids:
            continue
        seen_ids.add(rid)

        # ДИАГНОСТИКА (один раз): печатаем сырой JSON первой строки с
        # обоснованием "Продажа" — чтобы найти реальное имя поля для
        # эквайринга, раз "acquiring_bank_commission" почему-то всегда 0.
        if item.get('supplier_oper_name') == 'Продажа' and not globals().get('_diag_printed'):
            print("\n  🔎 ДИАГНОСТИКА — сырой JSON первой строки 'Продажа' (пришли мне этот вывод):")
            print("  " + json.dumps(item, ensure_ascii=False, indent=2)[:3000])
            print()
            globals()['_diag_printed'] = True

        all_fetched_rows.append(row_from_item(item))
        new_in_batch += 1
        if rid > batch_max_rrd:
            batch_max_rrd = rid

    print(f"  Получено {len(data)} строк, из них новых в этой пачке: {new_in_batch} (всего собрано: {len(all_fetched_rows)})")

    if new_in_batch == 0 or len(data) < 100000:
        break
    if batch_max_rrd <= rrdid:
        print("  ⚠️ Курсор не сдвинулся — останавливаемся, чтобы не зациклиться")
        break
    rrdid = batch_max_rrd
    time.sleep(2)

print(f"\nИтого свежих строк за окно: {len(all_fetched_rows)}")

# ── 3. Полная перезапись: старое (что оставили) + свежее окно ──────
print("\n→ Шаг 3: Перезаписываем лист (старое без изменений + свежее окно)...")

if is_first_run:
    final_rows = all_fetched_rows
else:
    final_rows = keep_rows + all_fetched_rows

ws.clear()
ws.append_row(FINANCE_HEADERS)
batch_size = 2000
for i in range(0, len(final_rows), batch_size):
    batch = final_rows[i:i + batch_size]
    ws.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Записано строк {i+1}–{i+len(batch)}")
    time.sleep(1)

print(f"\n✅ Готово! Итого строк в 'finance': {len(final_rows)}")
