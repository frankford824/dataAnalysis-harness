"""Resolve a line's original platform order without borrowing a merged header."""
import polars as pl


def original_parent(raw, header, shop, child, exported=None):
    raw=raw.cast(pl.String).str.strip_chars()
    header=header.cast(pl.String)
    shop=shop.cast(pl.String)
    normalized=header.str.replace_all('[，;；]',',').str.replace_all(r'\s+','')
    member=raw.str.extract(r'^(?:\d+:)?(\d{19})$',1)
    prefix=raw.str.extract(r'^(\d+):\d{19}$',1)
    shop_known=shop.str.contains(r'^\d+$').fill_null(False)
    prefix_ok=prefix.is_null() | (shop_known & (prefix==shop)).fill_null(False)
    allowed_prefix=pl.when(shop_known).then(pl.concat_str([pl.lit('(?:'),shop,pl.lit(':)?')])).otherwise(pl.lit(''))
    pattern=pl.concat_str([pl.lit('(?:^|,)'),allowed_prefix,member,pl.lit('(?:,|$)')])
    member_ok=member.is_not_null() & prefix_ok & (
        header.is_null() | (header=='') | normalized.str.contains(pattern).fill_null(False))
    candidate=pl.when(member_ok).then(member).otherwise(None)
    parent=header
    if exported:
        certified={str(k):str(v) for k,v in exported.items() if v}
        known=child.cast(pl.String).replace_strict(certified,default=None,return_dtype=pl.String)
        aliases=normalized.str.split(',').list.eval(pl.element().str.split(':').list.last())
        parent=pl.when(known.is_not_null() & (header.is_null() | (header=='') | aliases.list.contains(known).fill_null(False))).then(known).otherwise(parent)
        candidate=pl.when(known.is_null() | (known==candidate)).then(candidate).otherwise(None)
    return pl.coalesce(candidate,parent),candidate.is_not_null()
