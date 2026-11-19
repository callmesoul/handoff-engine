# Handoff Store Template

这是 Handoff Engine 的独立交接仓模板。复制到一个**私有 Git 仓库**后使用；不要把真实任务数据提交回引擎仓。

1. 修改 `config/repositories.yaml`、`environments.yaml`、`checks.yaml` 和 `admin.yaml`。
2. `cp config/local.yaml.example config/local.yaml`，写入机器专属路径；该文件不会进 Git。
3. 初始化并推送私有仓库。
4. 安装 Handoff Engine，运行 `handoffctl --root /path/to/store doctor`。

`tasks/`、`events/`、`contracts/`、`artifacts/` 由 `handoffctl` 管理，不要手工编辑。
