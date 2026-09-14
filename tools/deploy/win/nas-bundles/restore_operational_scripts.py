from pathlib import Path
import json,hashlib,shutil,os,uuid
base=Path('D:/ledger/releases');report=Path('C:/ledger-storage-migration/20260913')
man=json.loads((report/'nas-bundles.json').read_text(encoding='utf8'))
keep=['D:\\ledger\\releases\\ui-release.lock']
for b in man['bundles']:
 source=Path(b['source'])
 if not b['is_file'] or source.parent!=base or source.suffix.lower() not in {'.py','.mjs','.js','.ps1','.sh','.bat','.cmd','.exe','.dll'}:continue
 if not source.is_symlink():continue
 assert source.resolve()==Path(b['target']).resolve()
 temp=base/(source.name+'.runtime-restore-'+uuid.uuid4().hex)
 shutil.copyfile(source,temp)
 assert hashlib.sha256(temp.read_bytes()).hexdigest()==b['files'][0]['sha256']
 os.replace(temp,source);keep.append(str(source))
(report/'nas-runtime-exclusions.json').write_text(json.dumps({'kept_local':keep,'reason':'Operational scripts retain local path semantics; archived copies remain on NAS'},indent=2),encoding='utf8')
print(json.dumps({'operational_paths_local':len(keep)}))
