"""
ДИАГНОСТИКА №2: заказы за день из «Воронки продаж» (Analytics API — те же данные,
что в кабинете) против Statistics API (то, что пишет ежедневный скрипт).
Ничего не пишет в таблицу.

Токен должен иметь категории «Аналитика» И «Статистика».
  WB_TOKEN (или STORE_TOKEN), DIAG_DATE=2026-10-04 (день старше 2 суток), DIAG_STORE (для лога)

Методы (по документации dev.wildberries.ru/openapi/analytics):
  POST seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/grouped/history   — по дням, все карточки
  POST seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products          — по каждой карточке
Точные схемы запросов я по документации не сверял до конца — поэтому каждый шаг независим,
а при ошибке скрипт печатает ответ WB целиком, чтобы поправить запрос.
"""
import os, sys, json, time, requests
from collections import defaultdict
from datetime import datetime, timedelta

TOKEN = os.environ.get('STORE_TOKEN') or os.environ['WB_TOKEN']
HEADERS = {'Authorization': TOKEN, 'Content-Type': 'application/json'}
AN = 'https://seller-analytics-api.wildberries.ru'
STAT = 'https://statistics-api.wildberries.ru/api/v1/supplier/orders'
D = os.environ.get('DIAG_DATE', '').strip() or (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
print(f"Воронка vs Statistics API | магазин: {os.environ.get('DIAG_STORE','').strip() or 'не передан'} | день: {D}")


def call(method, url, label, **kw):
    """(ok, data). Повторы на 429/5xx; при другой ошибке печатает ответ целиком."""
    for attempt in range(5):
        try:
            r = requests.request(method, url, headers=HEADERS, timeout=60, **kw)
        except Exception as e:
            print(f"  [{label}] исключение: {e}"); time.sleep(10); continue
        if r.status_code == 429:
            w = 30 * (attempt + 1); print(f"  [{label}] 429 — жду {w}с"); time.sleep(w); continue
        if r.status_code >= 500:
            w = 30 * (attempt + 1); print(f"  [{label}] {r.status_code} — повтор через {w}с"); time.sleep(w); continue
        if r.status_code == 200:
            try: return True, r.json()
            except Exception: return True, []
        print(f"  [{label}] ОШИБКА {r.status_code}: {r.text[:700]}")
        return False, None
    return False, None


def find_rows(x, out):
    """Рекурсивно собирает словари, где есть и date, и orderCount (формат ответа гибкий)."""
    if isinstance(x, dict):
        if 'orderCount' in x and 'date' in x: out.append(x)
        for v in x.values(): find_rows(v, out)
    elif isinstance(x, list):
        for v in x: find_rows(v, out)
    return out


# ── 1. Воронка по дням, все карточки (итог по магазину) ──────────
print("\n[1] Воронка: итог за день (grouped/history)")
period = {'start': D, 'end': D}
ok, data = call('POST', f'{AN}/api/analytics/v3/sales-funnel/grouped/history', 'funnel-day', json={
    'selectedPeriod': period, 'nmIds': [], 'brandNames': [], 'subjectIds': [], 'tagIds': [],
    'skipDeletedNm': False, 'aggregationLevel': 'day'})
f_total_n = f_total_sum = None
if ok:
    rows = [r for r in find_rows(data, []) if str(r.get('date', ''))[:10] == D]
    print(f"  строк за {D}: {len(rows)} (если >1 — это группы, см. ниже)")
    f_total_n = sum(int(r.get('orderCount') or 0) for r in rows)
    f_total_sum = sum(float(r.get('orderSum') or 0) for r in rows)
    print(f"  ► ВОРОНКА: заказов {f_total_n} на сумму {f_total_sum:,.0f} ₽")
    if not rows: print("  ответ (начало):", json.dumps(data, ensure_ascii=False)[:600])
time.sleep(25)

# ── 2. Воронка по каждой карточке ────────────────────────────────
print("\n[2] Воронка по карточкам (products)")
f_by_nm, f_art = {}, {}
offset = 0
while True:
    ok, data = call('POST', f'{AN}/api/analytics/v3/sales-funnel/products', 'funnel-products', json={
        'selectedPeriod': period, 'nmIds': [], 'brandNames': [], 'subjectIds': [], 'tagIds': [],
        'skipDeletedNm': False, 'orderBy': {'field': 'orderCount', 'mode': 'desc'}, 'limit': 1000, 'offset': offset})
    if not ok: break
    d = data.get('data', data) if isinstance(data, dict) else data
    prods = d.get('products', []) if isinstance(d, dict) else (d or [])
    for p in prods:
        pr = p.get('product', {}); sel = (p.get('statistic') or {}).get('selected', {})
        nm = pr.get('nmId'); n = int(sel.get('orderCount') or 0)
        if nm is not None and n:
            f_by_nm[nm] = (n, float(sel.get('orderSum') or 0)); f_art[nm] = pr.get('vendorCode', '')
    print(f"  offset={offset}: карточек {len(prods)}")
    if len(prods) < 1000: break
    offset += 1000; time.sleep(25)
if f_by_nm:
    print(f"  ► карточек с заказами: {len(f_by_nm)}, заказов {sum(v[0] for v in f_by_nm.values())}")

# ── 3. Statistics API за тот же день ─────────────────────────────
print("\n[3] Statistics API (flag=1, заказы дня)")
ok, orders = call('GET', STAT, 'statistics', params={'dateFrom': D + 'T00:00:00', 'flag': 1})
s_by_nm, s_art = defaultdict(lambda: [0, 0.0]), {}
if ok:
    orders = [o for o in (orders or []) if str(o.get('date', ''))[:10] == D]
    for o in orders:
        nm = o.get('nmId'); s_by_nm[nm][0] += 1; s_by_nm[nm][1] += float(o.get('priceWithDisc') or 0)
        s_art[nm] = o.get('supplierArticle', '')
    print(f"  ► STATISTICS: заказов {len(orders)} на сумму (priceWithDisc) {sum(v[1] for v in s_by_nm.values()):,.0f} ₽, отменено {sum(1 for o in orders if o.get('isCancel'))}")

# ── Сравнение ────────────────────────────────────────────────────
print("\n[ИТОГ]")
if f_total_n is not None and ok:
    sn = sum(v[0] for v in s_by_nm.values()); ss = sum(v[1] for v in s_by_nm.values())
    print(f"  Воронка {f_total_n} / {f_total_sum:,.0f} ₽   Statistics {sn} / {ss:,.0f} ₽   разница {f_total_n - sn} шт ({(f_total_n - sn) / max(f_total_n, 1):.1%}) / {f_total_sum - ss:,.0f} ₽")
if f_by_nm and ok:
    diffs = []
    for nm in set(f_by_nm) | set(s_by_nm):
        fn = f_by_nm.get(nm, (0, 0))[0]; sn_ = s_by_nm[nm][0] if nm in s_by_nm else 0
        if fn != sn_: diffs.append((fn - sn_, nm, f_art.get(nm) or s_art.get(nm, ''), fn, sn_))
    diffs.sort(reverse=True)
    eq = len(set(f_by_nm) | set(s_by_nm)) - len(diffs)
    print(f"  карточек: совпало {eq}, в воронке больше {sum(1 for d in diffs if d[0] > 0)}, в Statistics больше {sum(1 for d in diffs if d[0] < 0)}")
    print("  где воронка БОЛЬШЕ (разница, nmId, артикул, воронка, statistics):")
    for d in diffs[:20]: print("   ", d)
print("\nКАК ЧИТАТЬ: Воронка ≈ цифре из кабинета, а Statistics меньше → Statistics API не отдаёт часть заказов;")
print("берём заказы из воронки. Если Воронка ≈ Statistics, а кабинет больше — смотрим другой отчёт кабинета.")
