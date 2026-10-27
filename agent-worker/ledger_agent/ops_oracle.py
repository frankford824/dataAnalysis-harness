"""Parent-owned pure-function oracle. Candidate output never creates its receipt."""
from __future__ import annotations
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import selectors
import shutil
import signal
import string
import subprocess
import time
import uuid

from .ops_runtime import digest,file_sha

BANNED={'open','eval','exec','compile','__import__','globals','locals','vars','dir','type','object',
    'getattr','setattr','delattr','input','print','help','breakpoint','exit','quit','os','sys','subprocess','socket','pathlib'}
PURE_ATTRIBUTES={'strip','lstrip','rstrip','casefold','lower','upper','split','join','startswith','endswith','replace',
    'append','extend','pop','reverse','sort','index','count','fromkeys','get','items','keys','values','setdefault','update','copy',
    'add','intersection','union','difference','discard','ceil','floor','Counter','deque','defaultdict',
    'chain','from_iterable','reduce','mean','isleap'}
RUNNER="import importlib.util,json,resource\nresource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))\nresource.setrlimit(resource.RLIMIT_CPU,(3,3))\nspec=importlib.util.spec_from_file_location('candidate','/candidate.py')\nmodule=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\ninputs=json.load(open('/inputs.json'));results=[]\nfor args in inputs:\n    try:results.append({'kind':'return','value':module.solve(*args)})\n    except Exception as error:results.append({'kind':'raise','type':type(error).__name__})\nprint(json.dumps(results,ensure_ascii=False,allow_nan=False,separators=(',',':')))\n"


def _normal(value):
    return json.loads(json.dumps(value,ensure_ascii=False,allow_nan=False))


