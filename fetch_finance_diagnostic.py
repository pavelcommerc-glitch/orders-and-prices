"""
МИНИМАЛЬНАЯ ДИАГНОСТИКА — один голый запрос, без ретраев, без параллели,
без остальных скриптов. Запускай ЛОКАЛЬНО (не через GitHub Actions), чтобы
исключить, что дело в параллельных матричных джобах или соседних crontab.

Запуск:
  export WB_TOKEN='...'
  python fetch_finance_diagnostic.py
"""

import os
import requests
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}

date_from = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
date_to = datetime.now().strftime('%Y-%m-%d')

print(f"→ Один запрос к reportDetailByPeriod (dateFrom={date_from}, dateTo={date_to})...")
print(f"→ Токен (первые 15 символов): {WB_TOKEN[:15]}...")

r = requests.get(
    'https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod',
    headers=HEADERS,
    params={'dateFrom': date_from, 'dateTo': date_to, 'limit': 10, 'rrdid': 0},
    timeout=30,
)

print(f"\nHTTP статус: {r.status_code}")
print(f"Заголовки ответа (ищем что-то про лимиты):")
for k, v in r.headers.items():
    if 'limit' in k.lower() or 'retry' in k.lower() or 'rate' in k.lower():
        print(f"  {k}: {v}")

if r.status_code == 200:
    data = r.json()
    print(f"\n✅ Успех! Строк в ответе: {len(data)}")
else:
    print(f"\n❌ Текст ответа: {r.text[:1000]}")

print("\n" + "="*60)
print("→ Для сравнения: тот же токен на ДРУГОЙ метод статистики (sales)...")
r2 = requests.get(
    'https://statistics-api.wildberries.ru/api/v1/supplier/sales',
    headers=HEADERS,
    params={'dateFrom': date_from, 'flag': 0},
    timeout=30,
)
print(f"HTTP статус (sales): {r2.status_code}")
if r2.status_code == 200:
    print(f"✅ sales работает нормально — значит проблема именно в reportDetailByPeriod, не в токене вообще")
else:
    print(f"❌ {r2.text[:500]}")
    print("Если тут тоже не 200 — токен/аккаунт залочен на ВСЮ статистику, не только на finance")
