"""Print exact JSON field differences between the newest runs for one store-period."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def differences(left, right, path=""):
    if type(left) is not type(right):
        return [(path, left, right)]
    if isinstance(left, dict):
        out = []
        for key in sorted(left.keys() | right.keys()):
            if key not in left or key not in right:
                out.append((f"{path}/{key}", left.get(key), right.get(key)))
            else:
                out.extend(differences(left[key], right[key], f"{path}/{key}"))
        return out
    if isinstance(left, list):
        out = []
        if len(left) != len(right):
            out.append((f"{path}/length", len(left), len(right)))
        for index, (a, b) in enumerate(zip(left, right)):
            out.extend(differences(a, b, f"{path}/{index}"))
        return out
    return [] if left == right else [(path, left, right)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--period", required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    rows = connection.execute(
        "select id,result from run where store_id=? and period=? order by id desc limit 2",
        (args.store, args.period),
    ).fetchall()
    if len(rows) != 2:
        raise SystemExit("需要至少两次 run")
    diff = differences(json.loads(rows[1][1]), json.loads(rows[0][1]))
    print(json.dumps({
        "before_run": rows[1][0], "after_run": rows[0][0],
        "difference_count": len(diff), "differences": diff[:args.limit],
    }, ensure_ascii=False, indent=2))
    return 1 if diff else 0


if __name__ == "__main__":
    raise SystemExit(main())
