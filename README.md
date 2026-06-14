# AstrBot LeetCode 每日一题插件

一个用于 AstrBot 的 LeetCode 题目推送插件，支持群聊订阅、个人私信订阅、每日一题、随机刷题、题号查询、AI 解题分析、多语言显示和图片推送。

## 功能特性

- 每日一题推送：按配置时间向已订阅群聊或个人用户推送 LeetCode 每日一题。
- 个人订阅：用户可在私聊中订阅每日一题，并单独设置推送时间和显示语言。
- 随机刷题：支持 `/lc随机`，当今日题目已做过或想再刷一题时可随机获取一道题。
- 题目查询：支持按题号查询指定 LeetCode 题目。
- AI 解题：接入 AstrBot LLM，提供题目理解、思路分析、复杂度和参考代码。
- 多语言显示：支持中文、英文、双语显示。
- 图片推送：可开启文转图推送，图片发送失败时自动回退文本。
- 本地缓存：每日一题当天缓存，避免重复请求。

## 数据来源

每日一题优先使用 LeetCode CN 官方 GraphQL：

```text
https://leetcode.cn/graphql/
```

该接口可直接返回中英文标题、中英文题干、难度、标签和通过率。只有 LeetCode CN 请求失败时，才会回退到旧备用接口：

```text
https://leetcode-api-pied.vercel.app/daily
```

指定题号查询当前仍会使用：

```text
https://lcid.cc/info/{question_id}
https://leetcode.com/graphql
```

语言设置只影响展示方式，不会决定每日一题接口来源。

## 安装

将插件放入 AstrBot 插件目录后重启 AstrBot。

