#!/usr/bin/env bash
# 造两个一次性测试仓库，外加一个独立的交接仓，用来试 create / validate / index。
# 不碰真实仓库，不联网，不调 codex。
#
#   bash scripts/make-sandbox.sh            # 首次创建，已存在则复用
#   bash scripts/make-sandbox.sh --reset    # 删掉重建（只删带标记的沙箱目录）
#
# 产物默认在 ~/code/handoff-sandbox：
#   api.git  api/   —— 后端裸仓与工作副本（dev 分支）
#   web.git  web/   —— 前端裸仓与工作副本
#   store/          —— 交接仓（config + tasks + events），任务单写在这里
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SB="${HANDOFF_SANDBOX:-$HOME/code/handoff-sandbox}"
MARKER=".handoff-sandbox"

reset=0
for arg in "$@"; do
  case "$arg" in
    --reset) reset=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

if [ "$reset" = "1" ] && [ -e "$SB" ]; then
  if [ ! -f "$SB/$MARKER" ]; then
    echo "拒绝删除 $SB：没有 $MARKER 标记，看起来不是本脚本创建的目录" >&2
    exit 3
  fi
  rm -rf "$SB"
fi

if [ -d "$SB/api.git" ] && [ -d "$SB/web.git" ] && [ -d "$SB/store" ]; then
  echo "沙箱已存在：$SB"
  echo "要重建就加 --reset"
  exit 0
fi

mkdir -p "$SB"
touch "$SB/$MARKER"

export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.com
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.com

seed_repo() {
  local name="$1" branch="$2"
  git init --bare -q -b main "$SB/$name.git"
  rm -rf "$SB/$name"
  mkdir -p "$SB/$name"
  (
    cd "$SB/$name"
    git init -q -b "$branch"
  )
}

# ---- 后端：org/api 的替身 -------------------------------------------------
seed_repo api dev
(
  cd "$SB/api"
  mkdir -p src/orders tests/orders scripts
  cat > README.md <<'EOF'
# api（沙箱后端）

订单服务的最小骨架，只用来试交接流程。
EOF
  cat > src/orders/Order.java <<'EOF'
package orders;

public class Order {
    private final String id;
    private final long amountCents;

    public Order(String id, long amountCents) {
        this.id = id;
        this.amountCents = amountCents;
    }

    public String id() { return id; }
    public long amountCents() { return amountCents; }
}
EOF
  cat > tests/orders/OrderTest.java <<'EOF'
package orders;

public class OrderTest {
    public static void main(String[] args) {
        Order order = new Order("o-1", 1200);
        if (!"o-1".equals(order.id())) throw new AssertionError("id");
        if (order.amountCents() != 1200) throw new AssertionError("amount");
        System.out.println("ok");
    }
}
EOF
  cat > scripts/contract-check.sh <<'EOF'
#!/usr/bin/env bash
# 验收时由 handoffctl 以 argv 直接执行，不经过 shell
set -euo pipefail
test -f src/orders/Order.java
echo "contract-check: src/orders/Order.java 存在"
EOF
  chmod +x scripts/contract-check.sh
  cat > .gitignore <<'EOF'
build/
out/
EOF
  git add -A
  git commit -qm "init: 订单服务骨架"
  git remote add origin "$SB/api.git"
  git push -q origin dev:dev
  git push -q origin dev:main
)

# ---- 前端：org/web 的替身 -------------------------------------------------
seed_repo web main
(
  cd "$SB/web"
  mkdir -p src/api src/pages
  cat > README.md <<'EOF'
# web（沙箱前端）

只放一个订单列表页和一个接口封装，用来试「前端发单」。
EOF
  cat > src/api/orders.ts <<'EOF'
export type OrderStatus = "OPEN" | "PAID" | "CANCELLED";

export interface Order {
  id: string;
  amount: number;
  created_at: string;
  // TODO: 后端加 status 字段后补上
}

export async function listOrders(page = 1): Promise<{ total: number; items: Order[] }> {
  const res = await fetch(`/api/orders?page=${page}`);
  if (!res.ok) throw new Error(`orders: HTTP ${res.status}`);
  return res.json();
}
EOF
  cat > src/pages/OrdersPage.ts <<'EOF'
import { listOrders } from "../api/orders";

export async function renderOrders() {
  const { items } = await listOrders();
  return items.map((o) => `${o.id} ${o.amount}`);
}
EOF
  git add -A
  git commit -qm "init: 订单列表页与接口封装"
  git remote add origin "$SB/web.git"
  git push -q origin main:main
  # 前端也留一条 dev，配置里 allowed_base_refs 才站得住
  git push -q origin main:dev
)

