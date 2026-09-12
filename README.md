# QQ 名片点赞插件

通过 NapCat 适配器给 QQ 好友名片点赞，支持命令触发和 AI 口语化触发两种方式。

## 功能

- **命令触发**：`/赞 @某人`、回复消息发 `/赞`、`/赞 123456789`
- **口语化触发**：群里说“麦麦给我点个赞”、“帮我给某某点赞”，AI 自动识别并执行
- **安全保护**：每日限额、冷却时间、管理员权限控制

## 安装方式

1. 将本插件目录放入 MaiBot 的 `plugins/` 文件夹下
2. 确保已安装并启用 `maibot-team.napcat-adapter` 适配器插件
3. 重启 MaiBot
4. 在 WebUI `http://127.0.0.1:8001` 插件管理中确认已启用

## 配置说明

配置文件由 Runner 自动生成在插件目录下（`config.toml`）。

### [plugin] 插件基础

| 字段 | 默认值 | 说明 |
|------|--------|------|
| enabled | true | 是否启用插件 |
| config_version | "0.2.0" | 配置版本（请勿修改） |

### [like] 点赞设置

| 字段 | 默认值 | 说明 |
|------|--------|------|
| default_times | 10 | 默认点赞次数 |
| max_times | 10 | 单次最大点赞次数 |
| daily_limit_per_target | 10 | 每目标每日上限 |
| cooldown_seconds | 30 | 命令冷却秒数（0=不限制） |
| allow_at_target | true | 允许通过 @某人 指定目标 |
| allow_reply_target | true | 允许通过回复消息指定目标 |
| allow_raw_qq | true | 允许直接输入 QQ 号 |
| enable_llm_tool | true | 启用 AI 口语化触发 |

### [admin] 权限

| 字段 | 默认值 | 说明 |
|------|--------|------|
| admin_only | false | 是否仅管理员可用 |
| admins | "" | 管理员 QQ 号，逗号分隔 |

## 使用示例

- `/赞 @张三` → 给张三点 10 个赞
- `/赞 @张三 5` → 给张三点 5 个赞
- 回复某人消息发 `/赞` → 给被回复者点赞
- `/赞 123456789` → 给指定 QQ 号点赞
- 群里说“麦麦给我点个赞” → AI 自动触发

## 依赖

- MaiBot >= 1.0.0
- maibot-plugin-sdk >= 2.5.0
- `maibot-team.napcat-adapter`

## 许可证

MIT License