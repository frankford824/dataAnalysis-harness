"""Offline positive coverage before spending live calibration calls."""
import json
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'agent-worker'),str(ROOT),str(ROOT/'ledger')]
from ledger_agent.ops_evaluate import CASES,create_case
from ledger_agent.ops_oracle import configured_oracle,pure_source

REFERENCES={
 'inclusive_total':['def solve(n):\n    return n*(n+1)//2\n','def solve(n):\n    return sum(range(n+1))\n'],
 'clamp':['def solve(x,low,high):\n    return max(low,min(high,x))\n'],
 'stable_unique':['def solve(values):\n    return list(dict.fromkeys(values))\n','def solve(values):\n    result=[]\n    for value in values:\n        if value not in result: result.append(value)\n    return result\n'],
 'mean_empty':['def solve(values):\n    return sum(values)/len(values) if values else None\n','from statistics import mean\ndef solve(values):\n    return mean(values) if values else None\n'],
 'chunk_tail':['def solve(values,size):\n    if size<=0: raise ValueError("size must be positive")\n    return [values[i:i+size] for i in range(0,len(values),size)]\n'],
 'normalize_key':['def solve(value):\n    return value.strip().casefold()\n'],
 'count_groups':['from collections import Counter\ndef solve(values):\n    return dict(Counter(values))\n'],
 'parse_boolean':["def solve(value):\n    value=value.strip().casefold()\n    if value in ('true','yes','1'): return True\n    if value in ('false','no','0'): return False\n    raise ValueError('invalid {!r}'.format(value))\n", "_YES={'true','yes','1'}\n_NO={'false','no','0'}\ndef solve(value):\n    value=value.strip().lower()\n    if value in _YES: return True\n    if value in _NO: return False\n    raise ValueError('invalid {value!r}'.format(value=value))\n"],
 'flatten_once':['def solve(values):\n    return [item for group in values for item in group]\n','from itertools import chain\ndef solve(values):\n    return list(chain.from_iterable(values))\n'],
 'adjacent_pairs':['def solve(values):\n    return list(zip(values,values[1:]))\n'],
 'binary_index':['def solve(values,target):\n    try: return values.index(target)\n    except ValueError: return -1\n'],
 'balanced_parentheses':["def solve(text):\n    depth=0\n    for character in text:\n        if character=='(': depth+=1\n        elif character==')': depth-=1\n        if depth<0: return False\n    return depth==0\n"],
 'leap_year':['def solve(year):\n    return year%4==0 and (year%100!=0 or year%400==0)\n','from calendar import isleap\ndef solve(year):\n    return isleap(year)\n'],
 'relative_change':['def solve(new,old):\n    return (new-old)/old if old>0 else None\n'],
 'reverse_words':["def solve(text):\n    return ' '.join(text.split()[::-1])\n"],
 'ceiling_multiple':['def solve(n,step):\n    return ((n+step-1)//step)*step\n','from math import ceil\ndef solve(n,step):\n    return ceil(n/step)*step\n'],
 'duration_parts':['def solve(seconds):\n    return seconds//3600,seconds//60%60,seconds%60\n'],
 'first_nonempty':["def solve(values):\n    return next((value for value in values if value!=''),None)\n"],
 'unique_intersection':['def solve(left,right):\n    return sorted(set(left)&set(right))\n'],
 'matrix_transpose':['def solve(rows):\n    if rows and any(len(row)!=len(rows[0]) for row in rows): raise ValueError("ragged rows")\n    return [list(row) for row in zip(*rows)]\n'],
}


@pytest.mark.parametrize('item',CASES,ids=[item[0] for item in CASES])
def test_original_bug_fails_and_correct_reference_variants_pass_exact_contract(tmp_path,item):
    repair,identifier=create_case(tmp_path/item[0],item,ROOT/'ledger/.venv')
    oracle=configured_oracle(repair,identifier)
    initial=oracle();assert initial['executed'] is True and initial['passed'] is False
    for source in REFERENCES[item[0]]:
        pure_source(source);repair.write_code(identifier,'ledger/ledger/synthetic_case.py',source)
        repair.run_check(identifier,'original_contract')
        from ops.ledger_sentinel.state import digest
        diagnostic=json.loads((repair.path(identifier)/('check-'+digest('original_contract')[:12]+'.json')).read_text())
        assert diagnostic['exit_code']==0,item[0]
        observed=oracle();assert observed['executed'] is True and observed['passed'] is True,(item[0],observed)


@pytest.mark.parametrize('payload',[
 "def solve(*args):\n    g=(0 for x in [0])\n    return g.gi_frame.f_builtins['__import__']('os').getcwd()\n",
 "def solve(value):\n    return '{0.__globals__}'.format(solve)\n",
 "def solve(value):\n    return '{0[__import__]}'.format(value)\n",
 "def solve(value):\n    return '{0:{1.gi_frame}}'.format(value,value)\n",
 'import os\ndef solve(value):\n    os._exit(0)\n',
])
def test_reflection_and_process_variants_are_not_legal_equivalents(payload):
    with pytest.raises(ValueError):pure_source(payload)
