"""Run operational tests from the repository without assuming an editable install."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
for directory in (ROOT,ROOT/'ledger'):
    if str(directory) not in sys.path:sys.path.insert(0,str(directory))
