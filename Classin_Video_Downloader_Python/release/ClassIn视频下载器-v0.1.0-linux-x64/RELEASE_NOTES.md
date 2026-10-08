# ClassIn 视频下载器 v0.1.0

## 更新内容

- 去除对 `msedgedriver.exe` 的硬依赖
- 改为运行时自动检测浏览器并准备匹配驱动
- 完善 README，补充运行、打包与分发说明
- 调整 PyInstaller spec，保留必要资源目录

## 分发说明

- 主程序：`ClassIn视频下载器`
- 首次运行需要本机已安装 Edge / Chrome / Chromium 之一
- 如自动驱动获取失败，可手动指定驱动路径
