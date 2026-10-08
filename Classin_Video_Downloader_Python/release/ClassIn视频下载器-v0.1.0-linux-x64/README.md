# ClassIn 视频下载器

一个用于 **批量下载 ClassIn 课堂录播视频** 的桌面工具，带图形界面，支持导入课程数据、保存配置、自动检测浏览器并自动下载匹配驱动。

## 当前版本特点

- 图形界面操作，适合日常直接使用
- 支持导入 `CSV / XLSX / XLS` 课程数据
- 支持粘贴 Cookie 登录态进行下载
- 自动检测本机浏览器（优先 Edge，其次 Chrome/Chromium）
- **不再依赖项目目录预置 `msedgedriver.exe`**
- 运行时可通过 `webdriver-manager` 自动获取匹配驱动
- 支持批量任务列表、进度显示、日志输出
- 支持打包为 `.exe`

---

## 项目主要文件

- `gui.py`：图形界面入口
- `videodownloader.py`：下载主逻辑
- `driver_auto.py`：浏览器与 WebDriver 自动检测/下载
- `ClassIn视频下载器.spec`：PyInstaller 打包配置
- `videodata/`：示例或导入用数据目录
- `image/`：说明图片等资源

---

## 运行环境

- Python 3.10+
- Windows 下推荐使用（最终目标是打包 exe）
- 需要本机安装以下浏览器之一：
  - Microsoft Edge
  - Google Chrome
  - Chromium

## 依赖安装

```bash
python -m pip install selenium requests webdriver-manager pandas openpyxl pyinstaller
```

Linux/macOS 下如果只是开发调试，也可使用：

```bash
python3 -m pip install selenium requests webdriver-manager pandas openpyxl pyinstaller
```

---

## 使用方式

### 1. 准备课程数据
支持导入：
- `.csv`
- `.xlsx`
- `.xls`

常见字段包括：
- `课堂ID`
- `班级ID`
- `课堂名称`
- `班级名称`
- `开课时间`

### 2. 准备 Cookie
在已经登录 ClassIn / 翼鸥相关后台后，从浏览器开发者工具中复制 Cookie，粘贴到程序界面的 Cookie 输入框。

> 注意：Cookie 有时效，过期后需要重新获取。

### 3. 启动程序
开发环境运行：

```bash
python gui.py
```

### 4. 配置下载参数
在界面中填写或选择：
- 浏览器驱动路径（可留空，程序自动处理）
- 视频保存目录
- 数据文件路径
- Cookie

### 5. 开始下载
点击“开始下载”，程序会：
1. 检测可用浏览器
2. 自动准备驱动
3. 加载课程数据
4. 逐条获取录播地址
5. 下载视频到指定目录

---

## 打包 exe

### 推荐方式

```bash
python -m PyInstaller ClassIn视频下载器.spec --noconfirm
```

或：

```bash
pyinstaller ClassIn视频下载器.spec --noconfirm
```

### 打包说明

当前 spec 已调整为：
- **不强制要求项目目录存在 `msedgedriver.exe`**
- 自动打包 `image/` 资源目录
- 保留 Selenium / Requests / webdriver-manager 所需依赖

打包完成后，生成结果通常位于：

- `dist/ClassIn视频下载器.exe`

---

## 分发说明

分发 exe 时，建议附带：
- `README.md` 或简化版使用说明
- `LICENSE`
- 示例数据文件（如需要）

由于驱动已改为运行时自动处理，**一般不需要随发行包手动附带 `msedgedriver.exe`**。

---

## 常见问题

### 1）找不到浏览器
请确认本机已安装：
- Edge
- Chrome
- Chromium

### 2）Cookie 无效
请重新登录后台并复制最新 Cookie。

### 3）下载失败
常见原因：
- Cookie 过期
- 课堂页面结构变化
- 网络波动
- 某条视频下载链接失效

### 4）驱动相关报错
现在程序默认会尝试自动处理驱动；如果自动获取失败，再手动指定驱动路径。

---

## 后续建议

可继续完善：
- 异常重试机制
- 多线程下载
- 下载历史记录
- 更完整的错误提示
- 导出日志

---

## License

本项目使用仓库内附带的 `LICENSE`。
