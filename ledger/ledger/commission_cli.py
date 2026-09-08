"""Administrative entry point; secrets are read from protected files, never argv."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .commission_registry import Registry
from .workspace import default_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=default_root())
    parser.add_argument("--model", type=Path, default=Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce")
    commands = parser.add_subparsers(dest="command", required=True)
    operator = commands.add_parser("operator-add")
    operator.add_argument("name")
    operator.add_argument("--password-file", type=Path, required=True)
    operator.add_argument("--admin", action="store_true")
    commands.add_parser("catalog-sync")
    stage = commands.add_parser("stage")
    stage.add_argument("file", type=Path)
    stage.add_argument("--effective-from", required=True)
    stage.add_argument("--by", required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("batch_id")
    activate.add_argument("--by", required=True)
    activate.add_argument("--reason", required=True)
    commands.add_parser("status")
    policy = commands.add_parser("policy-set")
    policy.add_argument("--effective-from", required=True)
    policy.add_argument("--base", required=True)
    policy.add_argument("--wages", choices=["pending", "skip_preview"], default="pending")
    policy.add_argument("--on-loss", choices=["inherit", "skip", "deduct"], default="inherit")
    policy.add_argument("--by", required=True)
    policy.add_argument("--reason", required=True)
    args = parser.parse_args()
    registry = Registry(args.home)
    if args.command == "operator-add":
        registry.operator_add(args.name, args.password_file.read_text(encoding="utf-8").strip(), admin=args.admin)
        result = {"operator": args.name, "created": True}
    elif args.command == "catalog-sync":
        from .commission_catalog import refresh
        from .model.loader import load_model
        result = refresh(registry, load_model(args.model))
    elif args.command == "stage":
        from .commission_import import stage
        from .model.loader import load_model
        result = stage(registry, load_model(args.model), args.file.read_bytes(), args.file.name,
                       args.effective_from, args.by)
    elif args.command == "activate":
        from .commission_import import activate
        result = activate(registry, args.batch_id, args.by, args.reason)
    elif args.command == "policy-set":
        from .model.loader import load_model
        model = load_model(args.model)
        if args.base not in {n.id for n in model.commission_bases()}:
            raise ValueError("所选节点不能用于提成基数")
        body = {"base_node": args.base, "on_loss": args.on_loss, "wages": args.wages}
        if args.on_loss == "inherit":
            body["loss_by_store"] = {s.id: s.commission_on_loss for s in model.stores}
        result = registry.save_policy(body,
                                       args.effective_from, args.by, args.reason,
                                       stores=[s.id for s in model.active_stores()])
    else:
        with registry.connect() as conn:
            result = {"revision": registry.revision(),
                      "active_schemes": conn.execute("SELECT count(*) FROM scheme WHERE active_version IS NOT NULL").fetchone()[0],
                      "pending": [dict(r) for r in conn.execute("SELECT * FROM pending")],
                      "imports": [dict(r) for r in conn.execute("SELECT * FROM import_batch")]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
