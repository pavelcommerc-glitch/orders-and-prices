"""
ДИАГНОСТИКА заказов: сравнивает три источника и показывает, ГДЕ теряются заказы.
Ничего не пишет в таблицу — только читает и печатает.

  API (flag=0, окно N дней)  — то, что видит ежедневный скрипт
  API (flag=1, один день)    — все заказы, датированные этим днём (эталон от API)
  лист orders                — то, что реально записано

Запуск (GitHub Actions: orders_diagnostic.yml, вручную) или локально:
  STORE_TOKEN / WB_TOKEN, STORE_SPREADSHEET_ID / SPREADSHEET_ID, GOOGLE_CREDENTIALS
  DIAG_DATE=2026-10-04   (по умолчанию — 3 дня назад; бери день, которому >2 суток)
  DIAG_DAYS=10           (сколько дней в таблице по дням)
Лимит WB — 1 запрос в минуту на статистику, поэтому между двумя запросами пауза ~65 с.
"""
import os, sys, json, time, requests, gspread
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

TOKEN = os.environ.get('STORE_TOKEN') or os.environ['WB_TOKEN']
SHEET_ID = os.environ.get('STORE_SPREADSHEET_ID') or os.environ['SPREADSHEET_ID']
HEADERS = {'Authorization': TOKEN, 'Content-Type': 'application/json'}
URL = 'https://statistics-api.wildberries.ru/api/v1/supplier/orders'

DIAG_DATE = os.environ.get('DIAG_DATE', '').strip() or (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
DAYS = int(os.environ.get('DIAG_DAYS', '').strip() or 10)


def fetch(params, label):
    for attempt in range(5):
        try:
            r = requests.get(URL, headers=HEADERS, params=params, timeout=60)
        except Exception as e:
            print(f"  [{label}] исключение: {e}"); time.sleep(10); continue
        if r.status_code == 429:
            w = 65 * (attempt + 1); print(f"  [{label}] 429 — жду {w}с"); time.sleep(w); continue
        if r.status_code >= 500:
            w = 30 * (attempt + 1); print(f"  [{label}] {r.status_code} — повтор через {w}с"); time.sleep(w); continue
        if r.status_code == 200:
            d = r.json()
            return d if isinstance(d, list) else (d or [])
        print(f"  [{label}] ошибка {r.status_code}: {r.text[:200]}"); return None
    return None


def day_of(s):  # 'YYYY-MM-DDTHH:MM:SS' -> 'YYYY-MM-DD'
    return str(s or '')[:10]


def parse_sheet_day(v):
    v = str(v).strip()
    if len(v) >= 10 and v[4] == '-' and v[7] == '-':
        return v[:10]
    if len(v) >= 10 and v[2] == '.' and v[5] == '.':      # 05.10.2026
        return f"{v[6:10]}-{v[3:5]}-{v[0:2]}"
    try:                                                    # серийный номер даты Sheets
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).strftime('%Y-%m-%d')
    except Exception:
        return ''


def hours_between(a, b):
    try:
        fa = datetime.fromisoformat(str(a)[:19]); fb = datetime.fromisoformat(str(b)[:19])
        return (fb - fa).total_seconds() / 3600
    except Exception:
        return None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else None


print(f"Диагностика заказов | день для разбора: {DIAG_DATE} | таблица по дням: последние {DAYS}")

