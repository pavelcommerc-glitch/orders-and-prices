"""
fetch_catalog.py — выгружает справочник товаров одним листом «каталог»:
  Артикул поставщика | Артикул WB (nmID) | Название | Описание | Категория | Бренд
  | Рейтинг | Кол-во отзывов
  | Длина, см | Ширина, см | Высота, см | Вес с упаковкой, кг | Объём по габаритам, л
  | Объём WB, л | ID предмета | Последний баркод

Новые колонки (I:P) нужны юнитке: габариты и вес — из карточки (content-api, «как заявил продавец»);
«Объём WB, л» — объём по ЗАМЕРАМ WB из отчёта об остатках (seller-analytics-api /api/v1/warehouse_remains):
по нему WB считает логистику, и у части товаров он заметно отличается от объёма по габаритам продавца.
Если отчёт не получен (нет прав токена/лимит), колонка остаётся пустой, юнитка берёт объём по габаритам.
Лист ОБНОВЛЯЕТСЯ НА МЕСТЕ (clear + запись), а не удаляется — иначе формулы других листов, ссылающиеся на «каталог», ломаются (#REF!).

Название/описание/категория/бренд — из content-api (тот же WB_TOKEN, что и в
fetch_advert.py / fetch_storage.py).

Рейтинг и отзывы — из ПУБЛИЧНОГО API карточки WB (card.wb.ru), токен не нужен,
это тот же запрос, что делает сам сайт wildberries.ru при отображении карточки
товара. ⚠ Этот блок НЕ проверен на живых данных (нет доступа к доменам WB из
песочницы, где писался скрипт) — сам домен/формат ответа WB может отличаться
от того, что тут заложено. Если после первого запуска рейтинг/отзывы придут
пустыми — пришли мне вывод консоли (в частности пример ответа API), поправлю
под реальный формат за одну итерацию.

Запуск:
  export WB_TOKEN='...'
  export GOOGLE_CREDENTIALS='...'
  export SPREADSHEET_ID='...'
  pip install gspread google-auth requests
  python fetch_catalog.py
"""

import os
import json
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
CONTENT_URL = 'https://content-api.wildberries.ru'
CARD_URL = 'https://card.wb.ru/cards/v2/detail'  # публичный, без токена
ANALYTICS_URL = 'https://seller-analytics-api.wildberries.ru'

SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.environ['GOOGLE_CREDENTIALS'])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(os.environ['SPREADSHEET_ID'])


def wb_post(url, data, retries=5):
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, json=data, timeout=60)
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  429 — ждём {wait}с...")
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


print("Получаю карточки товаров через content-api...")
cards_rows = []
nm_ids = []
extra_map = {}   # nmID (str) -> [длина, ширина, высота, вес, объём по габаритам, ID предмета, последний баркод]
cursor = {}
page = 0

while True:
    payload = {
        "settings": {
            "cursor": {"limit": 100, **cursor},
            "filter": {"withPhoto": -1}
        }
    }
    result = wb_post(f'{CONTENT_URL}/content/v2/get/cards/list', payload)
    if not result:
        break
    cards = result.get('cards', [])
    if not cards:
        break
    for card in cards:
        nm_id = card.get('nmID', '')
        vendor_code = card.get('vendorCode', '')
        title = card.get('title', '')
        description = card.get('description', '')
        subject_name = card.get('subjectName', '')
        brand = card.get('brand', '')
        cards_rows.append([vendor_code, nm_id, title, description, subject_name, brand])
        dims = card.get('dimensions') or {}
        ln, wd, ht, wt = dims.get('length'), dims.get('width'), dims.get('height'), dims.get('weightBrutto')
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        vol_dims = round(ln * wd * ht / 1000, 3) if all(num(x) for x in (ln, wd, ht)) else ''
        barcode = ''
        for sz in (card.get('sizes') or []):
            for sku in (sz.get('skus') or []):
                barcode = sku            # последний баркод
        extra_map[str(nm_id)] = [ln if num(ln) else '', wd if num(wd) else '', ht if num(ht) else '', wt if num(wt) else '', vol_dims, card.get('subjectID', ''), barcode]
        if nm_id:
            nm_ids.append(str(nm_id))
    page += 1
    print(f"  Страница {page}: {len(cards)} карточек, всего: {len(cards_rows)}")
    if len(cards) < 100:
        break
    cur = result.get('cursor', {})
    if not cur.get('updatedAt') or not cur.get('nmID'):
        break
    cursor = {'updatedAt': cur['updatedAt'], 'nmID': cur['nmID']}
    time.sleep(0.5)

