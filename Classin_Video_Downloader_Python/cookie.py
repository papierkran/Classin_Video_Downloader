import json
import time

from selenium import webdriver

# 初始化浏览器（以 Chrome 为例）
options = webdriver.ChromeOptions()
# 可选：指定用户数据目录，实现持久化会话
# options.add_argument("user-data-dir=C:\\path\\to\\your\\chrome_profile")
driver = webdriver.Chrome(options=options)

# 打开目标网站
driver.get("https://www.eeo.cn/cn/login")


time.sleep(40)

# 等待页面加载完成，确保 cookie 已生成
driver.implicitly_wait(40)
# 获取所有 cookie
cookies = driver.get_cookies()

# 将 cookie 保存到文件（例如 cookies.json）
with open("cookies.json", "w", encoding="utf-8") as file:
    json.dump(cookies, file, ensure_ascii=False, indent=4)

print("Cookie 已保存到 cookies.json 文件中。")

# 关闭浏览器
driver.quit()
