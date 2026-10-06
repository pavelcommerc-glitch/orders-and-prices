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

ДИАПАЗОН ДАТ: метод отдаёт статистику по дням сразу за период до 31 дня в
ОДНОМ запросе (не нужно дёргать API по одному дню). По умолчанию скрипт
берёт ВЧЕРА + СЕГОДНЯ (для ежедневного крона) — не только "сегодня", потому
что крон идёт рано утром (03:30), и "сегодня" на тот момент — это первые
пару часов суток, почти весь расход ещё не нагорел. Раньше это приводило
к тому, что вчерашний день навсегда оставался с заниженным расходом (строка
с датой уже записана — скрипт её больше не трогал). Теперь при каждом
запуске окно [вчера, сегодня] ПЕРЕЗАПИСЫВАЕТСЯ целиком свежими данными —
не просто дописывается. Для разового бэкафилла задай ADS_DATE_FROM/ADS_DATE_TO:

  export ADS_DATE_FROM='2026-08-11'
  export ADS_DATE_TO='2026-08-24'
  python fetch_ads_history.py

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
from datetime import datetime, timedelta

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
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
DATE_FROM = os.environ.get('ADS_DATE_FROM', '').strip() or YESTERDAY
DATE_TO = os.environ.get('ADS_DATE_TO', '').strip() or TODAY
print("fetch_ads_history v3 (пустой ответ/null = не сбой)")
print(f"Период снятия рекламной статистики: {DATE_FROM} — {DATE_TO}")

# WB fullstats: максимум 31 день за один запрос — проверим и подскажем, если превысили
_days_span = (datetime.strptime(DATE_TO, '%Y-%m-%d') - datetime.strptime(DATE_FROM, '%Y-%m-%d')).days + 1
if _days_span > 31:
    print(f"❌ Период {_days_span} дней — больше лимита WB (31 день за раз). "
          f"Разбей на несколько запусков с разными ADS_DATE_FROM/ADS_DATE_TO.")
    exit(1)


