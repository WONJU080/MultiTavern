# 剧情卡 / 口述剧情 → MultiTavern 导入配置转换指令（不含角色卡）

**用法**：把本文件整份发给 AI，再附上你的素材——SillyTavern 剧情卡（JSON/PNG 导出的文本）、世界书、预设，或一段口头叙述的剧情概要。AI 会输出一个可直接导入 MultiTavern 的 JSON 配置。把生成的 JSON 存成 `.json` 文件，在建房页点 **Load config file**（导入配置）加载即可。

**适用范围**：本指令只转换**剧情、世界观、主持指令与生成预设**，**不转换、不输出角色卡**（不出现 `characters`、`host_character` 字段）。角色由房主在建房表单的「角色池」另行创建，玩家进房后自行认领。

---

## 一、给 AI 的任务

你是 MultiTavern（多人 AI 跑团）的素材改编助手。请把用户提供的素材改写成**一个合法的 JSON 对象**，它能被 MultiTavern 建房页的「Load config file / 导入配置」直接读取。硬性要求：

1. 只转换剧情与预设，**绝对不要输出 `characters` 或 `host_character` 字段**。素材里出现的角色卡内容（角色名、description、personality、mes_example 等）一律跳过；剧情关键的 NPC 请用文字写进 `scenario` 或 `lorebook`，而不是角色卡。
2. 只输出 JSON 本身：不要 markdown 代码围栏，不要注释，不要在 JSON 前后写任何解释文字。
3. **按素材内容决定包含哪些可选字段**，不必把下面所有字段都填上。没有对应内容的字段直接省略；不要凑数，不要发明素材中不存在的重要设定。
4. 文本语言与素材保持一致，专有名词保留原文。

---

## 二、输出结构总览（字段按需选用）

```jsonc
// 以下注释仅供阅读，真实输出必须是纯 JSON、不能带注释
{
  "scenario": "……",          // 必填：剧情设定（房主侧后台底稿，玩家看不到）
  "guidance": "……",          // 可选：主持私密指引（玩家看不到）
  "lorebook": [ /* ... */ ],  // 可选：关键词触发的世界书
  "prompt_blocks": [ /* ... */ ], // 可选：固定位置的提示块
  "sampling": { /* ... */ },  // 可选：temperature / top_p
  "time_config": { /* ... */ },   // 可选：游戏内时钟
  "time_rules": [ /* ... */ ],    // 可选：每轮耗时参考表
  "events": [ /* ... */ ],        // 可选：定时事件（依赖时钟开启）
  "random_turn_order": true       // 可选：是否每轮随机打乱行动顺序
}
```

再次强调：**没有** `characters`、`host_character`，也没有标题字段（标题由 AI 在开局时另行生成）。

---

## 三、字段写法与语义

### scenario（必填，1–500,000 字符）

- 它是**房主侧的后台剧情底稿**：作为 AI 的「初始情景」，只喂给 AI 并写入服务器存档，**玩家永远看不到**（玩家只看到 AI 据此生成的开场）。
- 因此可以放心写剧透、真相与结局条件。建议包含：时代 / 地点 / 氛围与世界观规则、当前局势、核心冲突或谜团、玩家方的目标与利害、开场时正在发生什么，以及剧情关键 NPC（用文字描述）。
- 写清楚玩家能感知的**目标、赌注与限制**，开场生成会依据它们展开。
- 建议精炼到必要信息，不要把整本设定原样堆进去。

### guidance（可选，有隐藏内容时强烈建议）

- 仅 AI 与存档可见的主持手册：节奏、规则、秘密真相、NPC 真实动机、反转、禁剧透要求、尺度边界、如何收束故事等。
- 系统会把 guidance 标记为 PRIVATE，要求 AI 不得引用、解释或泄露；AI 输出中若直接照抄 guidance 原文，会被校验机制拦下重试。
- 素材没有额外主持要求时，整个字段省略。

### lorebook（可选，≤200 条）

