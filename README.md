# 小雅全能助手 (Personal Secretary)

个人多模态 AI 秘书 / 知识库，自动记录归类文字、语音、图片、灵感。

## 功能

- **收集箱 (inbox)**：支持文字 / 语音 (.webm) / 图片，一键录入
- **AI 自动归类**：调用 sensenova 模型，自动判断内容归入 随想 / 记账 / A工地 等分类
- **自动落盘 (auto-file)**：高/中置信度内容自动归档进对应文件夹，低置信度留存人工确认
- **项目管理 / 记账 / 待办提醒**：分模块管理
- **PWA**：手机可添加到桌面，自适应布局
- **Cloudflare 隧道**：内网穿透公网可达
- **每日自动备份**

## 版本

### v0.5.1 (2026-11-24) — 自动归类落盘 (auto-file)

新增内容进收集箱后 **AI 自动归档** 能力，无需人工逐条确认：

- 新增 `auto_file_record()`：AI 分析后，高/中置信度自动 move 到目标文件夹
- 新增 `POST /api/auto-file` 端点：一键批量归类 inbox 待归档记录
- 置信度策略：high/medium 自动归档，low 留 inbox 等人工确认（保留人工兜底）
- 改动约 90 行，纯新增，可回滚（备份 `server.py.bak-*-before-autofile`）

### v0.5 (2026-11-11) — 安全加固版

- 添加 Bearer Token 认证
- AI 自动分类（sensenova SenseChat-5）
- 收集箱、项目、记账、待办、导出/备份

## 部署

Docker 容器运行，宿主机挂载：

```
/vol1/1000/personal-secretary/server.py -> /app/server.py
/vol1/1000/personal-secretary/data      -> /data
/vol1/1000/personal-secretary/web       -> /web
```

环境变量：
- `SECRETARY_TOKEN` — API 认证 Token
- `AI_API_KEY` — sensenova API Key
- `PORT` — 监听端口（默认 8080）

## 技术栈

Python 3 (stdlib HTTPServer) + 原生 HTML/JS (PWA) + sensenova API + Cloudflare Tunnel
