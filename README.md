# QQ 名片点赞插件

同时兼容 NapCat 与 SnowLuma 适配器给 QQ 好友名片点赞，支持命令触发和 AI 口语化触发两种方式。

## 功能

- **命令触发**：`/赞 @某人`、回复消息发 `/赞`、`/赞 123456789`
- **口语化触发**：群里说“麦麦给我点个赞”、“帮我给某某点赞”，AI 自动识别并执行
- **安全保护**：每日限额、冷却时间、管理员权限控制

## 安装方式

1. 将本插件目录放入 MaiBot 的 `plugins/` 文件夹下
2. 确保已安装并启用 **NapCat 或 SnowLuma** 其中一种适配器。MaiBot 1.3.0 环境推荐使用新版 SnowLuma 统一连接器；旧环境仍可使用 NapCat。
3. 重启 MaiBot
4. 在 WebUI `http://127.0.0.1:8001` 插件管理中确认已启用

## 配置说明

配置文件由 Runner 自动生成在插件目录下（`config.toml`）。

### [plugin] 插件基础

| 字段 | 默认值 | 说明 |
|------|--------|------|
| enabled | true | 是否启用插件 |
| config_version | "0.2.8" | 配置版本（请勿修改） |

### [like] 点赞设置

| 字段 | 默认值 | 说明 |
|------|--------|------|
| default_times | 10 | 默认点赞次数 |
| max_times | 20 | 单次最大点赞次数 |
| daily_limit_per_target | 30 | 每目标每日上限 |
| cooldown_seconds | 30 | 命令冷却秒数（0=不限制） |
| allow_at_target | true | 允许通过 @某人 指定目标 |
| allow_reply_target | true | 允许通过回复消息指定目标 |
| allow_raw_qq | true | 允许直接输入 QQ 号 |
| enable_llm_tool | true | 启用 AI 口语化触发 |

### [admin] 权限

| 字段 | 默认值 | 说明 |
|------|--------|------|
| admin_only | true | 是否仅管理员可用（默认开启，**必须至少填入一个管理员 QQ 号，否则任何人都无法使用**） |
| admins | "" | 管理员 QQ 号，逗号分隔，例如：`123456789, 987654321` |

## 使用示例

- `/赞 @张三` → 给张三点 10 个赞
- `/赞 @张三 5` → 给张三点 5 个赞
- 回复某人消息发 `/赞` → 给被回复者点赞
- `/赞 123456789` → 给指定 QQ 号点赞
- 群里说“麦麦给我点个赞” → AI 自动触发

## 适配器兼容性

- 同一份插件代码兼容 `maibot-team.napcat-adapter` 与 `maibot-team.snowluma-adapter`，无需安装两种适配器。
- 插件会尝试调用 `adapter.snowluma.*` 和 `adapter.napcat.*` 下的登录信息及点赞 API，并记住成功使用的命名空间。
- 只有 API 调用层抛出异常时才切换到另一个命名空间；如果 API 已返回业务失败，不会再次调用另一个接口，避免重复点赞。
- 插件清单不强制依赖某一种适配器；请自行安装并启用其中一种兼容的适配器。

## 依赖

- MaiBot >= 1.2.0
- maibot-plugin-sdk >= 2.5.0
- `maibot-team.napcat-adapter` 或 `maibot-team.snowluma-adapter`（二选一）

## 许可证

MIT License