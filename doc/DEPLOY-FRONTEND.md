# 前端部署与使用

前端机器需要两个独立 checkout：

- Handoff Engine：安装 CLI、skills 和可选 DSH 插件。
- 私有 Handoff Store：保存团队配置、任务、事件、契约与附件。

安装步骤见 [`DEPLOY-AGENT.md`](DEPLOY-AGENT.md)。以下示例假设：

```bash
export HANDOFF_ENGINE_ROOT=~/code/handoff-engine
export HANDOFF_ROOT=~/code/handoff-store
export PATH="$HANDOFF_ENGINE_ROOT/.venv/bin:$PATH"
```

## 本机配置

```bash
cp "$HANDOFF_ROOT/config/local.yaml.example" "$HANDOFF_ROOT/config/local.yaml"
```

在 `local.yaml` 中填写本机业务仓路径、环境覆盖、执行器位置和密钥文件路径。该文件必须被交接仓 `.gitignore` 排除。

前端机器通常只需要源业务仓权限和私有交接仓权限；不需要后端业务仓写权限。

## 自检

```bash
handoffctl --root "$HANDOFF_ROOT" doctor --role frontend
handoffctl --root "$HANDOFF_ROOT" validate
```

## 创建任务

```bash
handoffctl --root "$HANDOFF_ROOT" create \
  --from-file /tmp/handoff-draft.json \
  --source-path ~/code/web \
  --contract /path/to/openapi.yaml
```

`create` 会：

1. 从源业务仓读取完整 commit SHA 与仓库标识；
2. 校验 scope、迁移信息和结构化验收检查；
3. 把契约/附件复制进交接仓并记录 SHA-256；
4. 原子写入任务与审计事件，commit 并 push；
5. 在配置通知时唤醒后端 worker。

不要直接编辑 `tasks/*.json`，也不要让任务携带任意 shell 命令。

## 查询和验收

```bash
handoffctl --root "$HANDOFF_ROOT" show <task-id>
handoffctl --root "$HANDOFF_ROOT" events <task-id>
handoffctl --root "$HANDOFF_ROOT" deliver <task-id> --json
```

`deliver` 只有在部署版本包含目标 merge SHA、验收期间环境未漂移且全部结构化检查通过时才写 `verified`。前端人工联调完成后：

```bash
handoffctl --root "$HANDOFF_ROOT" accept <task-id> --reason '前端验收通过'
```

## 会话恢复

创建任务时，CLI 使用 `CODEX_THREAD_ID` 或 `DSH_SESSION_ID` 登记原会话；普通终端需显式传 `--source-thread`。完成事件由 `frontend-listener` 恢复原会话：

```bash
handoffctl --root "$HANDOFF_ROOT" frontend-listener
handoffctl --root "$HANDOFF_ROOT" notifier
```

这些常驻进程应从引擎虚拟环境启动，但 `--root` 始终指向私有交接仓。

可在私有仓的 `config/local.yaml` 中为恢复动作指定模型，两项均为可选：

```yaml
notifications:
  restore_models:
    codex: gpt-5.3-codex
    dsh:
      provider: deepseek-account
      model: deepseek-chat
```

省略 `codex` / `dsh`（或设为 `null`）时不会切换模型，由恢复会话沿用自己的选择；配置后则在每次恢复、投递完成消息之前切换。DSH 必须同时给出 `provider` 和 `model`，因为模型 id 在不同 provider 下可能重名。DSH 使用宿主官方 `selectModel` 接口，该接口还会把成功选择保存为当前 profile 的新会话默认模型。

## Skills

`handoff` / `deliver` skill 从 `$HANDOFF_ENGINE_ROOT/.agents/skills/` 安装。skill 通过 `HANDOFF_ROOT` 找数据仓并调用 PATH 中的 `handoffctl`，不会把引擎目录当成 store。

## 更新

```bash
git -C "$HANDOFF_ENGINE_ROOT" pull --ff-only
"$HANDOFF_ENGINE_ROOT/.venv/bin/pip" install -e "$HANDOFF_ENGINE_ROOT"
git -C "$HANDOFF_ROOT" pull --ff-only
```

两次更新应分开执行：引擎代码可独立发布，交接数据只按团队 Git 流程同步。
