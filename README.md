# Github_Repo_Tracker

一个 AstrBot 插件，用于监听一个或多个 GitHub 仓库的 Commit 与 Pull Request 变化，并推送到指定会话。支持公开仓库与私有仓库（通过 Fine-grained personal access token）。

要求 AstrBot >= v4.10.4（依赖 `template_list` 配置类型）。

## 功能

- 按仓库单独配置：是否私有、专属 Token、监听分支、是否监听 Commit / Pull Request、推送目标会话。
- 每个仓库的更新只推送给该仓库自己配置的会话，不会广播给其他仓库的订阅者。
- 公开仓库可不填 Token（使用全局 Token 或匿名请求）；私有仓库必须填写具备该仓库读权限的 Fine-grained PAT。

## 配置

在 AstrBot 管理面板中找到本插件，进入配置：

- `poll_interval_minutes`：轮询间隔（分钟），所有仓库共用。
- `global_token`：全局默认 Token（可选），仅用于未单独配置 Token 的公开仓库，不会自动用于私有仓库。
- `repos`：仓库列表，每项包含：
  - `full_name`：仓库全名，如 `AstrBotDevs/AstrBot`
  - `is_private`：是否私有仓库
  - `token`：该仓库专属 Token（私有仓库必填）
  - `branch`：监听分支（留空使用默认分支）
  - `watch_commits` / `watch_pull_requests`：是否监听对应类型的变化
  - `target_sessions`：推送目标会话列表（`unified_msg_origin`）

## 指令

| 指令 | 说明 |
| --- | --- |
| `/ght whoami` | 返回当前会话的 `unified_msg_origin`，可手动复制填入配置 |
| `/ght bind <owner/repo>` | 将当前会话绑定为该仓库的推送目标（写回配置并持久化，管理面板同步可见） |
| `/ght unbind <owner/repo>` | 将当前会话从该仓库的推送目标中移除 |
| `/ght commits <owner/repo>` | 查询该仓库（须已在配置列表中）最新一条 Commit |
| `/ght prs <owner/repo>` | 查询该仓库（须已在配置列表中）最新更新的一个 Pull Request |

`bind`/`unbind`/`commits`/`prs` 均要求目标仓库已存在于 `repos` 配置列表中；未配置的仓库请先在管理面板中添加（可只填 `full_name`，`target_sessions` 留空，再用 `/ght bind` 绑定会话）。

## 安全提示

- Token 会以明文形式显示在管理面板配置中，请仅授予 Fine-grained PAT 所需的最小只读权限（Contents 和 Pull requests 的读权限），并妥善控制管理面板的访问权限。