def cases(name):
    common={
      'inclusive_total':([[n] for n in (0,1,2,7,31,83)],lambda n:sum(range(n+1))),
      'clamp':([[n,0,5] for n in (-8,0,3,5,9)],lambda n,low,high:max(low,min(high,n))),
      'stable_unique':([[['z','a','z','b','a']],[[3,1,3,2]],[[]]],lambda values:list(dict.fromkeys(values))),
      'mean_empty':([[[]],[[2,4,9]],[[-2,2]],[[1,2,4,9]]],lambda values:sum(values)/len(values) if values else None),
      'chunk_tail':([[[1,2,3,4,5],2],[[],2],[[1],0],[[1,2,3],8]],_chunks),
      'normalize_key':([[' A_B '],['Straße'],[''],['  MIXED  ']],lambda text:text.strip().casefold()),
      'count_groups':([[['a','b','a']],[[]],[[1,2,1,3,3,3]]],lambda values:dict(Counter(values))),
      'parse_boolean':([[' FALSE '],['yes'],['0'],['maybe'],['TRUE']],_boolean),
      'flatten_once':([ [ [[1,2],[],[3]] ], [ [] ], [ [[],[4],[5,6]] ] ],lambda values:[item for group in values for item in group]),
      'adjacent_pairs':([[[1,2,3]],[[1]],[[]],[[9,4,8,2]]],lambda values:list(zip(values,values[1:]))),
      'binary_index':([[[1,4,7,9],7],[[1],1],[[],1],[[1,4],3],[[2,4,8,16],16]],_index),
      'balanced_parentheses':([['(a())'],[')('],['(()'],[''],['ab(c)d']],_balanced),
      'leap_year':([[year] for year in (2000,1900,2024,2023,2400,2100)],lambda year:year%4==0 and (year%100!=0 or year%400==0)),
      'relative_change':([[15,10],[2,-2],[2,0],[7,4]],lambda new,old:(new-old)/old if old>0 else None),
      'reverse_words':([['  red  green blue '],[''],['one'],[' a\tb\n c ']],lambda text:' '.join(reversed(text.split()))),
      'ceiling_multiple':([[5,3],[-5,3],[0,3],[6,3],[19,4]],lambda n,step:((n+step-1)//step)*step),
      'duration_parts':([[n] for n in (3661,59,7200,0,86399)],lambda n:(n//3600,n//60%60,n%60)),
      'first_nonempty':([[['','alpha','beta']],[['','']],[[]],[['x','y']]],lambda values:next((value for value in values if value!=''),None)),
      'unique_intersection':([[[3,1,1,2],[2,3,3,4]],[[],[]],[[1],[2]],[[5,4,5],[5,6]]],lambda left,right:sorted(set(left)&set(right))),
      'matrix_transpose':([ [ [[1,2],[3,4]] ], [ [] ], [ [[1,2],[3]] ], [ [[1],[2],[3]] ] ],_transpose),
    }
    inputs,function=common[name];expected=[]
    for args in inputs:
        try:expected.append({'kind':'return','value':_normal(function(*args))})
        except Exception as error:expected.append({'kind':'raise','type':type(error).__name__})
    return inputs,expected


def _chunks(values,size):
    if size<=0:raise ValueError()
    return [values[index:index+size] for index in range(0,len(values),size)]
def _boolean(value):
    value=value.strip().casefold()
    if value in {'true','yes','1'}:return True
    if value in {'false','no','0'}:return False
    raise ValueError()
def _index(values,target):
    try:return values.index(target)
    except ValueError:return -1
def _balanced(text):
    depth=0
    for character in text:
        depth+=1 if character=='(' else -1 if character==')' else 0
        if depth<0:return False
    return depth==0
def _transpose(rows):
    if rows and any(len(row)!=len(rows[0]) for row in rows):raise ValueError()
    return [list(row) for row in zip(*rows)]


def equivalent(actual,expected):
    if isinstance(expected,bool) or expected is None:return type(actual) is type(expected) and actual==expected
    if isinstance(expected,(int,float)):
        return type(actual) in {int,float} and actual==expected
    if isinstance(expected,list):return isinstance(actual,list) and len(actual)==len(expected) and all(equivalent(a,b) for a,b in zip(actual,expected))
    if isinstance(expected,dict):return isinstance(actual,dict) and set(actual)==set(expected) and all(equivalent(actual[key],value) for key,value in expected.items())
    return type(actual) is type(expected) and actual==expected


POLICY_REASONS={'No solve function','Module side effects are not pure code','Non-pure name',
    'Attribute capability is not pure','Non-pure code construct','Non-pure function','Non-pure import',
    'Non-pure import member','Literal format fields must not traverse objects','Dynamic format is not a pure capability',
    'Oracle does not cover all candidate edits'}


def pure_format(node):
    if not isinstance(node.value,ast.Constant) or not isinstance(node.value.value,str):
        raise ValueError('Dynamic format is not a pure capability')
    for _literal,field,spec,conversion in string.Formatter().parse(node.value.value):
        if field is None:continue
        if ((field and not field.isdecimal() and (not field.isidentifier() or field.startswith('_')))
            or any(character in field for character in '.[]') or '{' in spec or '}' in spec
            or conversion not in (None,'s','r','a')):
            raise ValueError('Literal format fields must not traverse objects')


def pure_source(text):
    tree=ast.parse(text)
    if not any(isinstance(node,ast.FunctionDef) and node.name=='solve' for node in tree.body):raise ValueError('No solve function')
    for node in tree.body:
        if isinstance(node,(ast.Import,ast.ImportFrom,ast.FunctionDef)):continue
        if isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):continue
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            ast.literal_eval(node.value);continue
        raise ValueError('Module side effects are not pure code')
    for node in ast.walk(tree):
        if isinstance(node,ast.Name) and (node.id in BANNED or node.id.startswith('__')):raise ValueError('Non-pure name')
        if isinstance(node,ast.Attribute):
            if node.attr=='format':pure_format(node)
            elif node.attr not in PURE_ATTRIBUTES:raise ValueError('Attribute capability is not pure')
        if isinstance(node,(ast.ClassDef,ast.AsyncFunctionDef,ast.Global,ast.Nonlocal,ast.With,ast.AsyncWith)):
            raise ValueError('Non-pure code construct')
        if isinstance(node,ast.FunctionDef):
            if node.name.startswith('__') or node.decorator_list:raise ValueError('Non-pure function')
            for default in [*node.args.defaults,*[value for value in node.args.kw_defaults if value is not None]]:ast.literal_eval(default)
        if isinstance(node,ast.Import):
            if any(alias.name not in {'math','collections','itertools','functools','statistics','calendar'} for alias in node.names):raise ValueError('Non-pure import')
        if isinstance(node,ast.ImportFrom):
            if node.level or node.module not in {'math','collections','itertools','functools','statistics','calendar','__future__'}:raise ValueError('Non-pure import')
            allowed={'math':{'ceil','floor'},'collections':{'Counter','deque','defaultdict'},'itertools':{'chain'},
                'functools':{'reduce'},'statistics':{'mean'},'calendar':{'isleap'},'__future__':{'annotations'}}[node.module]
            if any(alias.name not in allowed for alias in node.names):raise ValueError('Non-pure import member')
    return tree


def _run(argv,timeout=8):
    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ,'stdout');selector.register(process.stderr,selectors.EVENT_READ,'stderr')
    chunks={'stdout':bytearray(),'stderr':bytearray()};deadline=time.monotonic()+timeout;reason=None
    try:
        while selector.get_map():
            if time.monotonic()>deadline:reason='oracle_timeout';break
            for key,_ in selector.select(.1):
                block=key.fileobj.read1(8192)
                if not block:selector.unregister(key.fileobj);continue
                chunks[key.data].extend(block)
                if len(chunks[key.data])>65536:reason='oracle_output_unbounded';break
            if reason:break
        if reason:
            import os
            os.killpg(process.pid,signal.SIGKILL)
        process.wait(timeout=2)
        return process.returncode,bytes(chunks['stdout']),bytes(chunks['stderr']),reason
    finally:
        if process.poll() is None:process.kill();process.wait()
        selector.close();process.stdout.close();process.stderr.close()


class PureFunctionOracle:
    def __init__(self,repair,identifier):
        self.repair,self.identifier=repair,identifier
        path,tree,body=repair._read(identifier);spec=repair.config.get('independent_oracle') or {}
        marker=json.loads((tree/'synthetic-fixture.json').read_text())
        if spec.get('kind')!='synthetic_pure_python.v1' or marker.get('kind')!='synthetic_ops_case' or marker.get('business_rows') is not False or marker.get('case')!=spec.get('case'):
            raise ValueError('Independent oracle is only configured for declared synthetic code')
        self.case=spec['case'];self.module=spec['module'];self.runtime=Path(spec['runtime']).resolve()
        if self.module!='ledger/ledger/synthetic_case.py':raise ValueError('Oracle has no authority for another module')
        self.inputs,self.expected=cases(self.case)

    def __call__(self):
        path,tree,body=self.repair._read(self.identifier);source=(tree/self.module).resolve()
        checked={'schema':1,'kind':'external_pure_function_oracle.v1','case':self.case,'network':'isolated',
            'host_fallback':False,'code_sha256':file_sha(source),'inputs_sha':digest(self.inputs),
            'expected_sha':digest(self.expected),'checked':len(self.inputs),'passed':False,'executed':False,'failure_category':None}
        folder=path/'external-oracle'/uuid.uuid4().hex;folder.mkdir(parents=True)
        receipt=folder/'oracle-receipt.json'
        try:
            changed=self.repair._git(tree,'diff','--name-only',body['base_code_sha']).decode().splitlines()
            changed+=list(filter(None,self.repair._git(tree,'ls-files','--others','--exclude-standard').decode().splitlines()))
            if any(relative!=self.module for relative in changed):raise ValueError('Oracle does not cover all candidate edits')
            text=source.read_text();pure_source(text)
            runner=folder/'runner.py';runner.write_text(RUNNER);inputs=folder/'inputs.json';inputs.write_text(json.dumps(self.inputs))
            candidate=folder/'candidate.py';candidate.write_text(text)
            bwrap=shutil.which('bwrap')
            if not bwrap:raise ValueError('Network oracle unavailable')
            argv=[str(Path(bwrap).resolve()),'--unshare-all','--unshare-net','--die-with-parent','--new-session','--clearenv',
                '--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp','--proc','/proc','--dev','/dev','--tmpfs','/tmp']
            for system in ('/usr','/bin','/lib','/lib64'):
                if Path(system).exists():argv+=['--ro-bind',system,system]
            argv+=['--ro-bind',str(self.runtime),'/runtime','--ro-bind',str(candidate),'/candidate.py',
                '--ro-bind',str(runner),'/runner.py','--ro-bind',str(inputs),'/inputs.json','--chdir','/tmp',
                '--','/runtime/bin/python','-I','-S','-B','/runner.py']
            code,stdout,stderr,reason=_run(argv)
            checked.update(executed=True,exit_code=code,stdout_sha256=hashlib.sha256(stdout).hexdigest(),stderr_sha256=hashlib.sha256(stderr).hexdigest())
            if reason:checked.update(reason=reason,failure_category='harness_resource_limit')
            elif code==0:
                actual=json.loads(stdout)
                checked['passed']=equivalent(actual,self.expected)
                if not checked['passed']:checked['failure_category']='contract_mismatch'
            else:checked.update(reason='oracle_candidate_failed',failure_category='candidate_execution_failed')
        except (ValueError,OSError,SyntaxError,TypeError) as error:
            checked['reason']='oracle_rejected_'+type(error).__name__
            if str(error) in POLICY_REASONS:
                checked['policy_hint']=str(error);checked['failure_category']='policy_rejected'
            else:checked['failure_category']='harness_or_candidate_invalid'
        receipt.write_text(json.dumps(checked,sort_keys=True,ensure_ascii=False))
        return {**checked,'receipt_path':str(receipt),'receipt_sha256':file_sha(receipt)}


def configured_oracle(repair,identifier):
    if not repair.config.get('independent_oracle'):return None
    return PureFunctionOracle(repair,identifier)
