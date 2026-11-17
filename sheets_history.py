"""Retry Google Sheets requests and publish completed history updates."""
import random
import time
from datetime import date, timedelta

from gspread.exceptions import APIError, WorksheetNotFound
from gspread.utils import rowcol_to_a1
from requests.exceptions import ConnectionError, Timeout


def sheets_retry(operation, *args, **kwargs):
    for attempt in range(5):
        try:
            return operation(*args, **kwargs)
        except (APIError, ConnectionError, Timeout) as error:
            delay = min(120, 5 * 2 ** attempt)
            if isinstance(error, APIError):
                status = error.response.status_code
                quota = status == 429 or (
                    status == 403
                    and any(reason in str(error) for reason in (
                        'rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded',
                    ))
                )
                if not quota and status not in (408, 500, 502, 503, 504):
                    raise
                if quota:
                    delay = max(delay, 65)
                retry_after = error.response.headers.get('Retry-After', '')
                if retry_after.isdecimal():
                    delay = max(delay, int(retry_after))
            if attempt == 4:
                raise
            delay += random.uniform(0, 10)
            print(f"Google Sheets: {operation.__name__}, повтор {attempt + 1}/4 через {delay:.0f} с", flush=True)
            time.sleep(delay)


def sheet_or_create(book, title, width, *, hidden=False):
    def get_or_add():
        try:
            return book.worksheet(title)
        except WorksheetNotFound:
            if not hidden:
                return book.add_worksheet(title=title, rows=100, cols=width)
            response = book.batch_update({'requests': [{'addSheet': {'properties': {
                'title': title, 'hidden': True,
                'gridProperties': {'rowCount': 1, 'columnCount': width},
            }}}]})
            return book.get_worksheet_by_id(response['replies'][0]['addSheet']['properties']['sheetId'])

    return sheets_retry(get_or_add)


def normalize_date(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
    value = str(value).strip()
    if len(value) >= 10 and value[4] == '-' and value[7] == '-':
        return value[:10]
    if len(value) >= 10 and value[2] == '.' and value[5] == '.':
        return f'{value[6:10]}-{value[3:5]}-{value[:2]}'
    return value


def grid_range(sheet, start, end, width):
    return {'sheetId': sheet.id, 'startRowIndex': start, 'endRowIndex': end,
            'startColumnIndex': 0, 'endColumnIndex': width}


def prepare_update(book, title, header, fresh, *, date_from='', date_to='',
                   cutoff='', frozen_columns=0):
    """Stage changed rows and return the requests that publish them together."""
    width = len(header)
    target = sheet_or_create(book, title, width)
    existing = sheets_retry(target.get_all_values, value_render_option='UNFORMATTED_VALUE')
    retained = []
    if date_from:
        for row in existing[1:]:
            day = normalize_date(row[0]) if row else ''
            if day and (not cutoff or day >= cutoff) and not date_from <= day <= date_to:
                retained.append(row)

    desired = [header] + retained + fresh
    first = 0
    prefix = [header] + retained
    if existing[:len(prefix)] == prefix:
        first = len(prefix)

    end = len(desired)
    requests = []
    if end > target.row_count or width > target.col_count:
        requests.append({'updateSheetProperties': {
            'properties': {'sheetId': target.id, 'gridProperties': {
                'rowCount': max(end, target.row_count),
                'columnCount': max(width, target.col_count),
            }},
            'fields': 'gridProperties(rowCount,columnCount)',
        }})
    replacement = desired[first:]
    if replacement:
        stage = sheet_or_create(book, f'_rnp_stage_{title}', width, hidden=True)
        stage_rows = len(replacement)
        if stage_rows > stage.row_count or width > stage.col_count:
            sheets_retry(stage.resize, rows=max(stage_rows, stage.row_count),
                         cols=max(width, stage.col_count))
        if retained and first == 0:
            # RAW preserves historical strings and numeric date serials.
            write_stage(stage, desired[:len(prefix)], width, 0, 'RAW')
            write_stage(stage, fresh, width, len(prefix), 'USER_ENTERED')
        else:
            write_stage(stage, replacement, width, 0, 'USER_ENTERED')
        requests.append({'copyPaste': {
            'source': grid_range(stage, 0, stage_rows, width),
            'destination': grid_range(target, first, end, width),
            'pasteType': 'PASTE_VALUES',
            'pasteOrientation': 'NORMAL',
        }})
        date_range = grid_range(target, max(first, 1), end, 1)
        if date_range['startRowIndex'] < end:
            requests.append({'repeatCell': {
                'range': date_range,
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy-mm-dd'}}},
                'fields': 'userEnteredFormat.numberFormat',
            }})
    if len(existing) > end:
        requests.append({'updateCells': {
            'range': grid_range(target, end, len(existing), width),
            'fields': 'userEnteredValue',
        }})
    if first == 0:
        requests.append({'repeatCell': {
            'range': grid_range(target, 0, 1, width),
            'cell': {'userEnteredFormat': {
                'textFormat': {'bold': True, 'foregroundColor': {'red': 1, 'green': 1, 'blue': 1}},
                'backgroundColor': {'red': 0.18, 'green': 0.18, 'blue': 0.18},
            }},
            'fields': 'userEnteredFormat(textFormat,backgroundColor)',
        }})
        freeze = {'frozenRowCount': 1}
        if frozen_columns:
            freeze['frozenColumnCount'] = frozen_columns
        requests.append({'updateSheetProperties': {
            'properties': {'sheetId': target.id, 'gridProperties': freeze},
            'fields': 'gridProperties(' + ','.join(freeze) + ')',
        }})
    print(f'{title}: подготовлено {len(replacement)} строк, итог {end - 1} строк данных')
    return requests


def write_stage(stage, rows, width, offset, input_option):
    batch_size = 1000
    for start in range(0, len(rows), batch_size):
        batch = []
        for row in rows[start:start + batch_size]:
            values = ['' if value is None else value for value in row[:width]]
            batch.append(values + [''] * (width - len(values)))
        first = offset + start + 1
        last_cell = rowcol_to_a1(first + len(batch) - 1, width)
        sheets_retry(stage.update, values=batch, range_name=f'A{first}:{last_cell}',
                     value_input_option=input_option)


def publish_updates(book, requests):
    if requests:
        sheets_retry(book.batch_update, {'requests': requests})
