# ClassIn 视频下载管理系统

前后端一体的 ClassIn 回放视频批量下载管理后台：导入课堂 → 创建任务 → 后台下载 → 实时进度/日志。

- **后端**：Python 3.12 + FastAPI + SQLAlchemy + SQLite（WAL）
- **前端**：Vue 3 + Element Plus（本地 vendor，无 CDN、无构建）
- **引擎**：Selenium（Edge/Chrome/Chromium）自动驱动，支持断点续传、限速、跳过已下载

详细设计见 [docs/技术方案.md](docs/技术方案.md)。

## 快速开始

```powershell
cd Classin_Video_Downloader_Front_backend
pip install -r requirements.txt
python run.py
# 打开 http://127.0.0.1:8000  默认账号 admin / admin123（首次登录后请改密）
```

> 浏览器与驱动缺失时，首次启动任务会自动下载匹配的驱动（也可手动放 `msedgedriver.exe` 到项目根目录）。

## 功能说明

| 页面 | 能力 |
|------|------|
| 登录 | 账号密码登录，5 次失败锁定 5 分钟 |
| 数据看板 | 课堂/任务聚合计数、最近 5 个任务、浏览器与驱动环境自检 |
| 课堂管理 | 手工新增、CSV/XLSX 批量导入（upsert 幂等）、状态过滤、关键词搜索、批量删除 |
| 下载任务 | 选课堂（或全部）建任务；启动/暂停/恢复/停止/重跑/删除；三级进度（任务→课堂→视频）；实时日志（增量游标） |
| 视频库 | 按课堂/班级/状态搜索视频片段；逐段显示下载中、成功、失败、已存在；支持在线播放、下载、失败课堂重试和扫描服务器旧文件 |
| 系统设置 | Cookie（脱敏显示+在线验证）、保存目录、并发数、限速、跳过已下载、无头模式、改密码 |

## 核心逻辑拆解

**1. 任务快照**
创建任务时把选中课堂复制为 `TaskItem` 快照——之后增删/改课堂不影响进行中的任务，历史任务永远可回显。

**2. 集中式状态机**
`created → running ⇄ paused → stopping → success/failed/cancelled`，所有迁移只在 `TaskService` 内发生（API 层零 status 赋值）。非法操作统一 409 + 中文状态名。判定优先级：**停止信号 > 有课成功 > 全部失败**。

**3. 全局单任务闸门**
`_runtime` 内存单例 + `_runtime_lock` 互斥锁：start 在锁内完成"校验→重置→写库→起线程"，同一时刻仅一个任务占用（单浏览器实例 + SQLite 单写者，并发收益为负）。轮询读不加锁，任务运行中接口不卡。

**4. worker 线程 + 回调桥接**
引擎在独立 daemon 线程阻塞执行（不卡事件循环），自带独立 DB session。4 个回调（日志/进度/课程状态/单课完成）模拟观察者模式回写：

- 全部经 `runtime.lock` 串行，防多线程写库竞态；
- 进度"内存必更 + DB 0.5s 节流"，防 SQLite 写放大；
- 回调只在 running 态写进度，避免把"已暂停"竞态改回"运行中"；
- worker `finally` 必调 `_finalize`，浏览器崩/网络断也不会卡在假 running。

**5. 崩溃恢复**
启动时 `recover_stale_tasks()` 把残留 running/paused/stopping 统一置 failed——进程重启后状态永远可信。

**6. 安全**
PBKDF2(240k) 口令哈希、JWT 7 天、登录限速（`用户名@IP`）、Cookie 明文只存库且 API 永远脱敏、500 只回 `request_id` 不回堆栈、HOST 默认只绑 127.0.0.1。

## Linux / 公网部署（2-3 人）

1. 安装 Python、Chromium/Chrome 及匹配的 WebDriver；系统设置启用无头模式。下载目录由运行服务的 Linux 用户读写；
2. 设置 `CLASSIN_HOST=127.0.0.1`，通过 Nginx/Caddy 配置域名和 TLS 并反代到本机服务；
3. 当前运行闸门是进程内锁，Uvicorn 必须使用单 worker（不要使用 `--workers` 多进程）；
4. 设置页的保存路径是 Linux 服务器路径（如 `/srv/classin/videos`），不是访问者电脑上的 `D:\...`。视频存储建议使用独立挂载盘并监控磁盘空间；迁移旧下载时先将原视频目录完整复制到服务器，保留“首次开课日期_课程名称/开课日期_课程名称-第几次课程/*.MP4”结构，再从“视频库”执行扫描；
5. 视频播放使用短时、单视频范围的 HttpOnly Cookie，并支持 HTTP Range；反向代理须保留 Range/Content-Range 响应头和断点请求；
6. `/api/health` 供探活；`/api/docs` 为 API 文档（可在反代层封掉）；备份数据库、`secret.key` 和视频目录。

## 目录结构

```
Front_backend/
├── run.py                  # 启动入口
├── requirements.txt
├── backend/
│   ├── main.py             # 装配：日志→建表→恢复→异常兜底→路由→静态
│   ├── api/                # 薄路由层（含视频库/播放接口）
│   ├── services/           # 状态机/业务（task_service 为核心）
│   ├── repositories/       # 泛型 CRUD + 各领域查询
│   ├── db/                 # models / session / PRAGMA(WAL,FK)
│   ├── core/               # config / security / exceptions / logging
│   └── engine/             # 复用的下载引擎（与上游保持一致，勿重构）
├── frontend/               # index.html + app.js + styles.css + vendor/
├── docs/技术方案.md
└── data/                   # app.db / app.log / secret.key / videos/（运行时生成）
```

## 测试

```powershell
python -m pyflakes backend run.py        # 静态检查（engine 目录为外部代码，除外）
# 功能验证：启动后 API 路由经 OpenAPI 核验；状态机 E2E 覆盖
# start→pause→双pause(409)→resume→stop→cancelled 全链路
```
