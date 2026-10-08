"""
Заказы по дням и артикулам из «Воронки продаж» (Analytics API) -> лист 'funnel_history'.

ЗАЧЕМ: Statistics API (/api/v1/supplier/orders) отдаёт не все заказы — на сверке за
04.10.2026 в нём 96 заказов / 143 320 ₽, а в кабинете и в воронке 115 / 176 496 ₽.
Воронка даёт ровно кабинетные цифры, поэтому ДРР от заказов считаем по ней.

Метод: POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products
Токен: категория «Аналитика». Лимит ~3 запроса/мин — между запросами пауза.
Один запрос = один день (по умолчанию окно 7 дней: сегодня и 6 предыдущих). Карточки
отсортированы по числу заказов, поэтому листаем страницы, только пока на странице ещё
есть карточки с заказами — обычно это 1 запрос на день.

Окно ПЕРЕЗАПИСЫВАЕТСЯ целиком при каждом запуске (воронка обновляется раз в час, данные
за последние дни дозревают), всё, что старше окна, не трогается. Если хоть один день окна
не удалось получить — лист НЕ трогаем (чтобы не заменить полные данные неполными).

Запуск:
  WB_TOKEN, GOOGLE_CREDENTIALS, SPREADSHEET_ID
  FUNNEL_DATE_FROM / FUNNEL_DATE_TO (YYYY-MM-DD) — для бэкафилла (например 30 дней назад)
"""
import os, sys, json, time, requests, gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': TOKEN, 'Content-Type': 'application/json'}
URL = 'https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products'
PAGE = 1000
PAUSE = 22            # сек между запросами (лимит ~3/мин)
WINDOW_DAYS = 7
MAX_DAYS = 120

now = datetime.now()
DATE_TO = os.environ.get('FUNNEL_DATE_TO', '').strip() or now.strftime('%Y-%m-%d')
DATE_FROM = os.environ.get('FUNNEL_DATE_FROM', '').strip() or (now - timedelta(days=WINDOW_DAYS - 1)).strftime('%Y-%m-%d')
d0, d1 = datetime.strptime(DATE_FROM, '%Y-%m-%d'), datetime.strptime(DATE_TO, '%Y-%m-%d')
days = [(d0 + timedelta(days=i)).strftime('%Y-%m-%d') for i in range((d1 - d0).days + 1)]
if not days or len(days) > MAX_DAYS:
    print(f"❌ Некорректный диапазон дат: {DATE_FROM} — {DATE_TO} ({len(days)} дней, максимум {MAX_DAYS})"); sys.exit(1)
print("fetch_funnel_history v1")
print(f"Период: {DATE_FROM} — {DATE_TO} ({len(days)} дн.)")


def post(body, label):
    """(ok, json). Повторы на 429/5xx; прочие ошибки печатаем целиком."""
    for attempt in range(5):
        try:
            r = requests.post(URL, headers=HEADERS, json=body, timeout=60)
        except Exception as e:
            print(f"  [{label}] исключение: {e}"); time.sleep(10); continue
        if r.status_code == 429:
            try: w = int(float(r.headers.get('X-Ratelimit-Retry', '') or 0))
            except Exception: w = 0
            w = max(w, 30 * (attempt + 1)); print(f"  [{label}] 429 — жду {w}с"); time.sleep(w); continue
        if r.status_code >= 500:
            w = 30 * (attempt + 1); print(f"  [{label}] {r.status_code} — повтор через {w}с"); time.sleep(w); continue
        if r.status_code == 200:
            try: return True, r.json()
            except Exception: return True, {}
        print(f"  [{label}] ОШИБКА {r.status_code}: {r.text[:500]}")
        return False, None
    return False, None