- 关键词触发的世界书。**只把「相关关键词在最近剧情出现时才需要注入」的设定放这里**（地点、组织、人物关系、真相等）；常驻事实优先写进 `scenario`，不要重复。
- 触发方式：`keys` 命中**最近的对话和当前输入**（不区分大小写、按「包含」匹配），命中后条目内容会被静默融入剧情，不会对玩家说「根据设定……」。
- `keys`：每条 1–100 个，每个 ≤40 字符，同一条内不可重复（忽略大小写）；请用专有名词、地名、人名等**精确而不过分常见的词**，避免误触发。
- `constant: true` 表示无视关键词每轮都注入，请克制使用（常用于必须始终生效的核心事实）。
- `order` 越大注入越靠后（存在感越强）；`title` 可选，会显示在注入块中。
- 素材里没有可触发条目时，整个字段省略。

### prompt_blocks（可选，≤100 条）

- 直接拼入 LLM 请求的指令块，`position` 三选一：
  - `system`：核心系统提示之后，权重最高——总规则、语气、破限类要求。
  - `scenario`：剧情 / 私密指引之后——本局追加的叙事规则。
  - `output`：生成前追加在请求末尾——输出格式、视角、字数、禁用内容等。
- 每条 content 1–10,000 字符。**只把素材中真实存在的硬性要求写进来**（系统提示、作者注、越狱预设、格式约束）；不要复述本项目内置规则，也不要把大段设定塞进 prompt_blocks。宁少勿滥。
- 后台材料，玩家看不到。

### time_config / time_rules / events（可选，成套使用）

- 只有当剧情真正依赖时间（倒计时、日程、宵禁、节日、探索耗时等）时才启用。
- `time_config`：
  - `enabled`：布尔，是否启用时钟；
  - `start_day`：0–100,000，默认 1；
  - `start_minute`：0–1439，从午夜起的分钟数（390 = 06:30），默认 390；
  - `max_elapsed_minutes`：0–525,600，单轮最大耗时，默认 600；
  - `default_elapsed_minutes`：0–525,600，AI 未估算耗时时的默认值，默认 15。
- `time_rules`（≤50 条）：每轮耗时参考表，供 AI 估算推进多少分钟。`activity` 活动名 ≤80 字符；`minutes_min` / `minutes_max` 0–525,600 且 min ≤ max。
- `events`（≤50 条）：定时事件，时钟到达该时刻时对全体在线玩家同时发生一次。
  - `name` ≤80 字符且不重复；`day` 0–100,000；`minute` 0–1439；`description` 1–10,000；
  - `public`: true = 事件名与描述会广播给玩家；false = 只给 AI 与存档（后台剧透）。
  - **events 必须配合 `time_config.enabled = true`**，否则整份配置会被拒绝。
- 不涉及时间推进的剧情，这些字段全部省略。

### sampling（可选）

- 只支持两个字段：`temperature` 0–2、`top_p` 0–1。
- **只取素材明确给出的值**；素材没有生成参数时省略，让服务器默认生效。

### random_turn_order（可选）

- 默认 `true`（每轮随机打乱玩家行动顺序）。只有剧情需要固定行动顺序时才写 `false`；没有特殊要求就省略。

---

## 四、SillyTavern 素材映射

| 素材内容 | 写入 | 说明 |
| --- | --- | --- |
| 角色卡的 scenario / 世界背景 / first_mes 里的开场设定 | `scenario` | 提取设定、局势与开场前因；**不要**抄角色本人的性格与说话风格 |
| 角色卡 description / personality / mes_example | —— | 属于角色卡，本次转换直接跳过；关键 NPC 改用 scenario / lorebook 文字描述 |
| 世界书 / World Info / character_book | `lorebook` | 一条对一条；常驻条目对应 `constant: true`，插入顺序对应 `order` |
| 系统提示 / Main Prompt / 越狱 / 作者注 / post_history_instructions | `prompt_blocks` | 全局规则 → `system`；本局叙事规则 → `scenario`；格式 / 字数 / 视角 → `output` |
| 采样设置（temperature / top_p） | `sampling` | 只取这两个字段 |
| 时间线 / 日程 / 倒计时 / 宵禁 | `time_config` + `time_rules` + `events` | 需要玩家感知的时间点用 events（`public` 按是否剧透选择） |
| 剧情真相 / 结局条件 / 禁剧透 / 主持节奏 | `guidance` | 后台材料，可写剧透 |
| 一段口头叙述的剧情概要 | 综合以上规则 | 先梳理「设定—局势—冲突—目标—隐藏真相」再分配字段；补全要克制 |