示例：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/NumInvis/astrbot_plugin_leetcode.git
```

如果你使用 fork 或 PR 分支，请将对应分支代码放入插件目录。

## 配置项

在 AstrBot 插件配置面板中配置：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `admin_users` | list | `[]` | 管理员用户 ID 列表。群聊命令需要管理员权限。 |
| `group_inform_hour` | int | `9` | 群订阅每日推送小时，范围 `0-23`。 |
| `group_inform_minute` | int | `0` | 群订阅每日推送分钟，范围 `0-59`。 |
| `check_interval_seconds` | int | `30` | 群订阅推送检查间隔，范围 `5-3600` 秒。 |
| `default_language` | string | `zh` | 默认显示语言，可选 `zh`、`en`、`both`。 |
| `enable_personal_subscribe` | bool | `true` | 是否开启个人私信订阅。 |
| `personal_inform_hour` | int | `9` | 个人订阅默认推送小时，范围 `0-23`。 |
| `personal_inform_minute` | int | `30` | 个人订阅默认推送分钟，范围 `0-59`。 |
| `enable_llm_translation` | bool | `true` | 备用接口或题号查询需要翻译时是否使用 LLM。 |
| `translation_provider_id` | string | `""` | 指定翻译用 LLM 提供商 ID，留空则使用 AstrBot 默认提供商。 |
| `enable_image_push` | bool | `false` | 开启后推送时优先使用文转图，失败自动回退文本。 |

配置会做基本校验，时间和间隔超出范围时会回退默认值，避免插件因错误配置启动失败。

## 命令列表

### 基础命令

| 命令 | 说明 |
| --- | --- |
| `/lc菜单` | 显示简版命令菜单。 |
| `/lc帮助` | 显示完整帮助。 |
| `/lc今日` | 获取今日 LeetCode 每日一题，使用当天缓存。 |
| `/lc随机` | 随机获取一道题，不使用今日题目缓存。 |
| `/lc题目 [题号]` | 查询指定题号的题目，例如 `/lc题目 1`。 |
| `/lc解题 [题号]` | 使用 AI 分析并解答题目，例如 `/lc解题 1`。 |

### 群聊订阅命令

群聊命令需要管理员权限。

| 命令 | 说明 |
| --- | --- |
| `/lc订阅` | 在当前群订阅每日一题推送。 |
| `/lc退订` | 在当前群取消每日一题推送。 |
| `/lc列表` | 查看当前群订阅状态。 |
| `/lc全部订阅` | 查看所有群订阅。 |

### 个人订阅命令

个人命令建议在私聊中使用。

| 命令 | 说明 |
| --- | --- |
| `/lc订阅我` | 订阅每日一题私信推送。 |
| `/lc退订我` | 取消个人订阅。 |
| `/lc我的状态` | 查看个人订阅状态、推送时间和语言设置。 |
| `/lc时间` | 查看当前个人推送时间。 |
| `/lc时间 HH:MM` | 设置个人推送时间，例如 `/lc时间 8:00`。 |
| `/lc时间 默认` | 恢复使用配置中的默认个人推送时间。 |
| `/lc语言 zh` | 设置仅中文显示。 |
| `/lc语言 en` | 设置仅英文显示。 |
| `/lc语言 both` | 设置双语显示。 |
| `/lc测试推送` | 私聊中立即给自己发送一次每日一题，用于测试私信推送通道。 |

### 管理员命令

| 命令 | 说明 |
| --- | --- |
| `/lc全部个人订阅` | 查看所有个人订阅用户。 |
| `/lc测试订阅 [用户ID] [--add]` | 测试给指定用户发送个人订阅推送；`--add` 可临时补充订阅记录。 |

## 使用示例

获取今日题目：

```text
/lc今日
```

随机再刷一题：

```text
/lc随机
```

查询两数之和：

```text
/lc题目 1
```

订阅个人每日推送：

```text
/lc订阅我
```

设置个人推送时间：

```text
/lc时间 8:00
```

设置双语显示：

```text
/lc语言 both
```

## 关于今日缓存和随机题

`/lc今日` 会使用当天缓存：

```text
同一天内重复执行 /lc今日，会优先返回同一道每日一题。
```

`/lc随机` 不使用今日缓存：

```text
每次都会重新请求题库并随机抽题，适合今日题目已经做过、会了，或者想额外刷一题的场景。
```

## 关于图片推送

开启：

```json
{
  "enable_image_push": true
}
```

开启后，插件会在以下场景优先尝试文转图：

- `/lc今日`
- `/lc随机`
- 群订阅推送
- 个人订阅推送
- `/lc测试推送`
- `/lc测试订阅`

如果图片生成或发送失败，会自动回退文本。

注意：当前会话回复图片和主动私信图片不是同一条发送链路。某些 QQ/NTQQ 协议端可能在主动私信发图时报：

```text
rich media transfer failed
```

这种情况下插件会降级发送文本。群聊发图正常并不一定代表主动私信发图也一定正常。

## 关于中文内容和 LLM 翻译

每日一题现在优先从 LeetCode CN 官方 GraphQL 获取中文题干和中文标题，正常情况下不需要 LLM 翻译。

以下场景可能仍会使用 LLM：

- LeetCode CN 每日一题接口失败后使用备用接口。
- 指定题号查询只拿到英文内容，需要生成中文显示。
- AI 解题分析本身需要 AstrBot 已配置 LLM。

如果未配置 LLM 或翻译失败，插件会尽量回退显示英文内容。

## 已知限制

- 主动私信推送依赖 AstrBot 主动消息能力和具体平台协议端支持。
- 图片推送在部分 QQ/NTQQ 主动私信场景可能失败，并自动回退文本。
- `/lc题目` 当前仍依赖 `lcid.cc` 和英文 LeetCode GraphQL，后续可进一步改为 LeetCode CN 查询。

## 更新记录

### 当前版本改动

- 每日一题优先使用 LeetCode CN 官方 GraphQL，减少对 Vercel 备用接口的依赖。
- 新增 `/lc随机` 命令，用于随机获取一道非每日缓存题目。
- 修复 LeetCode 示例代码块在图片渲染中显示为空的问题。
- 修复 LeetCode CN 中文标签字段识别问题。
- 统一 `enable_image_push` 在群推送、个人推送和测试推送中的行为。
- 修复私信图片路径中的 `file:` 前缀处理问题。
- 图片推送失败时自动降级文本，避免测试推送或个人推送完全失败。

## 致谢

- 原项目灵感：[nonebot-plugin-leetcode](https://github.com/zxz0415/leetcode)
- 框架支持：[AstrBot](https://github.com/Soulter/AstrBot)
- 数据来源：[LeetCode](https://leetcode.com/) / [LeetCode CN](https://leetcode.cn/)

## License

MIT License