def fetch_day(day):
    """{nmId: (vendorCode, заказы, сумма)} только по карточкам с заказами; None при сбое."""
    cards, offset = {}, 0
    while True:
        ok, data = post({
            'selectedPeriod': {'start': day, 'end': day}, 'nmIds': [], 'brandNames': [], 'subjectIds': [],
            'tagIds': [], 'skipDeletedNm': False,
            'orderBy': {'field': 'orderCount', 'mode': 'desc'}, 'limit': PAGE, 'offset': offset}, day)
        if not ok:
            return None
        d = data.get('data', data) if isinstance(data, dict) else data
        prods = d.get('products', []) if isinstance(d, dict) else (d or [])
        last_n = 0
        for p in prods:
            pr = p.get('product', {}); sel = (p.get('statistic') or {}).get('selected', {})
            nm = pr.get('nmId'); n = int(sel.get('orderCount') or 0); s = float(sel.get('orderSum') or 0)
            last_n = n
            if nm is not None and n > 0:
                cards[nm] = (pr.get('vendorCode', ''), n, s)
        if len(prods) < PAGE or last_n == 0:
            return cards
        offset += PAGE
        time.sleep(PAUSE)


# ── 1. Забираем все дни окна ─────────────────────────────────────
fresh, failed = [], []
for i, day in enumerate(days):
    cards = fetch_day(day)
    if cards is None:
        failed.append(day); print(f"  ❌ {day}: не удалось получить"); continue
    n_total = sum(v[1] for v in cards.values()); s_total = sum(v[2] for v in cards.values())
    zero_sum = sum(1 for v in cards.values() if v[2] == 0)
    print(f"  {day}: карточек с заказами {len(cards)}, заказов {n_total}, сумма {s_total:,.0f} ₽"
          + (f"  ⚠️ у {zero_sum} карточек сумма = 0 при ненулевых заказах (известная особенность WB)" if zero_sum else ""))
    for nm, (art, n, s) in sorted(cards.items(), key=lambda kv: kv[1][0].lower()):
        fresh.append([day, art, nm, n, round(s, 2)])
    if i < len(days) - 1:
        time.sleep(PAUSE)

# ── ЗАЩИТА ОТ ПОТЕРИ ДАННЫХ ──────────────────────────────────────
if failed:
    print(f"\n⛔ Не получены дни: {', '.join(failed)}. Лист НЕ трогаем (иначе заменили бы полные данные неполными).")
    print("   Запусти скрипт ещё раз позже."); sys.exit(1)

# ── 2. Лист funnel_history: старое вне окна не трогаем, окно пишем заново ──
creds = Credentials.from_service_account_info(json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
sh = gspread.authorize(creds).open_by_key(os.environ['SPREADSHEET_ID'])
HEADER = ['Дата', 'Артикул поставщика', 'nmID', 'Заказы, шт', 'Сумма заказов, ₽']


def norm_day(v):
    v = str(v).strip()
    if len(v) >= 10 and v[4] == '-' and v[7] == '-': return v[:10]
    if len(v) >= 10 and v[2] == '.' and v[5] == '.': return f"{v[6:10]}-{v[3:5]}-{v[0:2]}"
    return v


try:
    ws = sh.worksheet('funnel_history')
    existing = ws.get_all_values()
    old_rows = existing[1:] if existing else []
except Exception:
    ws = sh.add_worksheet(title='funnel_history', rows=100, cols=len(HEADER))
    old_rows = []
    print("  Лист 'funnel_history' создан")

keep = [r for r in old_rows if r and not (DATE_FROM <= norm_day(r[0]) <= DATE_TO)]
print(f"\nСтрок вне окна (не трогаем): {len(keep)}, убрано за окно: {len(old_rows) - len(keep)}, свежих: {len(fresh)}")

final_rows = keep + fresh
ws.clear()
ws.append_row(HEADER)
for i in range(0, len(final_rows), 2000):
    ws.append_rows(final_rows[i:i + 2000], value_input_option='USER_ENTERED')
    time.sleep(1)
try:
    ws.format('A1:E1', {'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
                        'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18}})
    ws.freeze(rows=1)
except Exception:
    pass
print(f"✅ Готово! Итого строк в 'funnel_history': {len(final_rows)}")
