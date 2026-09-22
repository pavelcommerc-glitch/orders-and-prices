"""
ДИАГНОСТИКА — старый метод GET /api/v5/supplier/reportDetailByPeriod
отключён Wildberries (объявлено к отключению 15 июля 2026, судя по всему
отключали постепенно — отсюда и 429 вчера-позавчера, а сегодня уже 404).

Новый метод живёт в отдельной категории токена "Финансы" (не Statistics!):
  POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed
  (точный путь пробуем несколько вариантов ниже — открытой документации
  с примерами не нашлось, поэтому угадываем по объявленным release notes)

Также проверяем отдельный метод специально под эквайринг — может быть,
это и есть то поле, которое мы не могли найти в старом отчёте:
  POST https://finance-api.wildberries.ru/api/finance/v1/acquiring/detailed

ВАЖНО: токену нужна категория "Финансы" — проверь в личном кабинете WB,
что она включена (Настройки → Доступ к API), иначе будет 401/403.

Ничего не пишет в Google Sheets — только печатает сырые ответы, чтобы
увидеть реальную структуру. Пришли весь вывод обратно.

Запуск:
  export WB_TOKEN='...'   (токен с категорией "Финансы"!)
  pip install requests
  python fetch_finance_v2_diagnostic.py
"""

import os
import json
import requests
from datetime import datetime, timedelta

WB_TOKEN = os.environ['WB_TOKEN']
HEADERS = {'Authorization': WB_TOKEN, 'Content-Type': 'application/json'}
FINANCE_URL = 'https://finance-api.wildberries.ru'

date_from = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
date_to = datetime.now().strftime('%Y-%m-%d')


def try_get(url, params=None):
    print(f"\n{'='*70}\nGET {url}")
    print(f"params: {params}")
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    except Exception as e:
        print(f"❌ Исключение: {e}")
        return None
    print(f"HTTP статус: {r.status_code}")
    if r.status_code == 200:
        return r.json()
    print(f"Текст ответа: {r.text[:1000]}")
    return None


def try_post(url, body=None):
    print(f"\n{'='*70}\nPOST {url}")
    print(f"body: {json.dumps(body, ensure_ascii=False) if body else '{}'}")
    try:
        r = requests.post(url, headers=HEADERS, json=body or {}, timeout=30)
    except Exception as e:
        print(f"❌ Исключение: {e}")
        return None
    print(f"HTTP статус: {r.status_code}")
    if r.status_code == 200:
        return r.json()
    print(f"Текст ответа: {r.text[:1000]}")
    return None


def dump(data, label):
    if data is None:
        return
    print(f"\n→ {label} — сырой ответ (обрезано до 2500 символов):")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:2500])


# ── Пробуем несколько вероятных вариантов пути для основного отчёта ──
print("### ПРОВЕРКА 1: список финансовых отчётов ###")
d1 = try_get(f'{FINANCE_URL}/api/finance/v1/sales-reports', params={'dateFrom': date_from, 'dateTo': date_to})
dump(d1, "sales-reports (список)")

print("\n\n### ПРОВЕРКА 2: детализация отчёта по периоду (GET, camelCase-параметры) ###")
d2 = try_get(f'{FINANCE_URL}/api/finance/v1/sales-reports/detailed',
             params={'dateFrom': date_from, 'dateTo': date_to, 'limit': 10, 'rrdId': 0})
dump(d2, "sales-reports/detailed (GET)")

print("\n\n### ПРОВЕРКА 3: то же самое, но POST ###")
d3 = try_post(f'{FINANCE_URL}/api/finance/v1/sales-reports/detailed',
              body={'dateFrom': date_from, 'dateTo': date_to, 'limit': 10})
dump(d3, "sales-reports/detailed (POST)")

print("\n\n### ПРОВЕРКА 4: отдельный метод по эквайрингу ###")
d4 = try_get(f'{FINANCE_URL}/api/finance/v1/acquiring/detailed',
             params={'dateFrom': date_from, 'dateTo': date_to})
dump(d4, "acquiring/detailed (GET)")

d4b = try_post(f'{FINANCE_URL}/api/finance/v1/acquiring/detailed',
               body={'dateFrom': date_from, 'dateTo': date_to})
dump(d4b, "acquiring/detailed (POST)")

print("\n\n### ПРОВЕРКА 5: старый метод — просто чтобы подтвердить, что он реально мёртв ###")
d5 = try_get('https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod',
             params={'dateFrom': date_from, 'dateTo': date_to, 'limit': 10, 'rrdid': 0})
dump(d5, "старый reportDetailByPeriod")

print("\n\n" + "="*70)
print("ГОТОВО. Пришли ВЕСЬ вывод целиком (не только последнюю часть) — по нему")
print("допишу финальную версию скрипта с точным маппингом полей.")