# ---- 交接仓 ---------------------------------------------------------------
cp -a "$SRC/examples/handoff-store/." "$SB/store/"

python3 - "$SB" <<'PY'
import json, pathlib, sys, yaml

sb = pathlib.Path(sys.argv[1])
config = sb / "store" / "config"

(config / "repositories.yaml").write_text(
    yaml.safe_dump(
        {
            "store": {"repo": None, "branch": "main", "remote": "origin"},
            "repositories": [
                {
                    "id": "local/api",
                    "role": "backend",
                    "host": "local",
                    "default_branch": "main",
                    "allowed_base_refs": ["dev"],
                    "pr_base_ref": "dev",
                    "remote": str(sb / "api.git"),
                    "cache_dir": ".handoff-run/repo-cache/local__api",
                },
                {
                    "id": "local/web",
                    "role": "frontend",
                    "host": "github",
                    "default_branch": "main",
                    "allowed_base_refs": ["dev"],
                    "pr_base_ref": "dev",
                    "remote": str(sb / "web.git"),
                    "cache_dir": ".handoff-run/repo-cache/local__web",
                },
            ],
        },
        allow_unicode=True,
        sort_keys=False,
    ),
    encoding="utf-8",
)

(config / "environments.yaml").write_text(
    yaml.safe_dump(
        {
            "environments": [
                {
                    "name": "dev",
                    "url": "http://127.0.0.1:8099",
                    "version_path": "/version",
                    "shared": True,
                    "concurrency_group": "deploy-sandbox-dev",
                    "auto_deploy": False,
                }
            ],
            "deploy_poll": {"interval_seconds": 1, "timeout_seconds": 10, "request_timeout_seconds": 2},
            "verify_poll": {"interval_seconds": 1, "timeout_seconds": 10, "request_timeout_seconds": 2},
        },
        allow_unicode=True,
        sort_keys=False,
    ),
    encoding="utf-8",
)

(config / "checks.yaml").write_text(
    yaml.safe_dump(
        {
            "test_suites": {},
            "repo_scripts": {
                "contract-check": {
                    "repo": "local/api",
                    "path": "scripts/contract-check.sh",
                    "timeout_seconds": 60,
                }
            },
            "gate": {
                "required_checks": [],
                "high_risk_paths": [".github/**", "infra/prod/**", "**/migrations/**", "**/auth/**"],
                "deny_auto_merge_if_migration_required": True,
                "max_changed_files": 50,
                "max_file_bytes": 65536,
            },
            "verifier": {
                "output_limit_chars": 2000,
                "max_parallel": 1,
                "repo_paths": {"local/api": str(sb / "api")},
            },
        },
        allow_unicode=True,
        sort_keys=False,
    ),
    encoding="utf-8",
)

(config / "admin.yaml").write_text(
    yaml.safe_dump(
        {
            "index": {"database": ".handoff-run/index/handoff.sqlite3", "poll_interval_seconds": 30},
            "server": {"host": "127.0.0.1", "port": 8787, "read_only": False},
            "users": [
                {"token": "sandbox-viewer", "id": "viewer@sandbox", "role": "viewer", "repos": ["*"]},
                {"token": "sandbox-operator", "id": "operator@sandbox", "role": "operator", "repos": ["local/api"]},
            ],
            "retention": {"codex_logs_days": 7, "check_output_days": 7},
        },
        allow_unicode=True,
        sort_keys=False,
    ),
    encoding="utf-8",
)