print(f"\nВсего товаров: {len(cards_rows)}")

if not cards_rows:
    print("❌ Ничего не получено — проверь права токена (WB_TOKEN) на content-api")
    exit(1)

# ── Рейтинг и отзывы — публичный API карточки, батчами по 100 nmID ──────────
print("\nПолучаю рейтинг и кол-во отзывов (card.wb.ru)...")
rating_map = {}   # nmID (str) -> (rating, feedbacks)
batch_size_cards = 100
for i in range(0, len(nm_ids), batch_size_cards):
    batch = nm_ids[i:i + batch_size_cards]
    try:
        r = requests.get(CARD_URL, params={
            'appType': 1, 'curr': 'rub', 'dest': -1257786, 'spp': 0,
            'nm': ';'.join(batch),
        }, timeout=30)
        if r.status_code == 200:
            data = r.json()
            products = data.get('data', {}).get('products', [])
            for p in products:
                pid = str(p.get('id', ''))
                rating = p.get('reviewRating', p.get('rating'))
                feedbacks = p.get('feedbacks')
                if pid:
                    rating_map[pid] = (rating, feedbacks)
        else:
            print(f"  Батч {i//batch_size_cards+1}: код {r.status_code} — пропускаю")
    except Exception as e:
        print(f"  Батч {i//batch_size_cards+1}: ошибка {e} — пропускаю")
    time.sleep(0.3)

print(f"  Получено рейтингов: {len(rating_map)} из {len(nm_ids)} товаров")
if not rating_map:
    print("  ⚠ Рейтинги не получены вообще — вероятно, изменился домен/формат "
          "публичного API карточки. Колонки будут 'н/д', остальное не пострадает.")

def fetch_wb_volumes():
    """Объём по замерам WB (л) по nmID. Асинхронный отчёт: создать → ждать → скачать. Лимиты WB малы (≈1 запрос в минуту).
    Любая ошибка → пустой словарь (юнитка возьмёт объём по габаритам) + диагностика в консоль."""
    params = {'locale': 'ru', 'groupByNm': 'true', 'groupBySa': 'true', 'groupByBrand': 'false',
              'groupBySubject': 'false', 'groupByBarcode': 'false', 'groupBySize': 'false'}
    def get(url, **kw):
        for attempt in range(6):
            try:
                r = requests.get(url, headers=HEADERS, timeout=60, **kw)
            except Exception as e:
                print(f"  Исключение: {e}"); time.sleep(10); continue
            if r.status_code == 429:
                wait = 65 * (attempt + 1); print(f"  429 — ждём {wait}с..."); time.sleep(wait); continue
            return r
        return None
    print("\nПолучаю объём по замерам WB (warehouse_remains)...")
    r = get(f'{ANALYTICS_URL}/api/v1/warehouse_remains', params=params)
    if r is None or r.status_code != 200:
        print(f"  ⚠ Не удалось создать отчёт: {None if r is None else r.status_code} {'' if r is None else r.text[:300]}")
        return {}
    task_id = (r.json().get('data') or {}).get('taskId')
    if not task_id:
        print(f"  ⚠ В ответе нет taskId: {r.text[:300]}"); return {}
    for _ in range(60):                       # до ~6 минут
        time.sleep(6)
        rs = get(f'{ANALYTICS_URL}/api/v1/warehouse_remains/tasks/{task_id}/status')
        if rs is None or rs.status_code != 200:
            print(f"  ⚠ статус отчёта: {None if rs is None else rs.status_code}"); continue
        status = (rs.json().get('data') or {}).get('status')
        if status == 'done':
            break
        if status in ('purged', 'canceled'):
            print(f"  ⚠ отчёт {status}"); return {}
    else:
        print("  ⚠ отчёт не готов за отведённое время"); return {}
    time.sleep(61)                            # лимит на скачивание ≈ 1 запрос в минуту
    rd = get(f'{ANALYTICS_URL}/api/v1/warehouse_remains/tasks/{task_id}/download')
    if rd is None or rd.status_code != 200:
        print(f"  ⚠ скачивание отчёта: {None if rd is None else rd.status_code} {'' if rd is None else rd.text[:300]}"); return {}
    out = {}
    data = rd.json()
    for it in (data if isinstance(data, list) else []):
        v = it.get('volume')
        if it.get('nmId') and isinstance(v, (int, float)) and v > 0:
            out[str(it['nmId'])] = float(v)
    print(f"  Объём по замерам WB: {len(out)} товаров (пример записи: {str(data[0])[:200] if data else 'пусто'})")
    return out

