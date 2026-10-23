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

НОВОЕ (v2): кроме заказов забираем ВЕСЬ ТРАФИК воронки: переходы в карточку, добавления в корзину, выкупы, отмены.
  • лист 'funnel_history' — как раньше (только карточки с заказами: Дата | Артикул | nmID | Заказы | Сумма) — по нему считается ДРР и РНП;
  • лист 'funnel_traffic' — новый: Дата | Артикул | nmID | Переходы в карточку | Корзина | Заказы | Выкупы | Сумма выкупов | Отмены
    по всем карточкам, у которых за день была хоть какая-то активность; хранится TRAFFIC_KEEP_DAYS дней (старые строки сверху удаляются).
  Конверсии (переход→корзина, корзина→заказ, переход→заказ) считаются в таблице формулами из этих счётчиков.
  Запись листов: повтор при лимите Google (429) и перезапись только хвоста (окна), а не всего листа.

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
from gspread.utils import rowcol_to_a1

TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': TOKEN, 'Content-Type': 'application/json'}
URL = 'https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products'
PAGE = 1000
PAUSE = 22            # сек между запросами (лимит ~3/мин)
WINDOW_DAYS = 7
MAX_DAYS = 120
TRAFFIC_KEEP_DAYS = 60   # сколько дней хранить лист funnel_traffic

now = datetime.now()
DATE_TO = os.environ.get('FUNNEL_DATE_TO', '').strip() or now.strftime('%Y-%m-%d')
DATE_FROM = os.environ.get('FUNNEL_DATE_FROM', '').strip() or (now - timedelta(days=WINDOW_DAYS - 1)).strftime('%Y-%m-%d')
d0, d1 = datetime.strptime(DATE_FROM, '%Y-%m-%d'), datetime.strptime(DATE_TO, '%Y-%m-%d')
days = [(d0 + timedelta(days=i)).strftime('%Y-%m-%d') for i in range((d1 - d0).days + 1)]
if not days or len(days) > MAX_DAYS:
    print(f"❌ Некорректный диапазон дат: {DATE_FROM} — {DATE_TO} ({len(days)} дней, максимум {MAX_DAYS})"); sys.exit(1)
print("fetch_funnel_history v2 (заказы + трафик воронки)")
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


FIELDS = {   # метрика -> возможные имена поля в ответе WB (берём первое найденное)
    'opens':  ('openCount', 'openCardCount', 'openCard'),
    'carts':  ('cartCount', 'addToCartCount', 'addToCart'),
    'orders': ('orderCount',),
    'osum':   ('orderSum',),
    'buyouts': ('buyoutCount', 'buyoutsCount'),
    'bsum':   ('buyoutSum', 'buyoutsSum'),
    'cancels': ('cancelCount', 'cancelsCount'),
}
_seen_keys = set()


def pick(sel, names):
    for n in names:
        v = sel.get(n)
        if isinstance(v, (int, float)) and not isinstance(v, bool): return v
    return 0


def fetch_day(day):
    """{nmId: {art, opens, carts, orders, osum, buyouts, bsum, cancels}} по карточкам с ЛЮБОЙ активностью; None при сбое.
    Листаем ВСЕ страницы (карточки без заказов, но с трафиком, лежат вперемешку): каталог ~1–2 тыс. карточек = 1–2 запроса в день."""
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
        for p in prods:
            pr = p.get('product', {}); sel = (p.get('statistic') or {}).get('selected', {})
            if not _seen_keys and sel:
                _seen_keys.update(sel.keys()); print(f"  Поля воронки в ответе WB: {sorted(sel.keys())}")
            nm = pr.get('nmId')
            if nm is None: continue
            m = {k: pick(sel, names) for k, names in FIELDS.items()}
            if any(m[k] > 0 for k in ('opens', 'carts', 'orders', 'buyouts', 'cancels')):
                m['art'] = pr.get('vendorCode', ''); m['osum'] = float(m['osum']); m['bsum'] = float(m['bsum']); cards[nm] = m
        if len(prods) < PAGE:
            return cards
        offset += PAGE
        time.sleep(PAUSE)


# ── 1. Забираем все дни окна ─────────────────────────────────────
fresh, fresh_tr, failed = [], [], []
for i, day in enumerate(days):
    cards = fetch_day(day)
    if cards is None:
        failed.append(day); print(f"  ❌ {day}: не удалось получить"); continue
    with_orders = {nm: m for nm, m in cards.items() if m['orders'] > 0}
    n_total = sum(m['orders'] for m in cards.values()); s_total = sum(m['osum'] for m in cards.values())
    zero_sum = sum(1 for m in with_orders.values() if m['osum'] == 0)
    print(f"  {day}: карточек с активностью {len(cards)}, с заказами {len(with_orders)} | переходов {int(sum(m['opens'] for m in cards.values()))}, "
          f"корзин {int(sum(m['carts'] for m in cards.values()))}, заказов {int(n_total)}, сумма {s_total:,.0f} ₽"
          + (f"  ⚠️ у {zero_sum} карточек сумма = 0 при ненулевых заказах (известная особенность WB)" if zero_sum else ""))
    for nm, m in sorted(with_orders.items(), key=lambda kv: kv[1]['art'].lower()):
        fresh.append([day, m['art'], nm, int(m['orders']), round(m['osum'], 2)])
    for nm, m in sorted(cards.items(), key=lambda kv: kv[1]['art'].lower()):
        fresh_tr.append([day, m['art'], nm, int(m['opens']), int(m['carts']), int(m['orders']), int(m['buyouts']), round(m['bsum'], 2), int(m['cancels'])])
    if i < len(days) - 1:
        time.sleep(PAUSE)
