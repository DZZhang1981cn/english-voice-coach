# 雅思口语教练 · IELTS Speaking Coach

一个跑在 macOS 本地的 **AI 雅思口语教练**，用**实时语音**模拟真实雅思口语考试。它会像真正的雅思考官一样：给你 Part 2 话题卡 → 限时准备 → 你连续独白 → Part 3 深入追问 → 最后用英语点评「怎么组织答案」+ 给参考答案 + 按雅思官方**四维 0-9 分**给你评分。

> 学习数据（对话记录、评分、错题、报告）全部落盘本地，不锁在云端；语音识别/合成走阿里云百炼实时语音模型。

## 核心功能

| 功能 | 说明 |
|---|---|
| 🎲 **自动出题** | 从话题库随机抽一个 Part 2 话题卡（含提示点），也可自己手写话题 |
| ⏱ **1 分钟准备** | 雅思官方准备时长倒计时，帮你进入状态 |
| 🎤 **Part 2 独白** | 连续说 1-2 分钟，计时器只显示不打断，考官保持安静 |
| 🗣 **Part 3 追问** | 考官就同一话题追问 3-4 个深入问题，双向讨论 |
| 💬 **反馈 + 参考答案** | 考官英语点评「怎么组织/梳理答案」，并给一段高分参考答案 |
| 📊 **雅思四维评分** | FC 流利与连贯 / LR 词汇 / GRA 语法 / P 发音，各 0-9 分 + 总 Band |
| 📝 **错题集** | 自动纠错并按雅思维度归类（这个错误拉低哪一维分数） |
| 📚 **高分表达** | 自动提取值得学的高分词句（同义替换、话题词汇、搭配） |
| 📈 **进步趋势** | 跨天汇总四维 0-9 分数曲线，看长期进步 |
| 📚 **话题库** | Markdown 文件，随时批量增改话题 |

## 技术架构

```
┌─ 浏览器 (index.html) ─────────────────────────────┐
│  getUserMedia + RTCPeerConnection (WebRTC, 消回声) │
│  出题卡 / 计时器 / 聊天框 / 四维评分 / 趋势图        │
└──────────────┬────────────────────────────────────┘
               │ /signal /config /topic /assess /progress /report
┌──────────────▼───────────────┐   ┌──────────────────┐
│ 本地服务 (server.py, 8766)    │──▶│ 阿里云百炼实时语音  │
│ 信令代理 / 出题 / 报告         │   │ qwen-audio-3.0-   │
└──────────────┬───────────────┘   │ realtime-plus     │
               │                   └──────────────────┘
               │ /assess
┌──────────────▼───────────────┐
│ 本地 Ollama (qwen3.5:9b)     │
│ 雅思四维评分 / 纠错 / 高分表达  │
└──────────────────────────────┘
```

- **语音底座**：阿里云百炼 Token Plan 实时语音 `qwen-audio-3.0-realtime-plus`（WebRTC 协议，浏览器内核自带 AEC 回声消除 + 降噪）
- **雅思考官人设**：`人设.md` 注入 `session.update`，含 Part 2 / Part 3 / 反馈三段流程规则
- **出题**：`话题库.md`（Markdown）解析 → `GET /topic` 随机抽题
- **本地评分**：Ollama `qwen3.5:9b`（关思考模式）按雅思四维 0-9 打分、纠错、提取高分表达
- **落盘**：`训练历史/YYYY-MM-DD.jsonl`（对话流 + 评分 + 纠错 + 高分表达）、`训练历史/报告/*.md`（雅思报告）

## 快速开始

```bash
# 双击「启动英语教练.command」即可（自动起服务 + 开浏览器），无需打开任何 AI 工具
# 或手动：
~/Applications/anaconda3/envs/qwen-data-video/bin/python server.py
# 浏览器打开 http://127.0.0.1:8766/
```

关掉浏览器后，服务会**约 60 秒后自动退出**（心跳 + 空闲退出机制）。

## 使用流程（一次完整模拟）

1. **出题**（左栏）：点「🎲 随机抽题」从话题库抽一个，或在输入框自己写话题
2. **准备**：点「⏱ 开始准备」，1 分钟倒计时
3. **Part 2 独白**：点「🎤 开始作答」，连续说 1-2 分钟（计时器只显示不打断）
4. **进入 Part 3**：点「⏹ 结束作答」，考官开始追问
5. **获取反馈**：点「💬 结束 Part 3」，考官点评 + 参考答案 + 四维评分
6. **结束**：点「🏁 结束训练」，自动生成雅思报告

## 话题库（方便批量增改）

话题存在 **`话题库.md`**，纯 Markdown 格式，格式如下：

```markdown
## Describe a product you often recommend to your customers
- What the product is
- Who you usually recommend it to
- Why you recommend it
- And explain why it is popular with your customers

### Part 3
- Why do some products become more popular than others?
- How has online shopping changed the way people buy products?
- Do you think brand loyalty is still important for products like tires?
```

- `## ` 开头 = 一个 Part 2 话题（cue card 题目）
- 紧跟的 `- ` = 提示点（bullet points）
- `### Part 3` 后 = Part 3 追问问题

**批量增加 = 复制粘贴一个整块**，改文字即可，不用碰引号/逗号/括号。保存后随机抽题会自动包含新话题。

## 目录结构

```
私人英语教练/
├── server.py                # 本地服务（信令代理 + 出题 + 评分 + 报告）
├── kb_index.py              # 知识库索引 + 检索
├── index.html               # 浏览器界面（出题 | 聊天框 | 评分侧栏）
├── 人设.md                  # 雅思考官人设
├── 话题库.md                # 雅思话题库（Markdown，批量增改）
├── 知识库/                  # 训练背景资料（不上传）
├── 训练历史/                # 对话记录 + 报告（隐私，不上传）
├── 启动英语教练.command     # 双击启动
├── docs/screenshot.png      # 界面截图
└── README.md
```

## 隐私与安全

- **API key 不硬编码**：百炼 key 存 macOS Keychain（`security find-generic-password -s bailian-token-plan-key`），`server.py` 运行时动态读取，代码里没有任何明文 key。
- **学习数据全本地**：对话、评分、错题、报告都只落在你机器上，`.gitignore` 排除了 `训练历史/`、`知识库/`、`个人设置.json`、`话题库.md`、`人设.md`。

## 技术栈

- 前端：原生 HTML/CSS/JS，`getUserMedia` + `RTCPeerConnection`（WebRTC）+ Canvas 折线图
- 后端：Python 标准库 `http.server`（零 Web 框架依赖）
- 语音：阿里云百炼 Token Plan 实时语音（WebSocket 信令 + WebRTC 音频）
- 本地推理：Ollama `qwen3.5:9b`（雅思四维评分、纠错、高分表达提取）
