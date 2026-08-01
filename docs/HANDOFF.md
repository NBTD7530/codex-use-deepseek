# Codex Model Router 交接摘要

> 生成时间：2026-08-01。本文档汇总会话背景、实现、验证结果与下一步，供后续会话快速续接。

## 1. 目标

在 Codex Desktop 模型下拉菜单中加入 DeepSeek V4 Flash 与 V4 Pro：GPT 模型走 ChatGPT 登录订阅，DeepSeek 模型走 API Key，同一下拉菜单自动切换，弃用 CC Switch。

## 2. 实现方案（方案一）

- 本地路由服务监听 `127.0.0.1:17890`，按请求中的 `model` 字段分流：
  - `gpt-*` → `chatgpt.com/backend-api/codex`（保留 Codex 登录态）
  - `deepseek-v4-*` → `api.deepseek.com`（剥离 OpenAI 身份头，注入 Keychain Key）
- DeepSeek API Key 存于 macOS 登录钥匙串：服务 `codex-model-router.deepseek`，账号为当前登录用户名；配置文件无明文。
- LaunchAgent（`com.codex.model-router`）随登录启动、异常自动重启，仅监听回环地址。
- 模型目录 `~/.codex/models-router.json` 合并官方 GPT 条目与两个 DeepSeek 条目。

## 3. 当前状态

- 项目仓库：`/Users/wangshizhen.7530/work/codex-model-router`
- 分支 `main`，无远程，工作区干净
- 37 项测试全部通过
- 健康检查 `{"status":"ok"}`；GPT 实际请求 200；DeepSeek Flash 实际请求 200；Pro 被上游暂时拒绝（官方提示 2026 年 8 月上旬开放）
- 本机 gh 2.97.0 已安装并登录（账号 `NBTD7530`，HTTPS）

## 4. 开源准备已完成

- Apache License 2.0（官方规范全文）
- 真实 `uninstall` 命令（恢复备份配置、卸载并删除 LaunchAgent、清理路由与日志，保留钥匙串与备份）+ 测试
- GitHub Actions CI（macOS，Python 3.9/3.11）
- 文档：README、PROJECT.md、REUSE.md、docs/design 设计规格；内部 plans 已移除

## 5. 已知问题

1. `~/.codex/config.toml` 中 `[model_providers.local_router]` 与生效的 `custom` 段内容重复，属安装期冗余，当前不生效。
2. DeepSeek V4 Pro 需等待上游开放后才可实际使用。
3. 修改模型目录或配置后，需完整退出并重新启动 Codex Desktop（⌘Q）才会刷新模型列表。

## 6. 下一步

推送到 GitHub（需先确认仓库可见性）：

```bash
cd /Users/wangshizhen.7530/work/codex-model-router
gh repo create codex-model-router --public --source . --push
```

可选后续项：清理 `local_router` 冗余配置段、补充 CHANGELOG、完善 README 英文版。
