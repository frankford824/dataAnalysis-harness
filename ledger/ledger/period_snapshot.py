"""Period evidence identity, independent of unrelated periods' source versions."""
import hashlib
import json

from .content_fingerprint import update_frame
from .storage_integrity import comparable


def fingerprint(engine, model_revision, period, payload, sl, allocation=None):
    if not engine or engine == 'unknown' or 'dirty' in engine:
        return None
    h = hashlib.sha256(json.dumps(
        ['period-snapshot-v1', engine, model_revision, period, comparable(payload)],
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8'))
    # Preserve typed values, source anchors, row order, missingness, quantities,
    # allocation weights and even zero-sum line changes. Totals alone cannot
    # certify that two evidence sets are interchangeable.
    for name in ('facts', 'pricing_gaps', 'coverage_gap_rows',
                 'allocation_evidence', 'order_issue_rows'):
        h.update(('\0' + name + '\0').encode())
        frame = getattr(sl, name, None)
        if frame is None:
            h.update(b'absent')
        elif hasattr(frame, 'iter_slices'):
            update_frame(h, frame)
        else:
            return None
    if allocation:
        h.update(b'\0commission-details\0')
        update_frame(h, allocation[1])
        for name in ('model_json', 'rules_json', 'registry_revision'):
            h.update(json.dumps([name, allocation[2][name]],
                                ensure_ascii=False, sort_keys=True).encode())
    return 'period-v1:' + h.hexdigest()
