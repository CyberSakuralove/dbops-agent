"""Operator-only interface: inspect and decide pending requests on a local fixture.

python -m scripts.approve --db runs/demo/business.db
python -m scripts.approve --db runs/demo/business.db --request ID --decision approve \
    --actor engineer --reason 'confirmed duplicates against source'
"""

import argparse
import json
from pathlib import Path

from dbops_agent.guard.execution import ApprovalService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--request")
    parser.add_argument("--decision", choices=["approve", "deny"])
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("数据库不存在")
    service = ApprovalService(args.db)
    if args.request:
        if not all([args.decision, args.actor, args.reason]):
            parser.error("决策需要 --decision、--actor 和 --reason")
        service.decide(
            args.request, approve=args.decision == "approve", actor=args.actor, reason=args.reason
        )
        print("审批决策已保存。Agent 重试时仍会核验绑定、有效期与资源状态。")
    else:
        print(json.dumps(service.pending(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
