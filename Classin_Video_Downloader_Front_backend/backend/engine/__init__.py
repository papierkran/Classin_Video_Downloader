"""vendored 下载引擎包。

将原单文件项目的 `videodownloader.py` / `driver_auto.py` 原样迁入，
仅修补了 driver_auto 的导入方式（见 videodownloader.py 顶部注释）。
引擎对外只暴露两个能力，供 service 层调用：
- download_videos(...)：阻塞式下载主循环（内部用线程池并发下载视频）
- check_cookie_validity(cookie_str)：用 HTTP 探测验证登录态
- load_courses_from_csv(csv_file)：兼容 utf-8-sig/gbk 的 CSV 解析
"""
