# 后端升级：仓库 scope 模板

这次升级把 `allowed_paths` / `forbidden_paths` 的维护权从前端任务草稿移到后端仓库配置。新任务创建时解析模板，并把最终路径固化到任务 JSON；已有任务不变。

## 1. 拉取并安装新版引擎

在后端实际运行 Handoff Engine 的代码目录执行（路径按机器实际情况替换）：

```powershell
cd C:\handoff\app
git pull --ff-only
.\.venv-win\Scripts\python.exe -m pip install -e .
```

如果后端安装的是 wheel，而不是 editable checkout，请按现有发布方式重新安装新版 wheel；不要只拉源码而继续运行旧 site-packages。

## 2. 配置目标后端仓库

编辑交接仓 `config/repositories.yaml`，在 `lunaXtech/dropspot-api` 条目下增加：

```yaml
repositories:
  - id: lunaXtech/dropspot-api
    role: backend
    # 保留现有 host/default_branch/remote/cache_dir 等字段
    scope:
      default_profile: application
      forbidden_paths:
        - ".github/**"
        - "infra/prod/**"
        - "**/auth/**"
      profiles:
        application:
          allowed_paths:
            - "router/**"
            - "controller/**"
            - "service/**"
            - "model/**"
            - "internal/**"
            - "pkg/**"
            - "openapi/**"
            - "tests/**"
            - "docs/**"
        migration:
          allowed_paths:
            - "router/**"
            - "controller/**"
            - "service/**"
            - "model/**"
            - "internal/**"
            - "pkg/**"
            - "openapi/**"
            - "tests/**"
            - "docs/**"
            - "migrations/**"
```

规则：

- `default_profile` 必须存在于 `profiles`。
- 每个 profile 的 `allowed_paths` 至少一项，不能用空数组。
- 仓库级 `forbidden_paths` 对所有 profile 生效，profile 无法移除。
- 不要加入 `"**"`。
- `migrations/**` 不放进普通 `application` profile；`migration` profile 包含普通代码范围再额外加入迁移目录，需要迁移时显式选择它。
- `gate.high_risk_paths` 只控制是否允许自动合并，不能替代 `forbidden_paths`。

修改后提交并推送交接仓：

```powershell
cd C:\handoff\store
git add config\repositories.yaml
git commit -m "config: add backend scope profiles"
git push
```

## 3. 校验配置

```powershell
C:\handoff\app\.venv-win\Scripts\handoffctl.exe --root C:\handoff\store doctor --role backend
C:\handoff\app\.venv-win\Scripts\handoffctl.exe --root C:\handoff\store validate
```

配置缺失、默认 profile 不存在、glob 非法或 `allowed_paths` 为空时必须先修正；新版不会回退到全仓 `**`。

## 4. 重启运行进程

按后端当前部署方式重启 worker 和 panel，确保它们加载新代码与新配置。计划任务示例：

```powershell
Stop-ScheduledTask -TaskName HandoffWorker
Start-ScheduledTask -TaskName HandoffWorker
```

如果 panel 是独立进程，也要一并重启。浏览器重新打开或强制刷新面板，确认新建任务界面显示“后端路径模板”，不再显示可编辑的 `allowed_paths` / `forbidden_paths`。

## 5. 验证行为

新建一张测试任务并确认：

1. 前端只选择 `scope_profile`、填写需求和 `out_of_scope`。
2. 落库任务的 `scope.allowed_paths` 来自所选 profile。
3. 落库任务的 `scope.forbidden_paths` 包含仓库级禁区。
4. 删除仓库 `scope` 后，新建任务会明确失败，而不是生成 `allowed_paths: ["**"]`。

## 兼容性说明

- 已存在的任务继续使用各自任务 JSON 中已经固化的 scope，不会自动重算。
- 旧任务不需要迁移。
- 旧版任务草稿中的 raw `allowed_paths` / `forbidden_paths` 在新版 CLI 创建时不再决定路径；最终以目标仓库 profile 为准。
- 旧版面板/API 若继续提交 raw 路径会收到明确错误，升级后需使用新版面板。
