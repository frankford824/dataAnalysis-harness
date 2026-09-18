"""Transactional, rebuildable indexes of active scheme segments and people."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheme_read_meta(id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS catalog_read_meta(id INTEGER PRIMARY KEY, generation INTEGER NOT NULL);
INSERT OR IGNORE INTO catalog_read_meta VALUES(1,0);
CREATE TABLE IF NOT EXISTS scheme_read_segment(
 scheme_id TEXT NOT NULL, segment_no INTEGER NOT NULL, store_id TEXT NOT NULL,
 product_id TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL,
 mode TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(scheme_id,segment_no));
CREATE INDEX IF NOT EXISTS scheme_read_from ON scheme_read_segment(valid_from);
CREATE INDEX IF NOT EXISTS scheme_read_to ON scheme_read_segment(valid_to);
CREATE INDEX IF NOT EXISTS scheme_read_lookup ON scheme_read_segment(scheme_id,segment_no,valid_from,valid_to,mode,store_id,product_id);
CREATE INDEX IF NOT EXISTS scheme_read_mode ON scheme_read_segment(mode,valid_from,valid_to,store_id,scheme_id);
CREATE TABLE IF NOT EXISTS scheme_read_person(
 person_id TEXT NOT NULL, scheme_id TEXT NOT NULL, segment_no INTEGER NOT NULL,
 duty TEXT NOT NULL, PRIMARY KEY(person_id,scheme_id,segment_no,duty));
CREATE INDEX IF NOT EXISTS scheme_read_person_scheme ON scheme_read_person(scheme_id,segment_no);
CREATE INDEX IF NOT EXISTS scheme_product_id ON scheme(product_id);
CREATE INDEX IF NOT EXISTS catalog_search_names ON catalog(store_id,product_id,product_name);
CREATE INDEX IF NOT EXISTS scheme_search_names ON scheme(store_id,product_id,product_name);
CREATE INDEX IF NOT EXISTS catalog_primary_refresh ON catalog(refreshed_at)
 WHERE payload NOT LIKE '%"origin":"order_observation"%';
"""


def _segments(where):
    return f"""INSERT INTO scheme_read_segment
      SELECT s.id,cast(j.key as integer),s.store_id,s.product_id,
      json_extract(j.value,'$.valid_from'),coalesce(json_extract(j.value,'$.valid_to'),''),
      coalesce(json_extract(j.value,'$.mode'),'hold'),j.value
      FROM scheme s JOIN scheme_version v ON v.id=s.active_version
      JOIN json_each(v.body,'$.segments') j WHERE {where};"""


def _people(where):
    return f"""INSERT OR IGNORE INTO scheme_read_person
      SELECT json_extract(a.value,'$.person_id'),s.scheme_id,s.segment_no,coalesce(json_extract(a.value,'$.duty'),'')
      FROM scheme_read_segment s JOIN json_each(s.value,'$.allocations') a
      WHERE {where} AND json_extract(a.value,'$.person_id') IS NOT NULL;"""


def install(conn):
    conn.executescript(SCHEMA)
    for event in ('insert','update','delete'):
        conn.executescript(f'''CREATE TRIGGER IF NOT EXISTS catalog_read_{event} AFTER {event} ON catalog BEGIN
          UPDATE catalog_read_meta SET generation=generation+1 WHERE id=1; END;''')
    for action in ('INSERT', 'UPDATE OF active_version'):
        suffix = 'insert' if action == 'INSERT' else 'update'
        conn.executescript(f"""CREATE TRIGGER IF NOT EXISTS scheme_read_{suffix}
          AFTER {action} ON scheme BEGIN
          DELETE FROM scheme_read_person WHERE scheme_id=NEW.id;
          DELETE FROM scheme_read_segment WHERE scheme_id=NEW.id;
          {_segments('s.id=NEW.id')}
          {_people('s.scheme_id=NEW.id')}
          END;""")
    conn.executescript("""CREATE TRIGGER IF NOT EXISTS scheme_read_delete AFTER DELETE ON scheme BEGIN
       DELETE FROM scheme_read_person WHERE scheme_id=OLD.id;
       DELETE FROM scheme_read_segment WHERE scheme_id=OLD.id;
       END;""")
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM scheme_read_meta WHERE id=1 AND version=1').fetchone():
            conn.execute('DELETE FROM scheme_read_person')
            conn.execute('DELETE FROM scheme_read_segment')
            conn.execute(_segments('1'))
            conn.execute(_people('1'))
            conn.execute('INSERT OR REPLACE INTO scheme_read_meta VALUES(1,1)')


def person_keys(persons, moment):
    """Match the current segment, otherwise the earliest future segment."""
    sql = f"""SELECT DISTINCT s.store_id,s.product_id FROM scheme_read_person a
      JOIN scheme_read_segment s ON s.scheme_id=a.scheme_id AND s.segment_no=a.segment_no
      WHERE a.person_id IN ({','.join('?' for _ in persons)}) AND (
        (s.valid_from<=? AND (s.valid_to='' OR s.valid_to>?)) OR
        (s.valid_from>? AND NOT EXISTS(SELECT 1 FROM scheme_read_segment x
          WHERE x.scheme_id=s.scheme_id AND x.valid_from<=? AND (x.valid_to='' OR x.valid_to>?))
          AND s.valid_from=(SELECT min(x.valid_from) FROM scheme_read_segment x
                           WHERE x.scheme_id=s.scheme_id AND x.valid_from>?)))"""
    return sql, [*persons, *([moment] * 6)]