---

## 五、取舍清单（重要）

- `scenario` 必须输出且非空，其余字段「有的才写」。
- 没有对应内容就省略字段，不要为了填满结构而编造设定。
- 优先少而精：`lorebook` 和 `prompt_blocks` 越多越占上下文；重复、近义的条目应合并。
- **不要**输出 `characters`、`host_character`。
- 列表里的对象只允许出现上文列出的键名，不要发明新字段。

---

## 六、硬性限制（必须遵守）

| 字段 | 限制 |
| --- | --- |
| `scenario` | 1–500,000 字符，必填 |
| `guidance` | 0–500,000 字符 |
| `lorebook` | ≤200 条；`keys` 每条 1–100 个、每个 ≤40 字符且同条不重复；`content` 1–10,000；`title` ≤80；`order` 0–10,000；`enabled` / `constant` 布尔 |
| `prompt_blocks` | ≤100 条；`content` 1–10,000；`title` ≤80；`position` ∈ `system` / `scenario` / `output`；`enabled` 布尔 |
| `time_rules` | ≤50 条；`activity` ≤80；`minutes_min` ≤ `minutes_max`，0–525,600 |
| `events` | ≤50 条；`name` ≤80 且不重复；`day` 0–100,000；`minute` 0–1439；`description` 1–10,000；`public` 布尔；需 `time_config.enabled = true` |
| `time_config` | 见上文各字段范围与默认值 |
| `sampling` | `temperature` 0–2；`top_p` 0–1 |
| `random_turn_order` | 布尔，默认 `true` |

---

## 七、输出要求与格式示例

- 只输出一个合法 JSON 对象：双引号、无尾逗号、无注释、无围栏、无 JSON 以外的任何文字。
- 省略的字段直接不出现，不要写 `null` 占位。

格式示例（内容仅示意，请勿照抄）：

```json
{
  "scenario": "1920 年代的山间小镇……（背景规则、当前局势、核心冲突、玩家方目标与利害、开场时正在发生的事、关键 NPC）",
  "guidance": "隐藏真相是……；中期不要揭穿……；控制在三幕内收束，结局前保留一次反转。",
  "lorebook": [
    {
      "title": "钟楼",
      "keys": ["钟楼", "大钟"],
      "content": "钟楼三年前停摆，是小镇忌讳；钟声与失踪案有关。",
      "order": 10,
      "constant": false,
      "enabled": true
    }
  ],
  "prompt_blocks": [
    {
      "title": "叙事文风",
      "position": "output",
      "enabled": true,
      "content": "保持冷峻克制的第三人称叙述，单次回复不超过 300 字。"
    }
  ],
  "time_config": {
    "enabled": true,
    "start_day": 1,
    "start_minute": 1140,
    "max_elapsed_minutes": 480,
    "default_elapsed_minutes": 20
  },
  "time_rules": [
    { "activity": "调查一处地点", "minutes_min": 20, "minutes_max": 60 }
  ],
  "events": [
    {
      "name": "午夜钟声",
      "day": 1,
      "minute": 1380,
      "description": "停摆的钟楼自鸣，全镇同时听见。",
      "public": true
    }
  ],
  "sampling": { "temperature": 1.0, "top_p": 0.95 },
  "random_turn_order": true
}
```

---

## 八、导入后须知（给使用者）

- 导入只影响建房配置表单：`scenario` / `guidance` / `time_config` / `sampling` / `random_turn_order` 是**有则覆盖**；`time_rules` / `events` / `lorebook` / `prompt_blocks` 是**追加**（重复导入会叠加，想清空请刷新页面或手动删除）。
- 本文件不含角色卡：提交前请在「角色池」里至少添加一个带名字的角色，游戏才能开始；未被认领的角色由 AI 托管扮演。
- `scenario`、`guidance`、`lorebook`、`prompt_blocks`、`events.description` 都是后台材料：玩家看不到，但会写入服务器存档（HTML 记录）。
- `events` 中 `public = true` 的事件会向玩家广播名称与描述。
