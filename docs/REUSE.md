# 复用说明

> 面向其他人的交付文档。按本说明即可在一台新的 macOS 上部署整套 Codex 双模型路由。

## 1. 前置条件

- macOS（Apple Silicon 或 Intel 均可）
- 已安装 Codex Desktop，且已用 ChatGPT 账号登录
- 一个有效的 DeepSeek API Key
- 本机端口 `17890` 空闲
- 无需安装 CC Switch，无需额外 Python 依赖（使用系统自带 `/usr/bin/python3`）

## 2. 文件清单

部署方需要提供整个仓库目录：

```text
codex-model-router/
├── scripts/
│   ├── install.sh
│   ├── rollback.sh
│   └── uninstall.sh
├── src/
│   ├── codex_model_router.py
│   └── installer.py
├── config/deepseek-models.json
├── docs/
│   ├── PROJECT.md
│   └── REUSE.md
└── README.md
```

## 3. 安装步骤

在项目目录执行：

```sh
./scripts/install.sh
```

安装器会自动完成：

1. 检查 `127.0.0.1:17890` 是否可用。
2. 备份现有 `config.toml`、模型目录和 LaunchAgent 状态到 `~/.codex/model-router/backups/<时间戳>/`。
3. 从旧配置迁移 DeepSeek Key 到钥匙串（服务 `codex-model-router.deepseek`，账号为当前登录用户名），或复用已有钥匙串条目。
4. 生成混合模型目录 `~/.codex/models-router.json`。
5. 改写 `config.toml` 指向本地路由并移除明文 Key。
6. 安装 LaunchAgent 并启动。

如果钥匙串中没有 Key，安装器会提示输入 DeepSeek API Key 两次（输入时屏幕不显示字符，属正常现象）。

## 4. 验证

```sh
# 路由健康
curl --fail --silent http://127.0.0.1:17890/health
# 预期：{"status":"ok"}

# 启动代理状态
launchctl print "gui/$(id -u)/com.codex.model-router"

# ChatGPT 登录态
/Applications/ChatGPT.app/Contents/Resources/codex login status
# 预期：Logged in using ChatGPT
```

然后完整退出并重新打开 Codex Desktop（⌘Q，再启动），模型下拉菜单中应出现：

- `DeepSeek-V4-Flash`
- `DeepSeek-V4-Pro`

## 5. 更新路由后重装

```sh
launchctl bootout "gui/$(id -u)/com.codex.model-router"
./scripts/install.sh
```

## 6. 回滚

恢复最近一次备份：

```sh
./scripts/rollback.sh
```

恢复指定备份：

```sh
./scripts/rollback.sh ~/.codex/model-router/backups/<时间戳>
```

回滚会恢复备份的配置与模型目录，但不会删除钥匙串条目和已安装源码。

## 7. 卸载

```sh
./scripts/uninstall.sh
```

卸载会：

1. 恢复最近一次备份的 Codex 配置与模型目录。
2. 卸载并删除 LaunchAgent。
3. 删除已安装的路由脚本与日志。

钥匙串条目和备份目录会保留。如需连 Key 一起删除，手动执行：

```sh
security delete-generic-password \
  -a "$(id -un)" \
  -s "codex-model-router.deepseek"
```

## 8. 常见问题

**模型列表里没有 DeepSeek？**
完整退出 Codex Desktop（⌘Q）后重新打开；界面进程需要重启才会重新读取模型目录。

**Pro 报错/不可用？**
DeepSeek 官方目前仅向 Codex 开放 Flash，Pro 的 Codex 集成预计 2026 年 8 月上旬开放。

**改了系统代理后请求失败？**
路由在启动时读取 macOS 系统 HTTPS 代理配置，改代理后执行：

```sh
launchctl kickstart -k "gui/$(id -u)/com.codex.model-router"
```

**端口 17890 被占用？**
安装器会直接报错；先释放端口，或确认是否已有旧实例在运行。

## 9. 安全说明

- OpenAI 鉴权只转发给 `chatgpt.com` 的 TLS 连接。
- DeepSeek 请求会剥离 OpenAI 身份与账号相关请求头，再注入钥匙串中的 Key。
- 日志只记录模型、路由、状态码与耗时，不记录请求内容与凭据。
- API Key 只在当前用户登录钥匙串中，不写入配置文件或环境变量。
