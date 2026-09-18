"""Slim commission rows for the amount-summary list.

The live ``run.result`` blob keeps the full product tree. The summary list
only needs people, statement totals, and per-link duty/rate. Persist that
slice once so later opens do not parse the 18GB workspace again.
"""
from __future__ import annotations

import json


def compact_statement(statement):
    rows = []
    for item in statement or []:
        node_id = item.get('id')
        if not node_id:
            continue
        rows.append({
            'id': node_id,
            'value': item.get('value'),
            'available': item.get('available', True),
        })
    return rows


def slim_products(products):
    if not products:
        return None
    return [{
        'total_rate': product.get('total_rate'),
        'people': [
            {'person_id': crew.get('person_id'), 'duty': crew.get('duty')}
            for crew in product.get('people') or [] if crew.get('person_id')
        ],
    } for product in products]


def from_result(result):
    commission = dict(result.get('commission') or {})
    products = slim_products(commission.pop('products', None))
    manual = result.get('manual_cost')
    return {
        'overview_json': json.dumps({**result, 'commission': commission}, ensure_ascii=False),
        'commission_json': json.dumps(commission, ensure_ascii=False),
        'products_slim_json': json.dumps(products, ensure_ascii=False) if products else None,
        'statement_json': json.dumps(compact_statement(result.get('statement')), ensure_ascii=False),
        'store_name': result.get('store') or '',
        'manual_cost_json': json.dumps(manual, ensure_ascii=False) if manual is not None else None,
    }


def producer_ids(commission):
    """People who are 做货 on at least one product in this run."""
    found = set()
    for product in commission.get('products') or []:
        for crew in product.get('people') or []:
            pid = crew.get('person_id')
            if pid and (crew.get('duty') or 'produce') == 'produce':
                found.add(pid)
    return found


def save(conn, run_id, result, *, kind='run', payload_text=None):
    text = payload_text if payload_text is not None else json.dumps(result, ensure_ascii=False)
    row = from_result(result)
    conn.execute(
        "INSERT OR REPLACE INTO run_report_slice("
        "run_id,payload_kind,payload_bytes,commission_json,products_slim_json,"
        "statement_json,store_name,manual_cost_json,overview_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, kind, len(text), row['commission_json'], row['products_slim_json'],
         row['statement_json'], row['store_name'], row['manual_cost_json'], row['overview_json']),
    )


def slim_products_sql(payload):
    """Keep rate + person + duty; drop order lists and product names."""
    return (
        f"(SELECT json_group_array(json_object("
        f"'total_rate',json_extract(prod.value,'$.total_rate'),"
        f"'people',(SELECT json_group_array(json_object("
        f"'person_id',json_extract(crew.value,'$.person_id'),"
        f"'duty',json_extract(crew.value,'$.duty'))) "
        f"FROM json_each(prod.value,'$.people') crew))) "
        f"FROM json_each({payload},'$.commission.products') prod)"
    )


def compact_statement_sql(payload):
    return (
        f"(SELECT json_group_array(json_object("
        f"'id',json_extract(node.value,'$.id'),"
        f"'value',json_extract(node.value,'$.value'),"
        f"'available',coalesce(json_extract(node.value,'$.available'),1))) "
        f"FROM json_each({payload},'$.statement') node)"
    )
