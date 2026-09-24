# MultiTavern（多人酒馆）

系自娱自乐系列第四弹，非常好玩，尤其适合和同学玩。

基于 [AnyWorld](https://github.com/iamarxs/AnyWorld) 的多人 AI 跑团游戏：房主创建房间和角色池，朋友凭邀请码加入并认领角色，AI 作为主持人推进剧情。

## 玩法

1. **建房**：房主打开游戏页面，选择「创建房间」，输入自己的名字（若服务器要求，还需输入 admin 密码），得到一个邀请码。
2. **设定剧情**：房主创建角色池（每个角色可写一句描述），并选定自己扮演的角色；再输入剧情设定，AI 会生成一个标题。
3. **加入**：其他玩家选择「加入房间」，输入名字和邀请码，然后认领一个尚未被认领的角色。未被认领的角色由 AI 扮演。
4. **开始游戏**：房主点击「开始游戏」，AI 生成开场剧情。之后玩家按认领顺序轮流行动，AI 汇总并推进故事。
5. **中途加入**：游戏进行中，任何人仍可凭邀请码进入并认领空闲角色，下一轮起生效（AI 会叙述角色的交接）。
6. **投票跳过**：轮到某人行动时，其他在线玩家可以投票跳过；全体同意后，该玩家本轮的行动由 AI 代为决定，且该玩家会被移出游戏（需要重新加入）。
7. **关闭房间**：房主可随时点击「关闭房间」解散房间。房间不会因为无人而自动删除，会一直保留到被手动关闭或服务器重启。

## 服务器配置须知

- **运行环境**：Python 3.11+。安装依赖 `pip install -e .`，启动 `python app.py`。程序自带 HTTPS（自签证书），浏览器首次访问需手动「继续前往」。
- **接入 AI**：编辑 `config.yaml` 的 `llm` 部分，填 OpenAI 兼容接口。例如 DeepSeek：
  ```yaml
  llm:
    provider: "compatible"
    endpoint: "https://api.deepseek.com/v1"
    api_key: "你的key"
    model_name: "deepseek-chat"
    structured_outputs: false
  ```
- **常用配置**（`config.yaml`）：
  - `server.admin_password`：建房所需的密码。公网开放时建议设置，防止陌生人消耗你的 API 余额。
  - `server.max_characters`：每个房间的角色池上限。
  - `server.empty_room_timeout_seconds` / `server.abandoned_room_timeout_seconds`：设为 `null` 表示房间永不因空闲被自动删除。
- **运维面板**：访问 `https://<服务器地址>/admin/rooms`，输入 admin 密码后可查看所有运行中的房间并关闭它们。
- 游戏的所有 AI 调用都走你配置的 API，会消耗余额，请注意。

## 说明

本项目基于 [AnyWorld](https://github.com/iamarxs/AnyWorld)（MIT License）。

上传者本人在配置服务器时只是提供了地址和密码，详细内容不太清楚，因此建议想玩的直接问agent。