wb_vol = fetch_wb_volumes()

for row in cards_rows:
    nm_id = str(row[1]) if row[1] else ''
    rating, feedbacks = rating_map.get(nm_id, (None, None))
    row.append(rating if rating is not None else 'н/д')
    row.append(feedbacks if feedbacks is not None else 'н/д')
    ln, wd, ht, wt, vol_dims, subj_id, barcode = extra_map.get(nm_id, ['', '', '', '', '', '', ''])
    row.extend([ln, wd, ht, wt, vol_dims, wb_vol.get(nm_id, ''), subj_id, barcode])

# диагностика: насколько замер WB расходится с габаритами продавца
both = [(r[12], r[13]) for r in cards_rows if isinstance(r[12], (int, float)) and isinstance(r[13], (int, float)) and r[12] > 0]
if both:
    far = sum(1 for d, w in both if abs(w / d - 1) > 0.15)
    print(f"  Замер WB vs габариты продавца: у {far} из {len(both)} товаров расхождение больше 15% (логистика считается по замеру WB)")
nodim = sum(1 for r in cards_rows if r[12] == '' and r[13] == '')
print(f"  Без объёма вообще (нет габаритов и замера): {nodim}")

print("\nЗаписываю в Google Sheets...")
SHEET_NAME = 'каталог'
NCOLS = 16
try:
    ws = sh.worksheet(SHEET_NAME)
    ws.clear()                                    # НЕ удаляем лист: ссылки на него в формулах остались бы #REF!
    ws.resize(rows=len(cards_rows) + 5, cols=NCOLS)
except gspread.exceptions.WorksheetNotFound:
    ws = sh.add_worksheet(title=SHEET_NAME, rows=len(cards_rows) + 5, cols=NCOLS)

headers = ['Артикул поставщика', 'Артикул WB (nmID)', 'Название', 'Описание',
           'Категория', 'Бренд', 'Рейтинг', 'Кол-во отзывов',
           'Длина, см', 'Ширина, см', 'Высота, см', 'Вес с упаковкой, кг', 'Объём по габаритам, л',
           'Объём WB, л', 'ID предмета', 'Последний баркод']
rows = [headers] + cards_rows

batch_size = 500
for i in range(0, len(rows), batch_size):
    batch = rows[i:i + batch_size]
    start_row = i + 1
    end_row = start_row + len(batch) - 1
    ws.update(batch, f'A{start_row}:P{end_row}', value_input_option='RAW')   # RAW: баркоды и артикулы не превращаются в числа/даты
    print(f"  Записано строк {start_row}-{end_row}")

ws.format('A1:P1', {
    'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
    'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
})
ws.freeze(rows=1, cols=1)

print(f"\n✅ Готово. Лист '{SHEET_NAME}': {len(cards_rows)} товаров")
