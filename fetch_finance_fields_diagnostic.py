"""
Печатает ПОЛНЫЙ список полей одной строки "Продажа" из нового Finance API —
без обрезки, чтобы найти реальное поле с суммой логистики (предыдущая
диагностика обрывалась на "warehouseLogistic...", а "deliveryAmount"
оказался количеством доставок, не суммой в рублях).

Запуск:
  export WB_TOKEN='...'   (категория "Финансы")
  python fetch_finance_fields_diagnostic.py
"""

import os
import json
import requests
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}

date_from = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
date_to = datetime.now().strftime('%Y-%m-%d')

r = requests.post(
    'https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed',
    headers=HEADERS,
    json={'dateFrom': date_from, 'dateTo': date_to, 'limit': 500},
    timeout=30,
)
print(f"HTTP статус: {r.status_code}")
if r.status_code != 200:
    print(r.text[:1000])
    exit(1)

data = r.json()
print(f"Строк получено: {len(data)}")

# ищем строку с sellerOperName == "Продажа" и ненулевым forPay, чтобы поля
# были реально заполнены, а не нулевые
target = None
for item in data:
    if item.get('sellerOperName') == 'Продажа' and float(item.get('forPay', 0) or 0) != 0:
        target = item
        break
if not target and data:
    target = data[0]

print("\n=== ПОЛНЫЙ список ключей и значений одной строки ===\n")
for k, v in target.items():
    print(f"{k}: {v!r}")

print("\n\n=== Все ключи, где название намекает на логистику/доставку ===")
for k, v in target.items():
    if any(w in k.lower() for w in ['deliver', 'logist', 'rebill', 'warehous']):
        print(f"{k}: {v!r}")
