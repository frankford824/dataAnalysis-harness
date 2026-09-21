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
    # Commission reports use visible runs, not newer background candidates for
    # frozen periods. Keep the broader clocks above for stale-data indicators.
    for table in ('run','period','manual_finance','run_labor','cost_line_log'):
        for event in ('insert','update','delete'):
            row = 'OLD' if event == 'delete' else 'NEW'
            guard = ''
            if table == 'run' and event != 'delete':
                guard = f"WHEN NOT EXISTS(SELECT 1 FROM period p WHERE p.store_id={row}.store_id AND p.period={row}.period AND p.state='closed' AND p.run_id IS NOT NULL AND p.run_id<>{row}.id)"
            body = bump(f"'rp:'||{row}.period")
            conn.executescript(f'CREATE TRIGGER IF NOT EXISTS report_{table}_{event} AFTER {event} ON {table} {guard} BEGIN {body} END;')


def clock(conn, store_id='', period='', start='', end='', *, report=False):
    scopes=['global','s:__shared__']
    if store_id:
        scopes.append('s:'+store_id)
    if period:
        scopes.append('p:'+period)
    sql='SELECT scope,generation FROM read_generation WHERE scope IN ('+','.join('?' for _ in scopes)+')'
    args=list(scopes)
    if start and end:
        prefix='rp:' if report else 'p:'
        sql+=' OR scope BETWEEN ? AND ?';args.extend([prefix+start,prefix+end])
    return tuple(tuple(row) for row in conn.execute(sql+' ORDER BY scope',args))