if fresh_tr and not any(r[3] for r in fresh_tr):
    print("  ⚠️ Переходы в карточку = 0 у ВСЕХ карточек: проверьте имена полей (см. «Поля воронки в ответе WB» выше).")

# ── ЗАЩИТА ОТ ПОТЕРИ ДАННЫХ ──────────────────────────────────────
if failed:
    print(f"\n⛔ Не получены дни: {', '.join(failed)}. Лист НЕ трогаем (иначе заменили бы полные данные неполными).")
    print("   Запусти скрипт ещё раз позже."); sys.exit(1)

# ── 2. Запись листов ─────────────────────────────────────────────
creds = Credentials.from_service_account_info(json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
sh = gspread.authorize(creds).open_by_key(os.environ['SPREADSHEET_ID'])
HEADER = ['Дата', 'Артикул поставщика', 'nmID', 'Заказы, шт', 'Сумма заказов, ₽']
HEADER_TR = ['Дата', 'Артикул поставщика', 'nmID', 'Переходы в карточку, шт', 'Корзина, шт', 'Заказы, шт', 'Выкупы, шт', 'Сумма выкупов, ₽', 'Отмены, шт']
BATCH = 2000


def gs_retry(fn, *args, **kwargs):
    """Квота Google Sheets на запись — ~60 запросов в минуту на сервисный аккаунт (все магазины делят её). При 429/5xx ждём и повторяем."""
    for attempt in range(8):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            code = getattr(e, 'code', None) or getattr(getattr(e, 'response', None), 'status_code', None)
            if code in (429, 500, 502, 503):
                wait = 65 + 15 * attempt
                print(f"  ⏳ Google Sheets {code} (квота записи) — жду {wait}с и повторяю ({attempt + 1}/8)..."); time.sleep(wait); continue
            raise
    raise RuntimeError('Google Sheets: не удалось выполнить запись после 8 попыток')


def norm_day(v):
    v = str(v).strip()
    if len(v) >= 10 and v[4] == '-' and v[7] == '-': return v[:10]
    if len(v) >= 10 and v[2] == '.' and v[5] == '.': return f"{v[6:10]}-{v[3:5]}-{v[0:2]}"
    return v


def get_sheet(name, header):
    try:
        ws = sh.worksheet(name)
        return ws, gs_retry(ws.get_all_values)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(header))
        print(f"  Лист '{name}' создан")
        return ws, []


def write_sheet(name, header, fresh_rows, keep_old_days=None):
    """Строки вне окна не трогаем; окно пишем заново. Если старые строки лежат первыми и без изменений — стираем и пишем только ХВОСТ
    (≈4 запроса вместо ~10+), иначе (первый запуск, изменилась шапка/порядок) — полная перезапись."""
    ws, existing = get_sheet(name, header)
    ex_header = existing[0] if existing else []
    data = existing[1:] if existing else []
    if keep_old_days is not None:                                      # скользящее хранение: убираем из начала листа строки старше cutoff
        cutoff = (now - timedelta(days=keep_old_days)).strftime('%Y-%m-%d')
        k = 0
        while k < len(data) and data[k] and norm_day(data[k][0]) < cutoff: k += 1
        if k and ex_header == header:
            gs_retry(ws.delete_rows, 2, 1 + k); data = data[k:]
            print(f"  [{name}] удалено строк старше {keep_old_days} дн.: {k}")
    keep = [r for r in data if r and not (DATE_FROM <= norm_day(r[0]) <= DATE_TO)]
    print(f"\n[{name}] строк вне окна (не трогаем): {len(keep)}, убрано за окно: {len(data) - len(keep)}, свежих: {len(fresh_rows)}")
    final_rows = keep + fresh_rows
    prefix_ok = bool(existing) and ex_header == header and data[:len(keep)] == keep
    def append_batches(rows):
        for i in range(0, len(rows), BATCH):
            gs_retry(ws.append_rows, rows[i:i + BATCH], value_input_option='USER_ENTERED')
            print(f"  [{name}] записано {min(i + BATCH, len(rows))} из {len(rows)}"); time.sleep(2)
    if prefix_ok:
        first_tail = len(keep) + 2
        if ws.row_count >= first_tail:
            gs_retry(ws.batch_clear, [f"A{first_tail}:{rowcol_to_a1(ws.row_count, len(header))}"])
        append_batches(fresh_rows)
    else:
        print(f"  [{name}] полная перезапись листа (первый запуск или изменилась шапка/порядок строк)")
        gs_retry(ws.clear); gs_retry(ws.append_row, header); append_batches(final_rows)
        try:
            ws.format(f"A1:{rowcol_to_a1(1, len(header))}", {'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
                                                         'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18}})
            ws.freeze(rows=1)
        except Exception: pass
    n_sheet = len(gs_retry(ws.get_all_values)) - 1
    if n_sheet != len(final_rows):
        print(f"⚠ [{name}] в листе {n_sheet} строк, ожидали {len(final_rows)}. Запустите скрипт ещё раз — окно перезабирается целиком, это безопасно."); return False
    print(f"✅ [{name}] итого строк: {len(final_rows)}")
    return True


ok1 = write_sheet('funnel_history', HEADER, fresh)
ok2 = write_sheet('funnel_traffic', HEADER_TR, fresh_tr, keep_old_days=TRAFFIC_KEEP_DAYS)
if not (ok1 and ok2): sys.exit(1)
