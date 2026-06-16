# 水印小程序 🖼️

基于 **FastAPI + Pillow + MoviePy** 构建的水印添加工具，支持图片和视频。

---

## 功能列表

- 支持图片格式：JPG / PNG / WebP
- 支持视频格式：MP4 / MOV
- 文字水印（支持中文）
- Logo 图片水印
- 五种位置：左上 / 右上 / 左下 / 右下 / 居中
- 透明度调节（0 ~ 100）
- 全图平铺水印
- 简洁中文前端页面，无需外部 CDN 依赖

---

## 安装依赖

```bash
pip install -r requirements.txt
```

---

## 字体说明

中文水印需要一款支持 CJK（中日韩）字符的 TrueType 字体。

- **已内置字体**：项目已将 `wqy-zenhei.ttc`（文泉驿正黑，GPL 协议，可自由分发）直接放入 `fonts/` 目录，克隆仓库即可使用，无需任何额外配置。
- **自定义字体**：如需替换，可将任意 CJK 字体放入 `fonts/` 目录，程序会按以下文件名顺序优先查找：

```
fonts/wqy-zenhei.ttc
fonts/wqy-microhei.ttc
fonts/NotoSansCJK-Regular.ttc
fonts/NotoSansSC-Regular.otf
fonts/NotoSerifCJK-Regular.ttc
fonts/simhei.ttf
```

若 `fonts/` 中未找到字体，程序还会在系统字体路径（如 `/usr/share/fonts`、Nix store）中搜索 CJK 字体；全部失败时回退到 Pillow 默认字体（无法显示中文）并打印日志提示。

```
fonts/wqy-zenhei.ttc
fonts/wqy-microhei.ttc
fonts/NotoSansCJK-Regular.ttc
fonts/NotoSansSC-Regular.otf
fonts/NotoSerifCJK-Regular.ttc
fonts/simhei.ttf
```

若 `fonts/` 中未找到字体，程序还会在系统字体路径（如 `/usr/share/fonts`、Nix store）中搜索 CJK 字体；全部失败时回退到 Pillow 默认字体（无法显示中文）并打印日志提示。

---

## 启动服务

```bash
python main.py
```

服务默认监听 `http://0.0.0.0:8000`，打开浏览器访问即可使用。

---

## Telegram 机器人

本项目内置 Telegram 机器人，可通过机器人直接发送图片/视频并获取加水印结果。

### 环境变量配置

在 Railway（或本地）中设置以下环境变量：

