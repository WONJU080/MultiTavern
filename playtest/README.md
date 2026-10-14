# playtest —— 剧情卡自动跑测

用**多个 LLM 座位**真的把一张剧情卡跑一遍：房主建房并注入卡，每个角色一个座位，
轮到谁就调用该座位自己的模型提交行动，结束后写出覆盖度与健康报告。

它只说玩家说的话：走的是浏览器相同的 `/ws/{client_id}` 网关，所以建房、注入剧本、
认领角色、行动窗口、裁判、骰点、结算全是真的；不同的只是"人"换成了模型。

## 四个子命令

```bash
# 0. 网页跑测台：填 API、自检、跑测、看报告（本机 127.0.0.1，Key 只在内存里）
python -m playtest serve
python -m playtest serve --port 8787 --cards examples --no-open

# 1. 离线校验：不联网、不消耗 token，直接用服务器自身的校验逻辑 + 静态设计检查
python -m playtest validate --cards examples --config playtest/configs/example.yaml

# 2. 跑测前自检：服务器能不能连上、DM 凭据能不能用、每个座位端点与模型是否可达
python -m playtest check --config playtest/configs/example.yaml

# 3. 真跑一遍：每个角色一个 AI 座位，结束后写报告
python -m playtest run --config playtest/configs/example.yaml
python -m playtest run --config playtest/configs/example.yaml --dry-run   # 座位脚本化，只花 DM 的 token
```

安装包之后也可以直接用 `anyworld-playtest`（等价于 `python -m playtest`）。

## 网页跑测台

`python -m playtest serve` 会在本机启动一个单页界面（默认 <http://127.0.0.1:8787/>），
不用手写 YAML：

1. 选剧情卡（列表直接标出哪张卡不能跑、为什么）；
2. 填服务器地址、DM 凭据、默认座位 API，并按角色覆盖模型/风格/温度/独立 Key；
3. 点「自检」——只发少量 GET 请求，不花 token；
4. 点「开始跑测」，实时看进度（建房、座位就绪、谁提交了什么、每回合结算），
   结束后页面直接显示结论、覆盖度、健康问题与逐回合时间线，并可打开 Markdown 报告。

默认只监听 `127.0.0.1`：页面会接收 API Key，**不要**用 `--host 0.0.0.0` 把它暴露到局域网。
Key 只保存在该进程内存中，不写盘；「记住 Key」勾选后才会存进本机浏览器的 localStorage。

## 配置

见 [`configs/example.yaml`](configs/example.yaml)。要点：

- `server.url`：运行中的 anyworld 地址；自签证书默认跳过校验（`verify_tls: false`）。
- `dm.mode`：`borrow` 借用服务器的 `llm.api_key`（受 `server.borrowed_room_limit` 限制），
  或 `own` 由房主自带 endpoint/key/model。
- `seats.default` + `seats.overrides`：**每个角色一个座位**，可各自不同厂商/模型/温度；
  override 按字段合并进 default，只写要改的字段即可。`style` 决定玩家性格取向
  （`explore`/`social`/`bold`/`cautious`/`orderly`/`rotate`），默认在角色间轮转——
  同源模型若连提示词都一样，几个座位会写出几乎相同的行动，测卡就失去意义。
- `action_limit`（默认 80 字）：行动字数硬上限。真人玩家输入的是一句话，不是小作文；
  提示词会让座位尽量写到 40 字以内，超上限的回复会被**整句截断**（不会把引号切断）。
  想让某个角色说得更长，就在它的 override 里单独调大。
- Key 一律通过 `api_key_env` 从环境变量读取，不写进文件。

## 报告

每次运行写入 `<out_dir>/<时间戳>-<卡名>/`：

- `run.json`：机器可读全量数据（卡哈希、座位统计、逐回合时间线、token、覆盖度、健康检查）。
- `report.md`：人读版（覆盖度表、角色参与度、逐回合行动与结算、健康问题清单）。

覆盖度来自**玩家可见**的事实（共享地图、图鉴、揭晓的真相），不是 DM 的私密状态：

| 指标 | 说明 |
| --- | --- |
| 地区 | 访问/总数 + 未访问清单 |
| 物品 | 发现/调查/携带，线索物品单独统计 |
| 事件 | 触发/总数 + **已过触发时刻却没触发**的清单 |
| 世界书 | 关键词命中率（启发式：服务器不公开实际注入） |
| 真相 | 揭晓/作答 + **条件已满足却没揭晓**的清单（这是引擎缺陷，不是卡的问题） |
| 角色参与度 | 有效行动回合、被叙事提及、最长空档 |

退出码：`0` 正常结束 / `2` 运行失败（断线、卡死、服务器错误）/ `3` 未达结局（撞预算上限）。

## 两种调度模式

- **round**：轮流交卷，全队到齐一起结算（`turn_directive`）。
- **strict**：行动窗口（`turn_window`），每个行动有自己的耗时，时间轴连续推进；
  座位决策前会拉取 `map_state` / `codex_state`，并按需应答 `truth_submit`。

座位**从不使用 `hold`**：跑测的目的是推进剧情；全员观望会让时钟停住、窗口反复重开，
这正是工具里那个看门狗要处理的情况——检测到游戏内时间原地循环就改为强制提交行动。

## 常见问题

- **座位"返回了空行动"**：报告与运行日志会带上诊断（`finish_reason`、是否只返回了推理内容、输出 token 数）。
  若是推理型模型把额度用在思考上（`finish_reason=length`），工具会自动把 `max_output_tokens` 翻倍重试一次；
  仍为空时建议换模型或调大 `seats.default.max_output_tokens`（默认 800）。勾选"调试落盘"可以在报告目录的
  `responses/<角色>.md` 里看到模型**原样返回了什么**。
- **DM 结算失败**：引擎会暂停本回合并保留行动与骰点，工具会以房主身份自动 `retry_round`
  （次数由 `run.max_round_retries` 控制，默认 2），连续失败才停止并写明原因。
- **座位报 401 / `Incorrect API key`**：说明 endpoint 与 Key 不是同一家（例如 Key 是 DeepSeek 的，
  但 endpoint 留空会打到 OpenAI 官方）。工具会在第一次 401 时**立刻停止整局**并写明是哪个座位，
  不会再重试到白花额度。填好每个座位的 endpoint 即可。
- **座位指向本机/内网（llama.cpp、Ollama）时不要走代理**：默认策略就是「本机与内网直连、公网跟随系统代理」，
  单个座位可用 `proxy: true/false` 覆盖。
- **报 `ImportError: connecting through a SOCKS proxy requires python-socks`**：系统装了 SOCKS 代理，
  而 websockets 默认会跟随系统代理。默认配置（`server.proxy: false`）已经是直连，本机地址更是永远直连；
  只有确实要经代理访问远端服务器时才设 `server.proxy: true`，那时需要 `pip install python-socks`。

## 边界

- **行动被裁判驳回时**：座位会带上驳回理由重试（模型自己修）；同一回合内每座位提交次数有硬上限，
  超过就改用兜底行动，兜底也被驳回就停止该座位的本回合提交，等下一回合——避免"驳回→重试"空转与白花 token。
  已恢复的驳回只记为提示（`rejections`），不会把整局判成失败。
- 座位提示词只包含玩家可见信息：没有 `guidance`、没有后台世界书、没有队伍闲聊。
- 工具不会修改服务器状态，也不会自动修复发现的问题；发现疑似引擎缺陷时只在报告里标出。
- `.logged_games/*.html`（服务器端转写）与 `.debug/llm/` 属私密档案，跑测工具不读取、不导出。
