"""Canonical store ownership from row evidence and unambiguous file hints.

This module never infers a store from an order number, SKU or price.
"""
from dataclasses import dataclass
import polars as pl
from ..model.schema import normalize_header


@dataclass(frozen=True)
class OwnerContext:
    aliases: dict
    names: dict
    hint_proved: bool

    def owner(self,row):
        raw=row.get('store_name')
        value=str(raw).strip() if raw is not None else ''
        if not value and self.hint_proved:
            raw=row.get('__hint_store__')
            value=str(raw).strip() if raw is not None else ''
        return self.aliases.get(value)

    def expression(self,frame,*,names=False):
        declared=(pl.col('store_name').cast(pl.String).str.strip_chars().replace('',None)
                  if 'store_name' in frame.columns else pl.lit(None,dtype=pl.String))
        hinted=(pl.col('__hint_store__').cast(pl.String).str.strip_chars()
                if self.hint_proved else pl.lit(None,dtype=pl.String))
        values=({label:self.names[sid] for label,sid in self.aliases.items()} if names else self.aliases)
        return pl.coalesce(declared,hinted).replace_strict(values,default=None,return_dtype=pl.String)


def owner_context(frame,ref,model,platform='*'):
    stores=[s for s in model.stores if platform=='*' or s.platform==platform]
    candidates={}
    for store in stores:
        for label in (store.id,store.name,*store.aliases):
            candidates.setdefault(str(label).strip(),set()).add(store.id)
    aliases={label:next(iter(ids)) for label,ids in candidates.items() if len(ids)==1}
    names={s.id:s.name for s in stores}
    hay=normalize_header(getattr(ref,'filename',''))
    matches=[(s.id,normalize_header(label)) for s in stores for label in (s.name,*s.aliases)
             if normalize_header(label) and normalize_header(label) in hay]
    # A nested short brand alias is less specific; unrelated matched stores
    # remain a genuine ambiguity even when their names have different lengths.
    maximal=[(sid,label) for sid,label in matches if not any(label!=other and label in other for _,other in matches)]
    file_owners={sid for sid,_ in maximal}
    hints=(frame['__hint_store__'].drop_nulls().cast(pl.String).unique().to_list()
           if '__hint_store__' in frame.columns else [])
    hinted={aliases.get(str(value).strip()) for value in hints if str(value).strip()}
    proved=(len(hinted)==1 and None not in hinted and (not file_owners or file_owners==hinted))
    return OwnerContext(aliases,names,proved)
