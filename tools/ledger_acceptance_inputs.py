"""Hash every approved service bundle input, including CSV/CAS/feed objects.

Inventory is evidence binding only. It never approves expected money or widens
the financial scope of a historical case.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path,PurePosixPath
import re


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def relative_file(root,relative):
    if (not isinstance(relative,str) or '\\' in relative or not relative
            or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts
            or PurePosixPath(relative).as_posix()!=relative):raise ValueError('Input path must be canonical and relative')
    path=root/relative
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError('Approved input is missing or outside its root')
    return path


def inventory(roots):
    rows=[]
    if not isinstance(roots,dict) or not roots:raise ValueError('Approved input roots are missing')
    for key,spec in sorted(roots.items()):
        if not re.fullmatch('[a-z][a-z0-9_-]{0,63}',key):raise ValueError('Invalid input root identity')
        root=Path(spec['path']).resolve()
        if not root.is_dir():raise ValueError('Approved input root is missing')
        kind=spec.get('kind')
        if kind=='corpus_xlsx':
            expected={p.relative_to(root).as_posix():None for p in root.rglob('*.xlsx') if p.is_file()}
        elif kind=='explicit_files':expected=spec['files']
        elif kind=='frozen_bundle':
            seal=relative_file(root,'bundle.json')
            if sha(seal)!=spec['manifest_sha256']:raise ValueError('Approved bundle manifest changed')
            body=read(seal);records=body.get('files')
            if body.get('schema_version')!=1 or not isinstance(records,list) or not records:
                raise ValueError('Approved bundle inventory is missing')
            expected={row['path']:row['sha256'] for row in records}
            if len(expected)!=len(records) or 'bundle.json' in expected:raise ValueError('Duplicated or recursive bundle inventory')
            expected['bundle.json']=spec['manifest_sha256']
            actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
            if actual!=set(expected):raise ValueError('Approved bundle file scope changed')
        else:raise ValueError('Unsupported approved input root policy')
        if not isinstance(expected,dict) or not expected:raise ValueError('Empty input root cannot certify a release')
        for relative,wanted in sorted(expected.items()):
            path=relative_file(root,relative);actual=sha(path)
            if wanted is not None and actual!=wanted:raise ValueError('Approved input bytes changed')
            rows.append({'path':key+'/'+relative,'root_id':key,'relative_path':relative,'sha256':actual})
    return rows


def capture(corpus,service_cases=None,attestation=None):
    roots={'corpus':{'path':str(Path(corpus).resolve()),'kind':'corpus_xlsx'}}
    if attestation is not None:
        if service_cases is not None:raise ValueError('Use either approved service cases or a baseline attestation')
        path=Path(attestation).resolve()
        if not path.is_file():raise ValueError('Baseline attestation is missing')
        evidence=read(path).get('evidence') or {}
        report=relative_file(path.parent,evidence.get('path'))
        if sha(report)!=evidence.get('sha256'):raise ValueError('Attestation evidence changed')
        roots['attestation']={'path':str(path.parent),'kind':'explicit_files',
            'files':{path.name:sha(path),evidence['path']:evidence['sha256']}}
    if service_cases is not None:
        manifest=Path(service_cases).resolve()
        if not manifest.is_file():raise ValueError('Approved service manifest is missing')
        control=manifest.parent;files={manifest.name:sha(manifest)}
        def reference(spec):
            path=relative_file(control,spec['path'])
            if sha(path)!=spec['sha256']:raise ValueError('Approved case/control reference changed')
            files[spec['path']]=spec['sha256'];return path
        body=read(manifest)
        if body.get('workspace_decisions'):reference(body['workspace_decisions'])
        for ref in body.get('cases',[]):
            case_path=reference(ref);case=read(case_path)
            bundle=Path(case['bundle'])
            if not bundle.is_absolute():bundle=case_path.parent/bundle
            bundle=bundle.resolve()
            key='case_'+hashlib.sha256(str(bundle).encode()).hexdigest()[:16]
            roots[key]={'path':str(bundle),'kind':'frozen_bundle','manifest_sha256':sha(bundle/'bundle.json')}
            decisions=case.get('workspace_decisions')
            if decisions:
                path=Path(decisions['path'])
                if not path.is_absolute():path=case_path.parent/path
                path=path.resolve()
                if not path.is_relative_to(control):raise ValueError('Case decision control is outside the approved manifest directory')
                reference({**decisions,'path':path.relative_to(control).as_posix()})
        roots['controls']={'path':str(control),'kind':'explicit_files','files':files}
    return roots,inventory(roots)


def verify(receipt):
    if receipt.get('input_roots') is not None:
        actual=inventory(receipt['input_roots'])
        if actual!=receipt.get('inputs'):raise ValueError('Accepted full input inventory changed')
        return actual
    # Compatibility for independently tested legacy receipt contracts. The new
    # runner always emits the multi-root inventory; missing CSVs cannot hide in
    # a new historical service bundle.
    root=Path(receipt.get('corpus_root','')).resolve();expected=receipt.get('inputs') or []
    if not expected:raise ValueError('Accepted source inputs are missing')
    seen=set()
    for row in expected:
        path=relative_file(root,row['path'])
        if path in seen or sha(path)!=row['sha256']:raise ValueError('Accepted source input changed or was duplicated')
        seen.add(path)
    actual={p.resolve() for p in root.rglob('*.xlsx') if p.is_file()}
    if actual!=seen:raise ValueError('Accepted legacy corpus inventory changed')
    return expected
