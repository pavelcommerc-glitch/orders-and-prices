"""
Пробуем попросить у нового Finance API ЕЖЕДНЕВНЫЙ отчёт вместо
еженедельного — добавляем параметр period="daily" в тело запроса
(по аналогии со старым методом, где такой параметр был).

Запуск:
  export WB_TOKEN='...'
  python fetch_finance_daily_test.py
"""

import os
import requests
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}

date_from = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
date_to = datetime.now().strftime('%Y-%m-%d')

for period_value in ['daily', 'weekly', None]:
    body = {'dateFrom': date_from, 'dateTo': date_to, 'limit': 100}
    if period_value:
        body['period'] = period_value
    print(f"\n{'='*60}\nПробуем period={period_value!r}")
    r = requests.post(
        'https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed',
        headers=HEADERS, json=body, timeout=30,
    )
    print(f"HTTP статус: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"Строк: {len(data)}")
        if data:
            dates = set((item.get('dateFrom'), item.get('dateTo'), item.get('reportType')) for item in data)
            print(f"Уникальные (dateFrom, dateTo, reportType) в ответе: {dates}")
    else:
        print(r.text[:400])
