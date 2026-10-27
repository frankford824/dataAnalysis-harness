"""Install/check the independent SDK environment; never upgrade the ledger service venv."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPO=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venv',type=Path,required=True)
    parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args()
    target=args.venv.expanduser().resolve()
    if target in {(REPO/'ledger/.venv').resolve(),Path('D:/ledger/venv').resolve()}:
        raise SystemExit('Refusing to change or certify the financial service environment as an SDK worker')
    if not args.check_only:
        if sys.version_info[:2]!=(3,12):raise SystemExit('Bootstrap with the explicitly tested Python 3.12 interpreter')
        if target.exists() and not (target/'pyvenv.cfg').is_file():
            raise SystemExit('Target already contains non-venv files; never overwrite it')
        if not target.exists():subprocess.run([sys.executable,'-m','venv',str(target)],check=True)
    python=target/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    lock=REPO/'agent-worker/requirements.lock'
    if not python.is_file() or not lock.is_file():raise SystemExit('Worker environment/发行 lock is missing')
    if not args.check_only:
        subprocess.run([str(python),'-m','pip','install','-r',str(lock),
            '-e',str(REPO/'host-agent'),'-e',str(REPO/'agent-worker')],check=True)
    env={**os.environ,'PYTHONPATH':os.pathsep.join(str(REPO/p) for p in ('ledger','host-agent','agent-worker','.'))}
    subprocess.run([str(python),'-m','pip','check'],env=env,check=True)
    probe=('import sys; from importlib.metadata import version; '
        'from ledger_agent.runtime import Runtime; from finance_agent.control_plane import ControlPlaneClient; '
        'from ledger.assist import Config; '
        'v=version("agentscope"); '
        'sys.exit(0 if v=="2.0.11" and sys.version_info[:2]==(3,12) else 2)')
    subprocess.run([str(python),'-c',probe],env=env,check=True)
    print(json.dumps({'worker_environment':str(target),'sdk':'2.0.11','imports_verified':True,
        'lock_sha256':hashlib.sha256(lock.read_bytes()).hexdigest(),'financial_environment_changed':False}))


if __name__=='__main__':main()
