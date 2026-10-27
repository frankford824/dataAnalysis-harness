"""Model-visible tools expose references, never raw business documents."""
from __future__ import annotations
import json
import re

REFERENCE = re.compile(r'\{\{evidence:(ev_[0-9a-f]{32}):([a-z_]+)\}\}')
SENSITIVE = re.compile(r'(?:\b\d{11,}\b|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|Bearer\s+\S+|sk-[A-Za-z0-9_-]+)')


def check_outbound(payload):
    text = json.dumps(payload, ensure_ascii=False, default=str)
    # Opaque evidence/call IDs are protocol identifiers, not original order IDs.
    scrubbed = re.sub(r'\bev_[0-9a-f]{32}\b', '[reference]', text)
    if SENSITIVE.search(scrubbed):
        raise ValueError('Sensitive outbound content rejected')
    return payload


def render(text, state, input_sha):
    """All numeric business prose must cite a locally retained evidence field."""
    def value(match):
        record = state.read_evidence(match[1], input_sha)
        item = record.get(match[2])
        if not isinstance(item, (int,float,str)) or isinstance(item,bool):
            raise ValueError('Citation has no scalar evidence field')
        return str(item)
    remainder = REFERENCE.sub('', text)
    if re.search(r'\d|[零一二三四五六七八九十百千万亿]+(?:元|块|笔|家|%)', remainder):
        raise ValueError('Uncited numeric assertion')
    if '{{evidence:' in remainder:
        raise ValueError('Malformed evidence citation')
    return REFERENCE.sub(value, text)
