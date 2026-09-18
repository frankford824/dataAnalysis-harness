"""Small transactional dependency clocks for read caches."""


def install(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS read_generation(scope TEXT PRIMARY KEY,generation INTEGER NOT NULL)')
    def bump(scope):
        return f"INSERT INTO read_generation VALUES({scope},1) ON CONFLICT(scope) DO UPDATE SET generation=generation+1;"
    for table in ('run','period','slot','version','manual_finance','cost_line_log'):
        for event in ('insert','update','delete'):
            row='OLD' if event=='delete' else 'NEW'
            body=bump(f"'s:'||{row}.store_id")
            if table in ('run','period','manual_finance','cost_line_log'):
                body+=bump(f"'p:'||{row}.period")
            conn.executescript(f'CREATE TRIGGER IF NOT EXISTS read_{table}_{event} AFTER {event} ON {table} BEGIN {body} END;')
    conn.executescript(f"CREATE TRIGGER IF NOT EXISTS read_config_insert AFTER INSERT ON config_log BEGIN {bump(chr(39)+'global'+chr(39))} END;")


def clock(conn, store_id='', period='', start='', end=''):
    scopes=['global','s:__shared__']
    if store_id:
        scopes.append('s:'+store_id)
    if period:
        scopes.append('p:'+period)
    sql='SELECT scope,generation FROM read_generation WHERE scope IN ('+','.join('?' for _ in scopes)+')'
    args=list(scopes)
    if start and end:
        sql+=' OR scope BETWEEN ? AND ?';args.extend(['p:'+start,'p:'+end])
    return tuple(tuple(row) for row in conn.execute(sql+' ORDER BY scope',args))
