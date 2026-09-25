# 适配指南：把剧本 / SillyTavern 文件改写成多合一导入文件

这份文档是喂给 AI 编程助手的「说明书」。你可以把它丢给任意一个 agent（opencode / Claude / Cursor 等），让它学习本项目的配置格式，然后根据你的剧本或现成的 SillyTavern 文件，改写并生成一个可以直接导入本项目的 JSON 文件。

---

## 一、这个项目是什么

MultiTavern 是多人 AI 跑团（文字 RPG）。房主建房时会在表单里配置一整局的内容，配置可导出成一个 JSON 文件，之后任何人再建房时「导入」该文件即可复用整局设定（导入是**合并**语义：文件里有哪个字段就覆盖/追加哪个字段，不会清空已填内容）。

因此，一个「多合一导入文件」= **剧情（scenario）+ GM 手册（guidance）+ 预设（prompt_blocks / lorebook / sampling / 角色卡等）** 的 JSON 打包。

---

## 二、导入文件的完整 JSON 结构

顶层是一个对象，所有字段都**可选**（缺失即不覆盖）。完整字段如下：

```jsonc
{
  // —— 剧情 ——
  "scenario": "写给 AI 的场景描述与开场前因（房主可见，也会成为游戏的开场依据）",
  "guidance": "仅房主/AI 可见的主持私密指引（玩家永远看不到），写清规则、节奏、真相与禁剧透要求",

  // —— 角色池（玩家/角色卡） ——
  "characters": [
    {
      "name": "角色名（必填，用作 player_resolutions 的 key）",
      "description": "角色身份/外貌/背景（一句话到几句话）",
      "personality": "性格",
      "style": "文风/语气",
      "example_dialogue": "台词示例"
    }
  ],
  "host_character": "房主认领的角色名（可留空 = 房主以主持身份旁观）",

  // —— 时间系统（可选，开启后剧情按分钟推进） ——
  "time_config": {
    "enabled": true,
    "start_day": 1,
    "start_minute": 390,          // 起始时刻 = 6:30（分钟数）
    "max_elapsed_minutes": 600,   // 单轮最大耗时上限
    "default_elapsed_minutes": 15 // 玩家未填耗时的默认值
  },
  "time_rules": [                 // 耗时参考表（供 AI 估算每轮推进多少分钟）
    { "activity": "搜查单间", "minutes_min": 20, "minutes_max": 40 }
  ],
  "events": [                     // 定时事件（需要 time_config.enabled 才会生效）
    { "name": "午间广播", "day": 1, "minute": 720, "description": "后台描述", "public": false }
  ],

  // —— 世界书（关键词触发） ——
  "lorebook": [
    {
      "title": "条目名（可选）",
      "keys": ["关键词1", "关键词2"],   // 1~100 个，任一出现在剧情里即插入 content
      "content": "当关键词出现时注入的设定文字",
      "order": 0,                      // 插入顺序，越大越靠后（影响越强）
      "constant": false,               // true = 常驻（无视关键词）
      "enabled": true
    }
  ],

  // —— 提示块（直接拼进 LLM 请求） ——
  "prompt_blocks": [
    {
      "title": "块标题（可选）",
      "position": "system",           // system | scenario | output
      "enabled": true,
      "content": "注入的指令文字"
    }
  ],

  // —— 采样参数 ——
  "sampling": { "temperature": 1.0, "top_p": 0.95 },

  // —— 随机回合顺序 ——
  "random_turn_order": true
}
```

要点：

- `position` 三选一：`system`（拼在系统提示后，最高权重）、`scenario`（拼在场景设定后）、`output`（拼在输出格式附近，适合约束格式/视角/字数）。
- `lorebook.keys` 单条 **1~100 个关键词**；`content` 最长 10000 字符。
- `time_rules` 的分钟数上限放宽到 525600（支持以天计的规则）。
- `events` 只有在 `time_config.enabled = true` 时才会被接受。
- `guidance`、`lorebook` 里的隐藏设定、`events.description` 都是**后台材料**：只进 LLM 和存档，绝不发给房间成员。

---

## 三、把 SillyTavern 素材映射进来

拿到现成的 SillyTavern（或酒馆类）文件时，按下面的映射改写：

| SillyTavern 概念 | 对应本项目字段 |
| --- | --- |
| 角色卡 / Character Card（description、personality、first message 等） | `characters[].description / personality / style / example_dialogue` |
| 世界书 / World Info / Lorebook（keyword 触发条目） | `lorebook[]`（keys + content + order + constant） |
| 系统提示 / System Prompt、作者注 / Author's Note | `prompt_blocks[]`（position 用 `system` 或 `scenario`） |
| JB / 越狱 / 破限预设 | `prompt_blocks[]`（position 用 `system`，enabled=true） |
| 采样设置（Temperature、Top P 等） | `sampling` |
| 场景/剧情简介 | `scenario` |
| 主持人规则 / GM 提示 | `guidance` |

---

## 四、给 agent 的提示词模板

把本 md 和你的原始素材一起丢给 agent，然后说：

> 请阅读这份《适配指南》学习 MultiTavern 的导入文件格式。现在请根据我提供的剧本（或这些 SillyTavern 文件），改写并生成一个**多合一导入文件 JSON**：
> 1. 剧情部分写进 `scenario`；GM 规则、节奏、真相、禁剧透要求写进 `guidance`；
> 2. 角色卡改写进 `characters`（每人含 name/description/personality/style/example_dialogue）；
> 3. 世界书条目改写进 `lorebook`（每条 1~100 个关键词）；
> 4. 系统提示/越狱/格式约束改写进 `prompt_blocks`，并按内容选好 `position`；
> 5. 采样参数写进 `sampling`；
> 6. 只输出一个合法的 JSON 对象，不要加任何解释文字。
> 素材如下：……

之后把生成的 JSON 保存为 `.json` 文件，在游戏建房页点「导入配置」即可。

---

## 五、注意事项

- 导入是**合并**：标量字段（scenario / guidance / time_config / sampling / random_turn_order）文件里有才覆盖；列表字段（characters / time_rules / events / lorebook / prompt_blocks）会**追加**，不会清空已有内容。想清空某列表请手动删除或刷新页面。
- 输出必须是**合法 JSON**，顶层是对象，不要包在 markdown 代码块里（去掉 ```json 围栏）。
- 关键词用逗号分隔，中英文均可；不要在 key 里放空串。
- `guidance` 和隐藏世界书条目可能包含剧透，请放心写——它们不会显示给玩家。
