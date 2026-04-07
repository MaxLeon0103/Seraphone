# Seraphone AI 版：详细中文文档（安装 / 配置 / 使用 / 发布）

> 适用分支：`feature/ai-draft-analysis`
> 
> 本文档覆盖：项目结构、数据链路、AI 接入、Telegram 推送、常见问题、发布流程。

---

## 1. 项目目标

本版本目标：在保留原有 LCU 功能的前提下，新增 **AI 对局分析层**。

核心能力：

1. 选人阶段（BP）给出阵容风险和可执行建议
2. 进入游戏后（拿到敌方信息）自动补全分析
3. 可选将分析结果推送到 Telegram（每局一次，防刷屏）
4. AI 服务在桌面端设置里配置（无需改后端代码）

---

## 2. 架构与数据流

### 2.1 事件来源（LCU）

- 文件：`app/lol/connector.py`
- 关键订阅：
  - `/lol-champ-select/v1/session` -> `champSelectChanged`
  - `/lol-gameflow/v1/gameflow-phase` -> `gameStatusChanged`

### 2.2 业务入口

- 文件：`app/view/main_window.py`
- 关键函数：
  - `__onChampionSelectBegin()`：BP 开始，读取我方数据并做首轮分析
  - `__onGameStart()`：进入游戏后，补全敌方信息并触发 Telegram 推送

### 2.3 数据解析

- 文件：`app/lol/tools.py`
- 函数：
  - `parseAllyGameInfo(...)`：我方队友 + 战绩 + 位置信息
  - `parseGameInfoByGameflowSession(...)`：进游戏后可拿敌方信息

### 2.4 AI 分析模块

- 文件：`app/lol/ai_analyzer.py`
- 策略：
  - 规则层优先（快速、稳定）
  - AI 重写层可选（OpenAI 兼容接口）

### 2.5 UI 配置入口

- 文件：`app/view/setting_interface.py`
- 新增分组：`AI Assistant`
- 不再要求手改配置文件

---

## 3. 新增配置项说明（桌面端可视化）

在 Settings -> AI Assistant 配置：

1. **Enable AI analysis**
   - 开启/关闭 AI 分析主开关

2. **AI Base URL**
   - OpenAI 兼容地址
   - 示例：`https://api.openai.com/v1`

3. **AI Model**
   - 模型名称
   - 示例：`gpt-4o-mini` / `qwen-max` / `deepseek-chat`

4. **AI API Key**
   - API 密钥（密码样式显示）

5. **Push in-game analysis to Telegram**
   - 开启后在进入游戏拿到敌方数据时推送一次

6. **Telegram Bot Token**
   - Bot token（密码样式显示）

7. **Telegram Chat ID**
   - 接收消息的 chat id

---

## 4. OpenAI 兼容接口要求

只要服务兼容以下范式即可：

- Endpoint：`POST /chat/completions`
- Header：`Authorization: Bearer <key>`
- Body：`model + messages`

若服务不兼容该格式，请自行做一层网关适配。

---

## 5. Telegram 推送逻辑

### 5.1 触发时机

- 在 `__onGameStart()` 后执行
- 原因：此时敌方信息更完整

### 5.2 防刷屏机制

- 以 match key 去重
- 同一局仅推送一次

### 5.3 推送内容

- 阵容/风险摘要
- 三条可执行动作
- 出装提示
- 可选 AI 总结

---

## 6. 安装与运行

### 6.1 环境

- Windows
- Python 3.10+（建议）
- League Client 可正常读取 LCU

### 6.2 依赖安装

```bash
pip install -r requirements.txt
```

### 6.3 启动

```bash
python main.py
```

启动后在 Settings -> AI Assistant 配置参数即可。

---

## 7. 常见问题

### Q1: BP 阶段为什么看不到完整对手信息？

A: 当前代码链路里，敌方完整信息主要在进入游戏后可稳定获取，这是 LCU 数据可用时序导致。

### Q2: AI 超时会不会卡住客户端？

A: 不会。分析是非阻塞策略，AI 不可用时仍保留规则层结果。

### Q3: Telegram 没收到消息？

检查：

1. EnableAiTelegramPush 是否开启
2. Bot Token / Chat ID 是否正确
3. 机器人是否能给该 chat 发消息
4. 本地网络是否可访问 `api.telegram.org`

---

## 8. 安全建议

1. 不要把 API key、Bot token 提交到仓库
2. 生产使用建议走环境隔离（或本地密钥管理）
3. token 泄露后立即轮换

---

## 9. 开发建议（下一阶段）

1. 增加对局阶段策略（3/6/11级时间窗）
2. 增加英雄对线 matchup 知识库
3. 增加“角色职责缺口”可视化评分
4. AI 输出 JSON Schema 强校验，避免自由文本漂移

---

## 10. 版本记录（本次）

- 新增 `AI Assistant` 设置分组
- 新增 `ai_analyzer.py`
- 新增 `aiDraftAnalysisUpdated` 信号
- 新增进游戏后 Telegram 一次性推送
- 支持 OpenAI 兼容接口配置

---

如需后续继续做“局内时间线战术建议（分钟级）”和“英雄特定出装分支模型”，在本版本上可直接迭代。
