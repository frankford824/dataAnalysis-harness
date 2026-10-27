"""Explicit local credential bootstrap/inspection; never contact an API.

Passwords are read from a private file or a terminal. Service tokens are written
only to an owner-only client file; the server JSON contains only their SHA256.
No command updates an existing operator/password. Doctor does not initialize DBs.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
CAPABILITIES = frozenset({'intake','recompute','freight_experiment','advance','approved_action','agent_control'})
SCOPES = frozenset({'business','ops'})
IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,127}')


class ConfigError(Exception):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self,message):
        self.print_usage(sys.stderr)
        self.exit(2,'invalid_cli_arguments; secret values are never echoed\n')


def local_path(value):
    path=Path(value)
    if not path.is_absolute() or '\x00' in str(path):raise ConfigError('explicit_absolute_local_path_required')
    if os.name=='nt' and path.drive.startswith('\\\\'):raise ConfigError('network_share_paths_not_supported')
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ConfigError('symlink_path_not_allowed')
    return path.resolve()


def _windows():
    """Owner-only protected DACL, created before any secret bytes are written."""
    import ctypes
    from ctypes import wintypes as w
    adv=ctypes.WinDLL('advapi32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    adv.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)];adv.OpenProcessToken.restype=w.BOOL
    adv.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD)];adv.GetTokenInformation.restype=w.BOOL
    adv.ConvertSidToStringSidW.argtypes=[w.LPVOID,ctypes.POINTER(w.LPWSTR)];adv.ConvertSidToStringSidW.restype=w.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[w.LPCWSTR,w.DWORD,ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.DWORD)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=w.BOOL
    adv.GetNamedSecurityInfoW.argtypes=[w.LPWSTR,ctypes.c_int,w.DWORD,ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.LPVOID)]
    adv.GetNamedSecurityInfoW.restype=w.DWORD
    adv.GetSecurityDescriptorControl.argtypes=[w.LPVOID,ctypes.POINTER(w.WORD),ctypes.POINTER(w.DWORD)];adv.GetSecurityDescriptorControl.restype=w.BOOL
    adv.GetAclInformation.argtypes=[w.LPVOID,w.LPVOID,w.DWORD,ctypes.c_int];adv.GetAclInformation.restype=w.BOOL
    adv.GetAce.argtypes=[w.LPVOID,w.DWORD,ctypes.POINTER(w.LPVOID)];adv.GetAce.restype=w.BOOL
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    kernel.LocalFree.argtypes=[w.HLOCAL];kernel.LocalFree.restype=w.HLOCAL
    def sid_text(pointer):
        text=w.LPWSTR()
        if not adv.ConvertSidToStringSidW(pointer,ctypes.byref(text)):raise ConfigError('private_acl_unavailable')
        try:return text.value
        finally:kernel.LocalFree(ctypes.cast(text,w.HLOCAL))
    token=w.HANDLE();needed=w.DWORD()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):raise ConfigError('private_acl_unavailable')
    try:
        adv.GetTokenInformation(token,1,None,0,ctypes.byref(needed));buffer=ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token,1,buffer,needed.value,ctypes.byref(needed)):raise ConfigError('private_acl_unavailable')
        sid=sid_text(ctypes.cast(buffer,ctypes.POINTER(w.LPVOID)).contents)
    finally:kernel.CloseHandle(token)
    return ctypes,w,adv,kernel,sid,sid_text


def private(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink!=1:raise ConfigError('private_regular_file_required')
    if os.name!='nt':
        if info.st_mode & 0o177 or info.st_uid!=os.geteuid():raise ConfigError('owner_only_file_permissions_required')
        return
    c,w,adv,kernel,sid,sid_text=_windows();owner=w.LPVOID();acl=w.LPVOID();sd=w.LPVOID()
    if adv.GetNamedSecurityInfoW(str(path),1,5,c.byref(owner),None,c.byref(acl),None,c.byref(sd)):
        raise ConfigError('private_acl_unavailable')
    try:
        control=w.WORD();revision=w.DWORD()
        if not adv.GetSecurityDescriptorControl(sd,c.byref(control),c.byref(revision)) or not control.value&0x1000 or sid_text(owner)!=sid:
            raise ConfigError('owner_only_file_permissions_required')
        class Sizes(c.Structure):_fields_=[('count',w.DWORD),('used',w.DWORD),('free',w.DWORD)]
        sizes=Sizes();ace=w.LPVOID()
        if not acl or not adv.GetAclInformation(acl,c.byref(sizes),c.sizeof(sizes),2) or sizes.count!=1 or not adv.GetAce(acl,0,c.byref(ace)):
            raise ConfigError('owner_only_file_permissions_required')
        # ACCESS_ALLOWED_ACE = four-byte header, DWORD access mask, SID.
        if c.cast(ace,c.POINTER(c.c_ubyte)).contents.value!=0 or sid_text(w.LPVOID(ace.value+8))!=sid:
            raise ConfigError('owner_only_file_permissions_required')
    finally:kernel.LocalFree(sd)


def create_private(path):
    if os.name!='nt':
        return os.fdopen(os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb')
    import msvcrt
    c,w,adv,kernel,sid,_sid_text=_windows();sd=w.LPVOID()
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW('O:'+sid+'D:P(A;;FA;;;'+sid+')',1,c.byref(sd),None):
        raise ConfigError('private_acl_unavailable')
    class Attributes(c.Structure):_fields_=[('length',w.DWORD),('descriptor',w.LPVOID),('inherit',w.BOOL)]
    attributes=Attributes(c.sizeof(Attributes),sd,False)
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.POINTER(Attributes),w.DWORD,w.DWORD,w.HANDLE]
    kernel.CreateFileW.restype=w.HANDLE
    try:
        handle=kernel.CreateFileW(str(path),0x40000000,0,c.byref(attributes),1,0x80,None)
        if handle==w.HANDLE(-1).value:
            if c.get_last_error() in {80,183}:raise FileExistsError(path)
            raise ConfigError('private_file_create_failed')
        return os.fdopen(msvcrt.open_osfhandle(handle,os.O_WRONLY),'wb')
    finally:kernel.LocalFree(sd)


def secure_new_path(path,*,directory=False):
    """Only call for a path this command just created, never existing accounts."""
    if os.name!='nt':
        path.chmod(0o700 if directory else 0o600)
        return
    c,w,adv,kernel,sid,_sid_text=_windows();sd=w.LPVOID()
    inherit='OICI' if directory else ''
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW('O:'+sid+'D:P(A;'+inherit+';FA;;;'+sid+')',1,c.byref(sd),None):
        raise ConfigError('private_acl_unavailable')
    adv.SetFileSecurityW.argtypes=[w.LPCWSTR,w.DWORD,w.LPVOID];adv.SetFileSecurityW.restype=w.BOOL
    try:
        if not adv.SetFileSecurityW(str(path),0x80000005,sd):raise ConfigError('private_acl_unavailable')
    finally:kernel.LocalFree(sd)


def stage(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with create_private(temporary) as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        private(temporary)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def atomic(path,raw):
    temporary=stage(path,raw)
    try:os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


@contextmanager
def locked(path,timeout=10):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        with create_private(path) as new:new.write(b'0')
    except FileExistsError:pass
    private(path)
    with path.open('r+b') as stream:
        acquired=False;end=time.monotonic()+timeout
        while not acquired:
            try:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
            except OSError:
                if time.monotonic()>=end:raise ConfigError('credential_operation_locked') from None
                time.sleep(.05)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def secret_file(path):
    private(path)
    if path.stat().st_size>4096:raise ConfigError('secret_file_too_large')
    value=path.read_text(encoding='utf-8').rstrip('\r\n')
    if not value or any(c in value for c in '\r\n\x00'):raise ConfigError('secret_file_invalid')
    return value


def selected(value,allowed,label):
    items=value.split(',')
    if not items or any(not v or v!=v.strip() for v in items) or len(set(items))!=len(items) or set(items)-allowed:
        raise ConfigError('unknown_or_duplicate_'+label)
    return sorted(items)


def identities(path):
    private(path)
    if path.stat().st_size>1024*1024:raise ConfigError('identity_file_too_large')
    try:body=json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError):raise ConfigError('identity_file_invalid') from None
    if not isinstance(body,dict) or not isinstance(body.get('identities'),list):raise ConfigError('identity_file_invalid')
    seen=set()
    for row in body['identities']:
        if (not isinstance(row,dict) or not IDENTIFIER.fullmatch(str(row.get('id',''))) or row['id'] in seen
            or not isinstance(row.get('enabled'),bool) or not re.fullmatch('[0-9a-f]{64}',str(row.get('token_sha256','')))
            or isinstance(row.get('expires_at'),bool) or not isinstance(row.get('expires_at'),(int,float))
            or not math.isfinite(row['expires_at']) or not 0<row['expires_at']<253402300799):raise ConfigError('identity_entry_invalid')
        seen.add(row['id'])
        if any(k!='token_sha256' and any(s in k.casefold() for s in ('token','password','secret','cookie')) for k in row):
            raise ConfigError('plaintext_secret_fields_not_allowed')
        for key,allowed in (('capabilities',CAPABILITIES),('scopes',SCOPES)):
            values=row.get(key)
            if not isinstance(values,list) or not values or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values) or set(values)-allowed:
                raise ConfigError('unknown_or_duplicate_'+key)
    return body


def issue(args):
    identity,token_path=local_path(args.identity_file),local_path(args.token_file)
    if identity==token_path:raise ConfigError('identity_and_client_token_paths_must_differ')
    if not IDENTIFIER.fullmatch(args.id):raise ConfigError('service_id_invalid')
    caps=selected(args.capabilities,CAPABILITIES,'capabilities');scopes=selected(args.scopes,SCOPES,'scopes')
    stamp=time.time();expires=stamp+args.expires_in
    if args.expires_in<=0 or not 0<expires<253402300799:raise ConfigError('expiry_invalid')
    with ExitStack() as locks:
        for path in sorted((identity,token_path),key=lambda p:str(p).casefold()):
            locks.enter_context(locked(path.with_name(path.name+'.lock')))
        body=identities(identity) if identity.exists() else {'schema':1,'identities':[]}
        old=next((r for r in body['identities'] if r['id']==args.id),None)
        if old and not args.rotate:raise ConfigError('service_id_exists_use_explicit_rotate')
        if args.rotate and old is None:raise ConfigError('service_id_not_found_for_rotation')
        previous_token=None
        if token_path.exists():
            if not args.rotate:raise ConfigError('client_token_file_exists')
            previous_token=secret_file(token_path)
            if hashlib.sha256(previous_token.encode()).hexdigest()!=old['token_sha256']:
                raise ConfigError('client_token_does_not_match_rotated_identity')
        token=secrets.token_urlsafe(48)
        entry={'id':args.id,'name':args.id,'enabled':True,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
               'capabilities':caps,'scopes':scopes,'issued_at':stamp,'expires_at':expires}
        body['identities']=[entry if r['id']==args.id else r for r in body['identities']]
        if old is None:body['identities'].append(entry)
        token_stage=stage(token_path,(token+'\n').encode())
        try:identity_stage=stage(identity,json.dumps(body,ensure_ascii=False,sort_keys=True,indent=2).encode())
        except BaseException:
            token_stage.unlink(missing_ok=True)
            raise
        published=False
        try:
            # Each publication is atomic. A process crash between publications
            # leaves an unrecognized client token, never another accepted token.
            os.replace(token_stage,token_path);published=True
            os.replace(identity_stage,identity)
        except BaseException:
            if published:
                if previous_token is None:token_path.unlink(missing_ok=True)
                else:atomic(token_path,(previous_token+'\n').encode())
            raise
        finally:
            token_stage.unlink(missing_ok=True);identity_stage.unlink(missing_ok=True)
    return {'ok':True,'action':'service.rotate' if args.rotate else 'service.issue','id':args.id,
            'identity_file':str(identity),'token_file':str(token_path),'capabilities':caps,'scopes':scopes,
            'expires_at':datetime.fromtimestamp(expires,timezone.utc).isoformat(),
            'server_environment':'LEDGER_SERVICE_IDENTITIES='+str(identity)}


def operator(args):
    home=local_path(args.home)
    database=local_path(home/'commission/registry.db')
    if not args.name or len(args.name)>128 or args.name!=args.name.strip() or any(ord(c)<32 for c in args.name):
        raise ConfigError('operator_name_invalid')
    if args.password_file:password=secret_file(local_path(args.password_file))
    else:
        if not sys.stdin.isatty():raise ConfigError('password_file_required_without_terminal')
        password=getpass.getpass('New ledger operator password: ')
        if password!=getpass.getpass('Repeat password: '):raise ConfigError('passwords_do_not_match')
    if len(password)<12:raise ConfigError('password_requires_at_least_12_characters')
    new_db=not database.exists()
    for directory in (home,database.parent):
        if not directory.exists():
            directory.mkdir(parents=True,mode=0o700)
            secure_new_path(directory,directory=True)
    sys.path.insert(0,str(REPO/'ledger'))
    from ledger.commission_registry import Registry
    registry=Registry(home)
    if new_db:
        for path in (database,database.with_name(database.name+'-wal'),database.with_name(database.name+'-shm')):
            if path.is_file():secure_new_path(path)
    registry.operator_add(args.name,password,admin=args.admin,by='system:ledger-auth-local-cli')
    return {'ok':True,'action':'operator.add','home':str(home),'name':args.name,'admin':args.admin}


def _source_digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def read_operators(database):
    """Read a stable private DB/WAL/journal copy, never create source sidecars.

    Immutable SQLite would ignore a live committed WAL. Opening mode=ro can
    create -wal/-shm on the original. A byte-verified copy permits SQLite recovery
    only in a private temporary directory; concurrent source change is unknown.
    """
    paths=[database,database.with_name(database.name+'-wal'),database.with_name(database.name+'-journal')]
    before={p.name for p in paths if p.is_file()}
    temporary=tempfile.TemporaryDirectory(prefix='ledger-auth-doctor-')
    folder=Path(temporary.name).resolve();base=Path(tempfile.gettempdir()).resolve()
    if folder.parent!=base:raise ConfigError('doctor_snapshot_path_invalid')
    try:
        secure_new_path(folder,directory=True)
        captured={}
        for source in paths:
            if source.name not in before:continue
            local_path(source)
            digest=hashlib.sha256()
            with source.open('rb') as reader,create_private(folder/source.name) as writer:
                for block in iter(lambda:reader.read(1024*1024),b''):
                    digest.update(block);writer.write(block)
            captured[source.name]=digest.hexdigest()
        if {p.name for p in paths if p.is_file()}!=before or any(_source_digest(p)!=captured[p.name] for p in paths if p.name in before):
            raise ConfigError('registry_snapshot_changed_retry')
        # This connection may recover its COPY or create COPY sidecars only.
        db=sqlite3.connect(folder/database.name)
        try:
            rows=[{'id':r[0],'name':r[1],'admin':bool(r[2]),'enabled':not bool(r[3])}
                  for r in db.execute('SELECT id,name,admin,disabled FROM operator ORDER BY id')]
        finally:db.close()
        if {p.name for p in paths if p.is_file()}!=before or any(_source_digest(p)!=captured[p.name] for p in paths if p.name in before):
            raise ConfigError('registry_snapshot_changed_retry')
        return rows
    finally:
        if folder.parent!=base or folder.is_symlink():raise ConfigError('doctor_snapshot_path_invalid')
        temporary.cleanup()


def doctor(args):
    if not args.home and not args.identity_file:raise ConfigError('doctor_requires_explicit_home_or_identity_file')
    checks=[]
    if args.home:
        home=local_path(args.home);path=local_path(home/'commission/registry.db')
        if not path.is_file():checks.append({'kind':'operators','ok':False,'reason':'registry_database_missing'})
        else:
            try:
                rows=read_operators(path)
                checks.append({'kind':'operators','ok':any(r['enabled'] for r in rows),'operators':rows,
                               'enabled_count':sum(r['enabled'] for r in rows)})
            except (sqlite3.Error,OSError,ConfigError) as exc:
                checks.append({'kind':'operators','ok':False,'reason':str(exc) if isinstance(exc,ConfigError) else 'registry_database_unreadable'})
    if args.identity_file:
        path=local_path(args.identity_file)
        try:
            body=identities(path);stamp=time.time()
            rows=[{'id':r['id'],'enabled':r['enabled'],'valid':r['enabled'] and r['expires_at']>stamp,
                   'expires_at':datetime.fromtimestamp(r['expires_at'],timezone.utc).isoformat(),
                   'capabilities':r['capabilities'],'scopes':r['scopes']} for r in body['identities']]
            expired=[r['id'] for r in rows if r['enabled'] and not r['valid']]
            checks.append({'kind':'services','ok':any(r['valid'] for r in rows) and not expired,'identities':rows,
                           'valid_count':sum(r['valid'] for r in rows),'expired_enabled_ids':expired})
        except (ConfigError,OSError) as exc:
            checks.append({'kind':'services','ok':False,'reason':str(exc) if isinstance(exc,ConfigError) else 'identity_file_missing_or_unreadable'})
    return {'ok':all(c['ok'] for c in checks),'action':'doctor','checks':checks}


def main(argv=None):
    parser=SafeParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    add=sub.add_parser('operator-add');add.add_argument('--home',required=True);add.add_argument('--name',required=True)
    add.add_argument('--password-file');add.add_argument('--admin',action='store_true')
    service=sub.add_parser('service-issue');service.add_argument('--identity-file',required=True);service.add_argument('--token-file',required=True)
    service.add_argument('--id',required=True);service.add_argument('--capabilities',required=True);service.add_argument('--scopes',required=True)
    service.add_argument('--expires-in',required=True,type=int);service.add_argument('--rotate',action='store_true')
    check=sub.add_parser('doctor');check.add_argument('--home');check.add_argument('--identity-file')
    args=parser.parse_args(argv)
    try:result={'operator-add':operator,'service-issue':issue,'doctor':doctor}[args.command](args)
    except Exception as exc:
        result={'ok':False,'action':args.command,'error_code':str(exc) if isinstance(exc,ConfigError) else 'local_configuration_operation_failed',
                'error_type':type(exc).__name__}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