| 变量 | 必填 | 说明 |
|------|------|------|
| `BOT_TOKEN` | ✅ | 从 [@BotFather](https://t.me/BotFather) 获取的机器人 Token |
| `ADMIN_IDS` | ✅ | 管理员的 Telegram 用户 ID，多个用英文逗号分隔，如 `123456,789012` |
| `SECRET_KEY` | ✅ | Session 加密密钥，设置为随机字符串，如 `openssl rand -hex 32` 的输出 |
| `WEB_ADMIN_PASSWORD` | ✅ | 管理员登录网页后台的密码 |
| `ADMIN_USERNAME` | 可选 | 显示给普通用户的管理员联系方式（不含 @），如 `myname` |
| `WEB_URL` | 可选 | 网站公开 URL，用于 `/webtoken` 回复中的链接，如 `https://your-domain.com` |
| `WEBHOOK_URL` | 可选 | 机器人 Webhook 公开 HTTPS URL，如 `https://your-domain.com`。设置后机器人改用 webhook 模式接收消息，避免多实例部署时的轮询冲突；不设置则使用 polling（仅适合本地开发） |
| `HTTPS_ONLY` | 可选 | 设为 `true` 时，Session Cookie 仅通过 HTTPS 发送（生产环境强烈建议开启） |
| `DB_PATH` | 可选 | SQLite 数据库文件路径，默认 `watermark_bot.db`（位于工作目录）。**Railway 等容器平台的文件系统是临时的，重新部署/重启会清空**，必须把它指向持久化卷才能保存用户数据与水印模板，例如挂载卷到 `/data` 后设置 `DB_PATH=/data/watermark_bot.db`。程序会自动创建该路径所在目录，并将 Logo 模板图片存放在同一目录下的 `user_logos/`，因此数据库与图片水印模板会一起持久化。 |

> 💡 **数据持久化提示（Railway）**：若未挂载持久化卷并设置 `DB_PATH`，水印模板看似“保存成功”，但容器重启后数据库会被重置，模板随之丢失。请在 Railway 中添加一个 Volume（如挂载到 `/data`），并将 `DB_PATH` 指向该卷内的文件。

设置 `BOT_TOKEN` 后，机器人将在 Web 服务启动时自动一起启动。

### 用户角色

| 角色 | 说明 |
|------|------|
| 👑 管理员 | 通过 `ADMIN_IDS` 配置，无限制使用，拥有所有管理命令 |
| ⭐ 会员 | 管理员授权，在有效期内无限制使用 |
| 👤 普通用户 | 每天最多 3 次，可联系管理员购买会员 |

### 用户命令

| 命令 | 说明 |
|------|------|
| `/start` | 欢迎页，查看身份和使用说明 |
| `/template` | 设置水印模板（文字 或 图片 Logo） |
| `/settings` | 调整水印位置、透明度、平铺等参数 |
| `/status` | 查看当前身份、使用次数和水印设置 |
| `/webtoken` | 获取网页后台登录令牌 |
| `/help` | 显示帮助 |

### 管理员命令

| 命令 | 说明 |
|------|------|
| `/addmember <用户ID> <天数>` | 授权会员，如 `/addmember 123456 30` |
| `/revokemember <用户ID>` | 撤销会员资格 |
| `/userinfo <用户ID>` | 查询用户信息和水印设置 |
| `/stats` | 查看机器人统计数据 |

### 使用流程（Telegram）

1. 发送 `/template` 选择水印类型：
   - **文字水印**：直接输入文字内容
   - **图片水印**：上传 Logo 图片（推荐 PNG 透明背景）
2. 发送 `/settings` 调整位置、透明度、是否平铺
3. 之后直接发送图片或视频，机器人自动加上水印返回

---

## 网页后台

访问 `http://服务器地址:8000` 即可使用网页版功能。

### 登录方式

| 角色 | 登录凭证 |
|------|---------|
| 👑 管理员 | Telegram ID + `WEB_ADMIN_PASSWORD` |
| ⭐ 会员 / 👤 普通用户 | Telegram ID + 网页令牌（在机器人发送 `/webtoken` 获取） |

### 网页功能

**所有已登录用户：**
- 查看身份和今日使用次数
- 设置并保存水印模板（文字/Logo、位置、透明度、平铺）
- 上传图片/视频并添加水印，下载结果

**管理员专属（进入 `/admin`）：**
- 📊 概览：用户总数、会员数、今日活跃统计
- 👥 用户管理：搜索用户、授权/撤销会员资格
- ⚙️ 系统设置：修改默认水印参数和普通用户每日限额

---

## 健康检查

`GET /health` 返回 `{"status": "ok"}`，可用于容器或 Railway 的健康探针配置。

---

## 已修复的安全问题

| # | 问题描述 | 修复方案 |
|---|---------|---------|
| 1 | `/download/{filename}` 路径穿越漏洞 | 使用正则白名单校验文件名字符，再通过 `resolve()` + `startswith` 确认路径在 outputs 目录内，文件不存在返回 404 |
| 2 | HTML 复选框布尔值解析错误 | `tiled` 改为字符串接收，手动转换为布尔值 |
| 3 | 视频平铺模式未实现 | 用 PIL 生成透明 PNG 水印图层叠加到视频，与图片逻辑一致 |
| 4 | 输出文件永不清理 | 使用 `BackgroundTasks` 在 300 秒后自动删除输出文件 |
| 5 | 视频编码线程数硬编码 | 改为 `os.cpu_count()` 自动适配 |
| 6 | 字体加载失败静默 | `except` 块添加 `print` 日志 |
| 7 | 视频写入阻塞事件循环 | 使用 `asyncio.get_event_loop().run_in_executor` 包裹 |
| 8 | 无上传大小限制 | 添加中间件，限制请求体最大 200 MB |
