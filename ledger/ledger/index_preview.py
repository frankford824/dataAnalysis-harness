"""Read one immutable indexed file without searching the company-wide index."""
from __future__ import annotations

import json
import logging
from contextlib import closing
from pathlib import Path
import re
import sqlite3

import polars as pl


def local(catalog: Path, sha: str, *, sheet: str = '', offset: int = 0, limit: int = 30):
    """Return the existing preview contract, or None to use the index service.

    Never parse NAS files, initialize a catalog, or modify a financial record.
    The file must be the indexed SHA's Parquet inside this catalog's directory.
    """
    if not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise ValueError('文件 SHA 不合法')
    catalog = Path(catalog).resolve()
    if not catalog.is_file():
        return None
    try:
        with closing(sqlite3.connect(catalog.as_uri() + '?mode=ro', uri=True, timeout=1)) as conn:
            conn.row_factory = sqlite3.Row
            record = conn.execute('''SELECT path,platform,store_id,source,authority,rows,parquet_path
                FROM file_catalog WHERE sha256=? AND state='ready'
                ORDER BY CASE authority WHEN 'calculation' THEN 0 ELSE 1 END,path LIMIT 1''', (sha,)).fetchone()
        if record is None:
            return None
        directory = (catalog.parent / 'parquet').resolve()
        expected = (directory / f'{sha}.parquet').resolve()
        if not expected.is_relative_to(directory) or not expected.is_file():
            return None
        if not record['parquet_path'] or Path(record['parquet_path']).resolve() != expected:
            return None
        offset, limit = max(0, min(offset, 1_000_000)), max(1, min(limit, 200))
        source = pl.scan_parquet(expected).filter(pl.col('file_sha') == sha)
        if sheet:
            source = source.filter(pl.col('sheet') == sheet)
        preview_rows = source.select(pl.len()).collect().item()
        frame = source.sort('row_no').slice(offset, limit).select('sheet', 'row_no', 'cells_json').collect()
        rows = [{'sheet': row['sheet'], 'row_no': row['row_no'], 'cells': json.loads(row['cells_json'])}
                for row in frame.iter_rows(named=True)]
        if any(not isinstance(row['cells'], list) or any(not isinstance(v, str) for v in row['cells']) for row in rows):
            return None
        return {'sha256': sha, 'offset': offset, 'limit': limit, 'rows': rows,
                'metadata': {key: record[key] for key in ('path','platform','store_id','source','authority')}
                            | {'total_rows': record['rows'], 'preview_rows': preview_rows}, 'backend': 'local_parquet'}
    except (OSError, sqlite3.Error, pl.exceptions.PolarsError, ValueError):
        logging.getLogger(__name__).warning('Local indexed preview unavailable sha=%s', sha, exc_info=True)
        return None
