# Codex Model Router 项目总览

> 本文档由 2026-07-31 的一次 Codex 会话整理而成，记录需求、决策、实现与验证结果，作为项目交付依据。

## 1. 背景与目标

用户希望在 Codex Desktop 的模型下拉菜单中加入 DeepSeek V4 Flash 与 DeepSeek V4 Pro，同时满足：

- 切换至 GPT 模型时使用登录态订阅（ChatGPT 登录），不改动官方鉴权。
- 使用 DeepSeek 模型时自动走 API Key。
- 两种认证方式在同一个下拉菜单中自动切换，不接受两个独立入口或 Profile。
- 弃用 CC Switch。

## 2. 关键决策

| 决策点 | 结论 |
| --- | --- |
| 方案 | 方案一：本地转发服务 + 同一下拉菜单自动切换 |
| 接入协议 | DeepSeek 原生支持 OpenAI Responses 协议（见 DeepSeek 官方 API 文档汇总） |
| API Key 存放 | macOS 登录钥匙串（服务 `codex-model-router.deepseek`），不写入配置文件明文 |
| 转发端口 | `127.0.0.1:17890`，仅监听回环地址，与 CC Switch 无冲突 |
| 启动方式 | LaunchAgent：`RunAtLoad` + `KeepAlive`，随登录启动、异常自动重启 |
| 模型目录 | `~/.codex/models-router.json`：官方 GPT 条目 + 两个 DeepSeek 条目合并 |
| 鉴权隔离 | OpenAI 请求保留 Codex 登录态；DeepSeek 请求剥离 OpenAI 身份头后注入 Keychain Key |

## 3. 运行架构

```text
Codex Desktop
  (model_catalog_json = ~/.codex/models-router.json)
       │ Responses 请求
       ▼
本地路由  http://127.0.0.1:17890
  ├─ gpt-*            → OpenAI 上游（保留 ChatGPT 登录态）
  └─ deepseek-flash / deepseek-v4-pro → api.deepseek.com（注入 Keychain API Key）
```

路由只放行模型目录中登记的模型；未知模型、缺少 Codex 鉴权头、缺少 DeepSeek Key 等情况均在本地直接拒绝，不会打到上游。

## 4. 组件清单

| 组件 | 位置 |
| --- | --- |
| 本地路由 | `~/.codex/model-router/codex_model_router.py` |
| 安装/迁移/回滚 | 仓库 `src/installer.py` |
| 混合模型目录 | `~/.codex/models-router.json` |
| LaunchAgent | `~/Library/LaunchAgents/com.codex.model-router.plist` |
| Keychain 条目 | 服务 `codex-model-router.deepseek`，账号为当前登录用户名 |
| 请求日志 | `~/.codex/model-router/logs/` |
| 配置备份 | `~/.codex/model-router/backups/<时间戳>/` |

## 5. 验证结果（2026-08-01）

- 单元测试：35 项全部通过（`/usr/bin/python3 -m unittest discover -s tests -v`）。
- 健康检查：`GET http://127.0.0.1:17890/health` 返回 `{"status":"ok"}`。
- LaunchAgent：`running`，PID 正常，无异常退出。
- Codex 登录态：`Logged in using ChatGPT`。
- Keychain：服务/账号匹配，Key 存在且可读，`config.toml` 无明文 Key。
- GPT 请求：经路由转发 HTTP 200。
- DeepSeek Flash：经路由转发 HTTP 200，Key 有效。
- DeepSeek V4 Pro：上游返回 HTTP 400，官方提示 Codex 集成将于 2026 年 8 月上旬开放，当前请使用 Flash。
- 模型列表：新启动的 app-server `model/list` 已返回 `deepseek-flash` 与 `deepseek-v4-pro`。

## 6. 当前状态与已知问题

1. `scripts/uninstall.sh` 已实现真正卸载：恢复备份配置、卸载并删除 LaunchAgent、清理已安装脚本与日志；钥匙串条目和备份目录按设计保留。
2. `~/.codex/config.toml` 中 `[model_providers.local_router]` 与生效的 `[model_providers.custom]` 内容重复，属于安装期冗余段，当前不生效。
3. 修改模型目录或配置后，需要完整退出并重新启动 Codex Desktop（⌘Q）才会刷新界面模型列表。
4. DeepSeek V4 Pro 需要等待上游开放后才可实际使用。

## 7. 仓库结构

```text
codex-model-router/
├── README.md
├── config/deepseek-models.json
├── docs/
│   ├── PROJECT.md      # 本文件：会话转项目总览
│   ├── REUSE.md        # 复用/交付说明
│   └── design/         # 设计规格与验收标准
├── scripts/
│   ├── install.sh
│   ├── rollback.sh
│   └── uninstall.sh
├── src/
│   ├── codex_model_router.py
│   └── installer.py
└── tests/
    ├── test_installer.py
    └── test_router.py
```

## 8. 变更记录

### 2026-09-17（来源：`发布 DeepSeek` 会话）

| 改造点 | 内容 | 落点 |
| --- | --- | --- |
| 模型改名 | slug `deepseek-v4-flash` → `deepseek-flash`，显示名 `DeepSeek-V4-Flash` → `DeepSeek-Flash`，`deepseek-v4-pro` 不变 | 模型目录模板、路由白名单、README/文档 |
| 图片输入 | Flash 条目 `input_modalities` 增加 `image`，`supports_image_detail_original` 置为 `true`；Pro 仍为纯文本 | `config/deepseek-models.json` |
| 工具结果兼容 | 请求中 `call_id` 非字符串的 `function_call_output`（孤儿工具结果）在发往 DeepSeek 前改写为普通用户文本，其余请求体原样转发 | `src/codex_model_router.py` 的 `normalize_deepseek_payload()` |
| 旧名迁移 | 重新安装时合并目录会丢弃历史遗留的 `deepseek-v4-flash` 条目，避免新旧两个 slug 并存 | `src/installer.py` 的 `merge_catalog()` |

本机复验（2026-09-17）：目录中只有 `deepseek-flash` 与 `deepseek-v4-pro`；以 `deepseek-flash` 发出的真实 Responses 请求返回 HTTP 200，响应 `model` 为 `deepseek-flash`；健康检查 `{"status":"ok"}`。

排障提示：`launchctl kickstart` 返回时服务可能尚未完成端口绑定，属启动时序竞争；判断服务状态应以 `/health` 返回 `{"status":"ok"}` 为准。