def wb_get_ex(url, params=None, retries=5):
    """Возвращает (ok, data). ok=False — РЕАЛЬНЫЙ сбой (429 до исчерпания
    попыток, код не 200, исключение). ok=True — WB ответил 200; data может
    быть списком/словарём, а также пустым: WB для "нет статистики" отдаёт
    и [] и null — оба приводим к [] (это не ошибка)."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  ⏳ 429 — жду {wait}с (попытка {attempt+1}/{retries})...")
                time.sleep(wait)
                continue
            if r.status_code == 200:
                data = r.json()
                return True, (data if data is not None else [])
            print(f"  Ошибка {r.status_code}: {r.text[:300]}")
            return False, None
        except Exception as e:
            print(f"  Исключение: {e}")
            time.sleep(10)
    return False, None


def wb_get(url, params=None, retries=5):
    ok, data = wb_get_ex(url, params, retries)
    return data if ok else None


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

# (дата, nm_id) -> агрегированные показатели за эту дату
agg = {}
any_batch_failed = False

batches = [all_advert_ids[i:i + BATCH_SIZE] for i in range(0, len(all_advert_ids), BATCH_SIZE)]
for bi, batch in enumerate(batches):
    ids_param = ','.join(str(x) for x in batch)
    print(f"  Пачка {bi+1}/{len(batches)}: {len(batch)} кампаний")
    ok, resp = wb_get_ex(f'{ADVERT_URL}/adv/v3/fullstats', params={
        'ids': ids_param,
        'beginDate': DATE_FROM,
        'endDate': DATE_TO,
    })
    if ok:
        # ok=True и пустой resp — это НЕ сбой: по этим кампаниям за окно
        # просто нет статистики (пауза/завершены).
        if not resp:
            print(f"    (пачка {bi+1}: статистики за окно нет — это нормально для неактивных кампаний)")
        for campaign in resp:
            for day in campaign.get('days', []):
                # day['date'] приходит как "2026-08-24T00:00:00Z" — берём только дату
                day_date = str(day.get('date', ''))[:10]
                if not day_date:
                    continue
                for app in day.get('apps', []):
                    for nm in app.get('nms', []):
                        nm_id = str(nm.get('nmId', ''))
                        if not nm_id:
                            continue
                        key = (day_date, nm_id)
                        if key not in agg:
                            agg[key] = {'views': 0, 'clicks': 0, 'orders': 0, 'sum': 0.0, 'sum_price': 0.0}
                        agg[key]['views'] += nm.get('views', 0) or 0
                        agg[key]['clicks'] += nm.get('clicks', 0) or 0
                        agg[key]['orders'] += nm.get('orders', 0) or 0
                        agg[key]['sum'] += nm.get('sum', 0) or 0
                        agg[key]['sum_price'] += nm.get('sum_price', 0) or 0
    else:
        print(f"    ⚠️ Пачка {bi+1}: РЕАЛЬНЫЙ сбой запроса (причина — в строке выше)")
        any_batch_failed = True

    if bi < len(batches) - 1:
        print(f"    ждём {SLEEP_BETWEEN_BATCHES}с (лимит API)...")
        time.sleep(SLEEP_BETWEEN_BATCHES)

dates_covered = sorted(set(k[0] for k in agg.keys()))
print(f"  Дат с данными: {len(dates_covered)} ({dates_covered[:3]}{'...' if len(dates_covered) > 3 else ''})")
print(f"  Всего пар (дата, артикул): {len(agg)}")

# ── 3. Формируем строки ───────────────────────────────────────────
print("\n→ Шаг 3: Формируем строки...")

rows = []
unmatched = set()
for (day_date, nm_id), m in agg.items():
    article, name = nm_to_article.get(nm_id, ('', ''))
    if not article:
        unmatched.add(nm_id)
    ctr = round(m['clicks'] / m['views'] * 100, 2) if m['views'] else 0
    cpc = round(m['sum'] / m['clicks'], 2) if m['clicks'] else 0
    rows.append([
        day_date, article, nm_id, name,
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

# ── ЗАЩИТА ОТ ПОТЕРИ ДАННЫХ: если хоть одна пачка кампаний не ответила —
# не трогаем лист. Иначе мы бы стёрли уже записанные (полные) строки за
# это окно и заменили их заведомо неполными (та пачка, что упала, туда
# просто не попадёт). ────────────────────────────────────────────────
if any_batch_failed:
    print("\n⛔ Хотя бы одна пачка кампаний не ответила — данные за окно неполные.")
    print("   Лист НЕ трогаем, чтобы не заменить уже записанные полные строки")
    print("   неполными. Запусти скрипт ещё раз позже.")
    exit(1)

is_new_sheet = False
try:
    ws = sh.worksheet('ads_history')
    existing = ws.get_all_values()
    if not existing:
        ws.append_row(HEADERS_ROW)
        existing_rows = []
        is_new_sheet = True
    else:
        existing_rows = existing[1:]
except Exception:
    ws = sh.add_worksheet(title='ads_history', rows=200000, cols=len(HEADERS_ROW))
    ws.append_row(HEADERS_ROW)
    existing_rows = []
    is_new_sheet = True
    print("  Лист 'ads_history' создан")

# ВАЖНО (исправлено): раньше строки с уже записанной датой просто
# пропускались — значит если в первый раз за сегодня (рано утром) данные
# были неполными, они такими и оставались НАВСЕГДА. Теперь вместо
# "пропустить, если дата уже есть" — "выбросить старые строки за даты
# из окна [DATE_FROM, DATE_TO] и записать их заново, целиком свежими".
# Всё, что ВНЕ этого окна (более старые даты), не трогаем — как в finance.
keep_rows = [r for r in existing_rows if len(r) > 0 and not (DATE_FROM <= r[0] <= DATE_TO)]
dropped = len(existing_rows) - len(keep_rows)
if dropped:
    print(f"  Убрано старых строк за окно {DATE_FROM}–{DATE_TO} (будут перезаписаны свежими): {dropped}")

final_rows = keep_rows + rows
print(f"  Строк вне окна (не трогаем): {len(keep_rows)}, свежих за окно: {len(rows)}, итого: {len(final_rows)}")

ws.clear()
ws.append_row(HEADERS_ROW)
batch_size = 2000
for i in range(0, len(final_rows), batch_size):
    batch = final_rows[i:i + batch_size]
    ws.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Записано строк {i+1}–{i+len(batch)}")
    time.sleep(1)

ws.format('A1:K1', {
    'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
    'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
})
ws.freeze(rows=1, cols=2)

print(f"\n✅ Готово!")
print(f"   Период (перезаписан): {DATE_FROM} — {DATE_TO}")
print(f"   Итого строк в 'ads_history': {len(final_rows)}")
