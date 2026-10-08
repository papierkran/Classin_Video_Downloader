import os
import re
import time
import csv
import random
import requests
from collections import defaultdict
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------------------
# 基础配置
# ---------------------------
SAVE_DIR = "Y:\\video"
CSV_FILE = "./courses.csv"
MAX_RETRIES = 3
TIMEOUT = 30
service = Service(executable_path='E:/project/python/Classin_Video_Downloader_Python/edgedriver_win64/msedgedriver.exe')


# ---------------------------
# 工具函数
# ---------------------------
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()

def parse_time(timestr: str):
    """CSV时间转datetime"""
    try:
        return datetime.strptime(timestr.split(" ")[0], "%Y/%m/%d")
    except:
        return None

def generate_unique_filename(base_name, extension, save_dir):
    index = 0
    while True:
        suffix = f"_{index}" if index > 0 else ""
        filename = f"{base_name}{suffix}{extension}"
        full_path = os.path.join(save_dir, filename)
        if not os.path.exists(full_path):
            return filename
        index += 1

def download_file(url, save_path, cookies=None):
    headers = {}
    if cookies:
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        headers["Cookie"] = cookie_str
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, stream=True, headers=headers, timeout=TIMEOUT) as r:
                r.raise_for_status()
                with open(save_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return True
        except Exception as e:
            print(f"[重试 {attempt}/{MAX_RETRIES}] 下载失败: {e}")
            time.sleep(2 * attempt)
    return False

def parse_cookie_string(cookie_str):
    cookies = []
    for cookie in cookie_str.split(";"):
        if cookie.strip():
            try:
                key, value = cookie.strip().split("=", 1)
                cookies.append({"name": key.strip(), "value": value.strip()})
            except:
                pass
    return cookies


# ---------------------------
# Selenium 页面解析
# ---------------------------
def get_videos(driver):
    videos = []
    try:
        rows = WebDriverWait(driver, 15).until(
            EC.presence_of_all_elements_located((By.XPATH, '//table/tbody/tr'))
        )
        for row in rows:
            try:
                download_url = row.find_element(By.XPATH, "./td[9]/div/a").get_attribute("href")
                record_time = row.find_element(By.XPATH, "./td[5]/div/span").text.strip().split(" ")[0]
                videos.append({
                    "download_url": download_url,
                    "record_date": record_time
                })
            except Exception:
                continue
    except Exception:
        pass
    return videos


# ---------------------------
# CSV 课程读取
# ---------------------------
def load_courses(csv_file):
    courses = []
    with open(csv_file, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            courses.append({
                "lesson_id": row.get("课堂ID", "").strip(),
                "course_id": row.get("班级ID", "").strip(),
                "lesson_name": sanitize_filename(row.get("课堂名称", "").strip()),
                "course_name": sanitize_filename(row.get("班级名称", "").strip()),
                "start_time": row.get("开课时间", "").strip()
            })
    return courses


# ---------------------------
# 主流程
# ---------------------------
def download_videos():
    driver = webdriver.Edge(service=service)
    driver.get("https://www.eeo.cn")

    cookie_str = "这里换成你的Cookie"
    cookies = parse_cookie_string(cookie_str)
    for c in cookies:
        c.setdefault("domain", ".eeo.cn")
        driver.add_cookie(c)
    driver.refresh()

    os.makedirs(SAVE_DIR, exist_ok=True)
    courses = load_courses(CSV_FILE)

    # 按班级分组，找每个班级最早时间
    course_groups = defaultdict(list)
    for c in courses:
        course_groups[c["course_id"]].append(c)

    for course_id, lessons in course_groups.items():
        # 该班级最早开课时间
        earliest = min(parse_time(l["start_time"]) for l in lessons if parse_time(l["start_time"]))
        earliest_str = earliest.strftime("%Y-%m-%d")
        course_name = lessons[0]["course_name"]


        # 一级目录
        course_folder = os.path.join(SAVE_DIR, f"{earliest_str}_{course_name}")
        os.makedirs(course_folder, exist_ok=True)

        for lesson in lessons:
            try:
                page_url = f"https://console.eeo.cn/saas/school/index.html#/singlePage/CourseManagement/recordLessonManagement?courseId={lesson['course_id']}&lessonId={lesson['lesson_id']}&record=true&live=true"
                driver.get(page_url)
                time.sleep(random.uniform(2, 5))

                videos = get_videos(driver)
                print(f"课程《{lesson['lesson_name']}》共 {len(videos)} 个视频")

                # 子目录：开课时间+课堂名称
                start_date = lesson["start_time"].split(" ")[0].replace("/", "-")
                lesson_folder = os.path.join(course_folder, f"{start_date}_{lesson['lesson_name']}")
                os.makedirs(lesson_folder, exist_ok=True)

                for v in videos:
                    base_filename = f"{start_date}_{lesson['lesson_name']}"
                    filename = generate_unique_filename(base_filename, ".mp4", lesson_folder)
                    save_path = os.path.join(lesson_folder, filename)

                    if os.path.exists(save_path):
                        print(f"已存在，跳过: {save_path}")
                        continue

                    if download_file(v["download_url"], save_path, cookies=cookies):
                        print(f"✅ 成功: {save_path}")
                    else:
                        print(f"❌ 失败: {save_path}")

                    time.sleep(random.uniform(2, 6))

            except Exception as e:
                print(f"课堂处理失败: {lesson} 错误: {e}")

    driver.quit()


if __name__ == "__main__":
    download_videos()