# 沙箱任务草稿：路径换成占位，契约由 create 复制
draft = {
    "title": "订单接口返回增加 status 字段",
    "source": {"role": "frontend", "repo": "local/web", "commit_sha": "0" * 40},
    "target": {"role": "backend", "repo": "local/api", "base_ref": "dev"},
    "requirement": (
        "GET /api/orders 的响应增加 status 字段。\n\n"
        "- 类型：string\n"
        "- 取值：OPEN | PAID | CANCELLED\n"
        "- 历史订单按 paid 时间推导，不加数据库列\n"
        "- 不得返回 null"
    ),
    "scope": {
        "out_of_scope": ["不改鉴权", "不改金额计算"],
        "allowed_paths": ["src/orders/**", "tests/orders/**"],
        "forbidden_paths": [".github/**", "infra/prod/**", "**/migrations/**", "**/auth/**"],
    },
    "acceptance": {
        "environment": "dev",
        "checks": [
            {
                "id": "orders-status",
                "type": "http",
                "method": "GET",
                "url": "{base_url}/api/orders?page=1",
                "expect_status": 200,
                "expect_jsonpath": [{"path": "$.items[0].status", "matches": "^(OPEN|PAID|CANCELLED)$"}],
            },
            {"id": "contract", "type": "repo_script", "repo": "local/api", "name": "contract-check"},
        ],
    },
    "migration": {
        "required": False,
        "backward_compatible": True,
        "plan": "",
        "rollback": "",
        "manual_approval_required": False,
        "order": "manual",
    },
}
(sb / "task-draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

# 假 codex：沙箱里替代真实模型。只写一个文件，回一条符合 --output-schema 的结果。
cat > "$SB/stub-codex.sh" <<'STUB'
#!/usr/bin/env bash
# 沙箱桩：不联网、不消耗额度，行为固定。
set -euo pipefail
if [ "${1:-}" = "--version" ]; then
  echo "codex-cli 0.0.0-sandbox"
  exit 0
fi
cat >/dev/null || true
mkdir -p src/orders tests/orders
cat > src/orders/OrderStatus.java <<'JAVA'
package orders;

public enum OrderStatus {
    OPEN,
    PAID,
    CANCELLED;

    public static OrderStatus derive(long amountCents, boolean paid) {
        if (paid) return PAID;
        return amountCents == 0 ? CANCELLED : OPEN;
    }
}
JAVA
cat > tests/orders/OrderStatusTest.java <<'JAVA'
package orders;

public class OrderStatusTest {
    public static void main(String[] args) {
        if (OrderStatus.derive(100, true) != OrderStatus.PAID) throw new AssertionError("paid");
        if (OrderStatus.derive(0, false) != OrderStatus.CANCELLED) throw new AssertionError("cancelled");
        if (OrderStatus.derive(100, false) != OrderStatus.OPEN) throw new AssertionError("open");
        System.out.println("ok");
    }
}
JAVA
printf '%s\n' '{"type":"thread.started","thread_id":"01JSTUBSANDBOXTHREAD00000"}'
printf '%s\n' '{"type":"item.completed","item":{"type":"agent_message","text":"{\"status\":\"completed\",\"summary\":\"新增 OrderStatus 枚举与推导逻辑\",\"changed_paths\":[\"src/orders/OrderStatus.java\",\"tests/orders/OrderStatusTest.java\"],\"tests\":[{\"name\":\"sandbox-stub\",\"status\":\"passed\"}],\"migration_required\":false,\"risks\":[],\"notes\":\"沙箱桩，不是真实模型产出\"}"}}'
STUB
chmod +x "$SB/stub-codex.sh"

(
  cd "$SB/store"
  git init -q -b main
  git add -A
  git commit -qm "init: 沙箱交接仓"
)

echo "沙箱就绪：$SB"
echo
echo "下一步："
echo "  ./bin/handoffctl --root $SB/store doctor"
echo "  ./bin/handoffctl --root $SB/store create \\"
echo "    --from-file $SB/task-draft.json \\"
echo "    --source-path $SB/web \\"
echo "    --source-thread sandbox-frontend \\"
echo "    --source-executor codex \\"
echo "    --contract $SRC/doc/openapi.example.yaml"
echo
echo "跑 worker（假 codex，本地 PR 记录，不联网）："
echo "  ./bin/handoffctl --root $SB/store worker --worker sandbox-01 --role backend \\"
echo "    --codex-bin $SB/stub-codex.sh --once"
