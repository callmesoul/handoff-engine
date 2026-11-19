#!/usr/bin/env bash
# 本地端到端冒烟：建后端 bare 远端、建交接仓、create、show、events、index、validate。
# 不触碰真实仓库，不调用 codex。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$SRC/.venv/bin/python}"
T="$(mktemp -d)"
export PYTHONPATH="$SRC/src"

git init --bare -q -b main "$T/api.git"
mkdir -p "$T/api-seed/src/orders"
(
  cd "$T/api-seed"
  git init -q -b dev
  echo 'class Order {}' > src/orders/Order.java
  git add -A
  git -c user.email=t@t -c user.name=t commit -qm seed
  git remote add origin "$T/api.git"
  git push -q origin dev:dev
  git push -q origin dev:main
)

H="$T/handoff"
cp -a "$SRC/examples/handoff-store/." "$H/"

"$PY" - "$H" "$T/api.git" <<'PY'
import sys, yaml, pathlib
h = pathlib.Path(sys.argv[1]); api = sys.argv[2]
p = h / "config" / "repositories.yaml"
d = yaml.safe_load(p.read_text())
d["store"]["repo"] = None
d["repositories"] = [
    {
        "id": "local/api",
        "role": "backend",
        "host": "local",
        "default_branch": "main",
        "allowed_base_refs": ["dev"],
        "pr_base_ref": "dev",
        "remote": api,
    },
    {
        "id": "local/web",
        "role": "frontend",
        "host": "local",
        "default_branch": "main",
        "allowed_base_refs": ["dev"],
        "remote": api,
    },
]
p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))
PY

(
  cd "$H"
  git init -q -b main
  git add -A
  git -c user.email=t@t -c user.name=t commit -qm init
)

CTL=("$PY" -m handoff.cli --root "$H")

echo "== validate（空仓） =="
"${CTL[@]}" validate

cat > "$T/draft.json" <<'JSON'
{
  "title": "订单接口增加 status 字段",
  "source": {"role": "frontend", "repo": "local/web", "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "target": {"role": "backend", "repo": "local/api", "base_ref": "dev"},
  "requirement": "GET /api/orders 返回增加 status 字段，取值 OPEN/PAID/CANCELLED。",
  "scope": {
    "out_of_scope": ["不要动鉴权"],
    "allowed_paths": ["src/orders/**"],
    "forbidden_paths": [".github/**"]
  },
  "acceptance": {
    "environment": "dev",
    "checks": [
      {"id": "version", "type": "http", "method": "GET", "url": "{base_url}/version", "expect_status": 200}
    ]
  },
  "migration": {"required": false, "backward_compatible": true, "plan": "", "rollback": "", "manual_approval_required": false}
}
JSON

echo "== create =="
"${CTL[@]}" create --from-file "$T/draft.json" --contract "$SRC/doc/HANDOFF-SPEC.md" --contract-kind text \
  --source-thread smoke-frontend --source-executor codex --source-workspace "$T" --json

TID="$("${CTL[@]}" list --json | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["tasks"][0]["id"])')"

echo "== show =="
"${CTL[@]}" show "$TID"
echo "== events =="
"${CTL[@]}" events "$TID" --json
echo "== admin index =="
"${CTL[@]}" admin index
echo "== validate（有任务后） =="
"${CTL[@]}" validate
echo "== git log =="
git -C "$H" log --oneline
echo "SMOKE OK ($H)"
