import os
import requests
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
date_from = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
date_to = datetime.now().strftime('%Y-%m-%d')

r = requests.post(
    'https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed',
    headers=HEADERS,
    json={'dateFrom': date_from, 'dateTo': date_to, 'limit': 5000},
    timeout=60,
)
data = r.json()
delivery_rows = [i for i in data if i.get('sellerOperName') == 'Доставка']
print(f"Строк 'Доставка': {len(delivery_rows)}")

# по каждому числовому/строково-числовому полю считаем сумму и сколько ненулевых
candidate_fields = set()
for row in delivery_rows[:5]:
    candidate_fields.update(row.keys())

print("\n=== По каждому полю: сумма и кол-во ненулевых (только числовые) ===")
for field in sorted(candidate_fields):
    total = 0.0
    nonzero = 0
    ok = True
    for row in delivery_rows:
        v = row.get(field)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            ok = False
            break
        total += fv
        if fv != 0:
            nonzero += 1
    if ok and nonzero > 0:
        print(f"  {field:30s}: сумма={total:12.2f}  ненулевых={nonzero}/{len(delivery_rows)}")
