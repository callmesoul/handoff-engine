# Handoff Engine

Handoff Engine 是一个 **Git-native 的结构化任务交接引擎**：任务、状态与审计事件保存在独立 Git 交接仓；worker 在隔离工作区调用 Codex、ZCode 或 DSH Agent 完成实现，再经过确定性 gate、受控 PR/合并、部署版本对齐和结构化验收。

核心原则：**Git 是唯一真实来源**。HTTP 服务、SQLite 索引、通知和桌面面板都只是可重建的加速层。

## 引擎与数据分离

本仓只保存可复用引擎，不保存任何真实任务或机器配置：

```text
handoff-engine/                 Python 引擎、CLI、测试、插件和部署模板
handoff-store/                  独立私有 Git 仓
  config/                       团队共享配置
  tasks/                        当前任务快照
  events/                       不可变审计事件
  contracts/                    契约附件
  artifacts/                    复现材料
```

`handoffctl` 通过 `--root <store>` 访问交接仓。也可设置 `HANDOFF_ROOT` 供 skill、服务脚本和人工命令统一使用。

## 功能

- 结构化任务 schema 与字段级语义校验
- Git CAS、原子任务/事件提交、租约和非快进重试
- 隔离 worktree 中的 Codex / ZCode / DSH Agent 执行
- 路径、文件大小、符号链接、凭据和迁移 gate
- GitHub PR、自动合并条件和 merge SHA 回填
- 部署版本轮询、环境漂移检测、HTTP/OpenAPI/仓库脚本验收
- 双向签名通知、前端会话恢复、可重建管理索引
- 本地任务面板与 DSH 插件

## 安装

要求 Python 3.10+ 和 Git；GitHub PR 操作需要 `gh`；按所选执行器安装 `codex` 或 `zcode`。

```bash
git clone <handoff-engine-url> ~/code/handoff-engine
cd ~/code/handoff-engine
python3 -m venv .venv
.venv/bin/pip install -e .

handoffctl --help
```

安装 wheel 后，任务、事件和 Codex 结果 schema 会随 Python 包一起安装，不依赖引擎 checkout。

## 创建独立交接仓

仓库提供不含真实数据的模板：

```bash
cp -a examples/handoff-store ~/code/handoff-store
cd ~/code/handoff-store
git init -b main
# 修改 config/*.yaml，配置团队仓库、环境、gate 与本机覆盖
# 再添加私有远端并提交
```

至少需要：

- `config/repositories.yaml`：交接仓及业务仓、角色、基础分支、远端，以及后端仓库的 scope profiles
- `config/environments.yaml`：验收环境与版本接口
- `config/checks.yaml`：允许执行的检查与确定性 gate
- `config/admin.yaml`：索引/API 配置（示例 token 必须替换）
- `config/codex.yaml`：Codex 执行器配置
- `config/local.yaml`：机器专属路径和密钥文件位置；不提交 Git

```bash
export HANDOFF_ROOT=~/code/handoff-store
handoffctl --root "$HANDOFF_ROOT" doctor --role frontend
handoffctl --root "$HANDOFF_ROOT" validate
```

## 基本流程

```bash
# 创建任务
handoffctl --root "$HANDOFF_ROOT" create \
  --from-file doc/task-draft.example.json \
  --source-path ~/code/web \
  --contract doc/openapi.example.yaml

# 后端 worker
handoffctl --root "$HANDOFF_ROOT" worker --worker backend-01 --role backend

# 查询、部署对齐与验收
handoffctl --root "$HANDOFF_ROOT" show <task-id>
handoffctl --root "$HANDOFF_ROOT" deliver <task-id>

# 管理与审计
handoffctl --root "$HANDOFF_ROOT" events <task-id>
handoffctl --root "$HANDOFF_ROOT" admin index
```

完整协议与部署说明：

- [`doc/HANDOFF-SPEC.md`](doc/HANDOFF-SPEC.md)
- [`doc/ADR-0001-platform.md`](doc/ADR-0001-platform.md)
- [`doc/DEPLOY-AGENT.md`](doc/DEPLOY-AGENT.md)
- [`doc/DEPLOY-BACKEND.md`](doc/DEPLOY-BACKEND.md)
- [`doc/DEPLOY-FRONTEND.md`](doc/DEPLOY-FRONTEND.md)
- [`doc/AUTO-UPDATE.md`](doc/AUTO-UPDATE.md)
- [`doc/SCOPE-TEMPLATES-UPGRADE.md`](doc/SCOPE-TEMPLATES-UPGRADE.md)

## 本地验证

```bash
python -m pytest
bash scripts/smoke.sh
bash scripts/make-sandbox.sh --reset
```

`smoke.sh` 和 `make-sandbox.sh` 只创建临时业务仓与交接仓，不访问真实项目。

## 扩展

- `dsh-plugin/`：把完成事件排回原 DSH 会话
- `dsh-handoff-panel/`：在 DSH 中嵌入本地任务面板
- `dsh-handoff-studio/`：用 DSH Agent 执行交接任务
- `.agents/skills/`：`handoff` / `deliver` skill 模板
- `deploy/`：systemd、Windows 计划任务与 CI 示例

## 安全边界

- 任务不能携带任意 shell；检查只能引用审核过的配置项或结构化 HTTP/OpenAPI 检查。
- worker 不修改人的工作区，不 force push，不直接写主分支。
- 凭据从 agent 子进程环境中剔除；日志脱敏后才持久化。
- 交接仓通常包含内部需求、接口契约和运行痕迹，应使用私有仓库并单独控制权限。

## License

MIT，见 [`LICENSE`](LICENSE)。
