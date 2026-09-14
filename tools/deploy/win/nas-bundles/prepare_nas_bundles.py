from pathlib import Path
import json,time,shutil,hashlib,os
root=Path('D:/ledger');archive=Path(json.loads((root/'home/storage-policy.json').read_text())['archive_root'])/'bundles'/'ledger'
report=Path('C:/ledger-storage-migration/20260913/nas-bundles.json')
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(8*1024**2):h.update(b)
 return h.hexdigest()
choices=[root/'backups',root/'work-probe',root/'commission-deploy',root/'workspace-migration.db']
choices+=list((root/'qa').iterdir())
choices+=list((root/'releases').iterdir())
completed=[];skipped=[];cutoff=time.time()-86400
for source in choices:
 if source.is_symlink() or not source.exists() or source.name.endswith('.lock'):continue
 files=[source] if source.is_file() else [p for p in source.rglob('*') if p.is_file()]
 if not files or source.stat().st_mtime>cutoff or any(p.stat().st_mtime>cutoff for p in files):
  skipped.append(str(source));continue
 target=archive/source.relative_to(root);proof=[]
 for p in files:
  dest=target if source.is_file() else target/p.relative_to(source)
  dest.parent.mkdir(parents=True,exist_ok=True)
  before=p.stat();digest=sha(p)
  if not dest.exists() or sha(dest)!=digest:
   shutil.copyfile(p,dest)
  if sha(dest)!=digest:raise RuntimeError('Destination checksum mismatch: '+str(p))
  after=p.stat()
  if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise RuntimeError('Source changed: '+str(p))
  proof.append({'relative':'.' if source.is_file() else str(p.relative_to(source)),'bytes':before.st_size,'mtime_ns':before.st_mtime_ns,'sha256':digest})
 # Save only fully verified units, permitting safe resumption.
 completed.append({'source':str(source),'target':str(target),'is_file':source.is_file(),'files':proof})
 report.write_text(json.dumps({'bundles':completed,'skipped':skipped},ensure_ascii=False,indent=2),encoding='utf8')
 print(json.dumps({'verified_bundle':str(source),'files':len(proof),'bytes':sum(x['bytes'] for x in proof)}),flush=True)
report.write_text(json.dumps({'bundles':completed,'skipped':skipped},ensure_ascii=False,indent=2),encoding='utf8')
print('BUNDLES_VERIFIED',flush=True)

