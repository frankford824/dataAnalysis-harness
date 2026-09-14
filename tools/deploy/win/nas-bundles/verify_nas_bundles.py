from pathlib import Path
import json,hashlib
report=Path('C:/ledger-storage-migration/20260913/nas-bundles.json')
data=json.loads(report.read_text(encoding='utf8'))
for bundle in data['bundles']:
 source=Path(bundle['source'])
 if source.is_symlink():
  assert source.resolve()==Path(bundle['target']).resolve()
  stage=Path(str(source)+'.local-migration')
  if not stage.exists():continue
  source=stage
 actual={'.'} if source.is_file() else {str(p.relative_to(source)) for p in source.rglob('*') if p.is_file()}
 assert actual=={x['relative'] for x in bundle['files']},str(source)
 for row in bundle['files']:
  p=source if row['relative']=='.' else source/row['relative']
  s=p.stat();assert s.st_size==row['bytes'] and s.st_mtime_ns==row['mtime_ns'],str(p)
print('SOURCE_BUNDLES_UNCHANGED')

