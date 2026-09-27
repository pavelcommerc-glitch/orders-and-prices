"""
Ищет строку с sellerOperName == "Доставка" (или похожим) и печатает её
целиком — чтобы найти реальное поле с суммой логистики в рублях.

Запуск:
  export WB_TOKEN='...'   (категория "Финансы")
  python fetch_finance_delivery_diagnostic.py
"""

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
print(f"HTTP статус: {r.status_code}")
if r.status_code != 200:
    print(r.text[:1000])
    exit(1)

data = r.json()
print(f"Строк получено: {len(data)}")

# смотрим, какие вообще бывают sellerOperName
oper_names = {}
for item in data:
    name = item.get('sellerOperName', '')
    oper_names[name] = oper_names.get(name, 0) + 1
print("\n=== Все встреченные sellerOperName и сколько раз ===")
for name, count in sorted(oper_names.items(), key=lambda x: -x[1]):
    print(f"  {name!r}: {count}")

# ищем строку логистики
target = None
for item in data:
    name = item.get('sellerOperName', '')
    if 'доставк' in name.lower() or 'логист' in name.lower():
        target = item
        break

if target:
    print(f"\n=== Полная строка с sellerOperName={target.get('sellerOperName')!r} ===\n")
    for k, v in target.items():
        print(f"{k}: {v!r}")
else:
    print("\n⚠️ Не нашёл строку с 'доставка'/'логист' в названии — смотри список sellerOperName выше,")
    print("   возможно, называется иначе. Пришли этот список, поищем по нему.")