# ── лист orders ──────────────────────────────────────────────────
creds = Credentials.from_service_account_info(json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive'])
ws = gspread.authorize(creds).open_by_key(SHEET_ID).worksheet('orders')
vals = ws.get_all_values()
sheet_day_cnt, sheet_srids_by_day = Counter(), defaultdict(set)
for row in vals[1:]:
    if len(row) > 10:
        d = parse_sheet_day(row[0])
        sheet_day_cnt[d] += 1
        if row[10]: sheet_srids_by_day[d].add(row[10])
print(f"В листе orders строк: {len(vals) - 1}")

# ── запрос 1: flag=0, окно (как у ежедневного скрипта) ───────────
start = (datetime.now() - timedelta(days=DAYS)).strftime('%Y-%m-%d')
w0 = fetch({'dateFrom': start + 'T00:00:00', 'flag': 0}, 'flag=0')
if w0 is None:
    print("❌ flag=0: API не ответил"); sys.exit(1)
print(f"flag=0 с {start}: получено {len(w0)} строк")
srids0 = [str(o.get('srid', '')) for o in w0]
print(f"  дублей srid внутри ответа: {len(srids0) - len(set(srids0))}, пустых srid: {sum(1 for s in srids0 if not s)}")
api_day_cnt, api_day_cancel = Counter(), Counter()
for o in w0:
    d = day_of(o.get('date')); api_day_cnt[d] += 1
    if o.get('isCancel'): api_day_cancel[d] += 1

print("\nПО ДНЯМ (API flag=0 vs лист):")
print(f"{'день':<12}{'API':>6}{'в т.ч. отмен.':>15}{'лист':>7}{'API-лист':>10}")
for d in sorted(api_day_cnt)[-DAYS:]:
    print(f"{d:<12}{api_day_cnt[d]:>6}{api_day_cancel[d]:>15}{sheet_day_cnt.get(d, 0):>7}{api_day_cnt[d] - sheet_day_cnt.get(d, 0):>10}")

# ── запрос 2: flag=1, один день ──────────────────────────────────
print("\nждём 65с (лимит 1 запрос/мин)...")
time.sleep(65)
w1 = fetch({'dateFrom': DIAG_DATE + 'T00:00:00', 'flag': 1}, 'flag=1')
if w1 is None:
    print("❌ flag=1: API не ответил"); sys.exit(1)
w1 = [o for o in w1 if day_of(o.get('date')) == DIAG_DATE]
A = {str(o.get('srid', '')): o for o in w1 if o.get('srid')}
B = {str(o.get('srid', '')) for o in w0 if day_of(o.get('date')) == DIAG_DATE}
C = sheet_srids_by_day.get(DIAG_DATE, set())

def fsum(os_, f): return sum(float(o.get(f) or 0) for o in os_)
print(f"\nДЕНЬ {DIAG_DATE}:")
print(f"  A) API flag=1 (эталон API): {len(A)} заказов | отменено {sum(1 for o in A.values() if o.get('isCancel'))}")
print(f"     сумма totalPrice={fsum(A.values(),'totalPrice'):,.0f}  priceWithDisc={fsum(A.values(),'priceWithDisc'):,.0f}  finishedPrice={fsum(A.values(),'finishedPrice'):,.0f}")
print(f"  B) API flag=0 (окно, что видит скрипт): {len(B)}")
print(f"  C) лист orders: {len(C)}")
print(f"  в API, нет в листе (A−C): {len(set(A) - C)} | в листе, нет в API (C−A): {len(C - set(A))} | в A, нет в B (A−B): {len(set(A) - B)}")

miss = [A[s] for s in set(A) - C]
if miss:
    print(f"\nЗаказы, которые есть в API, но НЕ записаны в лист ({len(miss)}):")
    print(f"  сумма priceWithDisc: {fsum(miss,'priceWithDisc'):,.0f} | отменённых среди них: {sum(1 for o in miss if o.get('isCancel'))}")
    print("  по складам:", Counter(o.get('warehouseName', '') for o in miss).most_common(5))
    print("  по часам заказа:", sorted(Counter(int(str(o.get('date'))[11:13] or 0) for o in miss).items()))
    lag_miss = median([hours_between(o.get('date'), o.get('lastChangeDate')) for o in miss])
    present = [A[s] for s in set(A) & C]
    lag_pres = median([hours_between(o.get('date'), o.get('lastChangeDate')) for o in present])
    print(f"  медиана (lastChangeDate − date), часов: пропущенные={lag_miss}, записанные={lag_pres}")
    for o in miss[:5]:
        print("  пример:", o.get('srid'), o.get('date'), o.get('lastChangeDate'), o.get('supplierArticle'), o.get('isCancel'))

print("\nКАК ЧИТАТЬ:")
print("  • A ≈ цифре из кабинета, а лист меньше (A−C > 0) → теряет наш скрипт/запись.")
print("  • A заметно меньше цифры из кабинета → сам Statistics API отдаёт не всё; скрипт ни при чём.")
print("  • A−B > 0 → окно/flag=0 не видит часть заказов этого дня (проблема параметров запроса).")
