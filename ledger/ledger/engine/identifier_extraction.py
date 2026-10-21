"""Apply narrowly scoped, source-declared order identifier evidence."""
import re
import polars as pl
from .rules import Matcher,text_expr


def apply(frame, template, notes):
    for rule in template.identifier_extractions:
        if rule.when.field not in frame.columns:
            continue
        matcher=Matcher(rule.when)
        text=text_expr(frame.schema[rule.when.field],rule.when.field)
        if matcher.vectorizable:
            selected=matcher.mask(text)
        else:
            selected=pl.col(rule.when.field).map_elements(lambda v:matcher.apply(v) is not None,return_dtype=pl.Boolean).fill_null(False)
        source=pl.col(rule.from_role).cast(pl.String) if rule.from_role in frame.columns else pl.lit(None,dtype=pl.String)
        # Collect every candidate before choosing: picking the first order from
        # conflicting remarks would silently charge the wrong order.
        pattern=re.compile(rule.extract);valid=re.compile(rule.value_pattern)
        def unique(value):
            found=set(pattern.findall(value or ''))
            return next(iter(found)) if len(found)==1 and valid.fullmatch(next(iter(found))) else None
        value=pl.when(selected).then(source).otherwise(None).map_elements(unique,return_dtype=pl.String)
        original=pl.col(rule.target).cast(pl.String) if rule.target in frame.columns else pl.lit(None,dtype=pl.String)
        raw='__raw_'+rule.target
        if raw not in frame.columns:frame=frame.with_columns(original.alias(raw))
        frame=frame.with_columns(pl.when(selected).then(value).otherwise(original).alias(rule.target))
        counts=frame.select(selected.sum().alias('selected'),(selected & pl.col(rule.target).is_null()).sum().alias('pending')).row(0,named=True)
        if counts['selected']:
            notes.append(f"按场景从 {rule.from_role} 提取 {rule.target}：{counts['selected']} 行，其中 {counts['pending']} 行缺失、格式不符或有冲突，保留待核对")
    return frame
