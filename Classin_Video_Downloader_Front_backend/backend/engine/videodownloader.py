import os
import re
import sys
import time
import csv
import socket
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 从顶层绝对导入改为包内相对导入：
# 本文件被 vendored 到 backend/engine/ 包中复用，原版 `import driver_auto`
# 依赖同目录脚本在 sys.path 上，作为包模块运行时会 ImportError。
from . import driver_auto


def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def sanitize_filename(name):
    if not name:
        return name
    s = re.sub(r'[\\/*?:"<>|]', '_', str(name)).strip()
    s = s.rstrip('. ')
    return s if s else '_'


def format_start_time_for_filename(start_time_str):
    if not start_time_str:
        return ""
    try:
        formatted = start_time_str.replace('/', '-').replace(' ', '_').replace(':', '-')
        parts = formatted.split('_')
        if len(parts) >= 2:
            date_part = parts[0]
            time_part = parts[1]
            date_parts = date_part.split('-')
            if len(date_parts) == 3:
                year, month, day = date_parts
                date_part = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            formatted = f"{date_part}_{time_part}"
        return formatted
    except Exception:
        return sanitize_filename(start_time_str)


def generate_unique_filename(base_name, extension, save_dir):
    return f"{base_name}{extension}"


def download_file(url, save_path, cookies=None, progress_callback=None, timeout=60, resume=False, speed_limit=0, stop_event=None):
    headers = {}
    if cookies:
        cookie_str = "; ".join([f"{cookie['name']}={cookie['value']}" for cookie in cookies])
        headers["Cookie"] = cookie_str
    
    # 断点续传支持
    existing_size = 0
    if resume and os.path.exists(save_path):
        existing_size = os.path.getsize(save_path)
        headers['Range'] = f'bytes={existing_size}-'
    
    try:
        with requests.get(url, stream=True, headers=headers, timeout=timeout) as r:
            # 设置socket读超时，防止iter_content无限阻塞
            sock = r.raw._fp.fp.raw._sock if hasattr(r.raw._fp.fp, 'raw') else None
            if sock is None:
                try:
                    sock = r.raw._fp.fp._sock
                except Exception:
                    sock = None
            if sock is not None:
                sock.settimeout(timeout)
            
            # 检查是否支持断点续传
            if resume and existing_size > 0 and r.status_code == 206:
                mode = 'ab'
                total = int(r.headers.get('Content-Length') or 0) + existing_size
                done = existing_size
            elif resume and existing_size > 0 and r.status_code == 200:
                mode = 'wb'
                total = int(r.headers.get('Content-Length') or 0)
                done = 0
            else:
                mode = 'wb'
                total = int(r.headers.get('Content-Length') or 0)
                done = 0
            
            r.raise_for_status()
            last_t = time.time()
            last_done = done
            speed_limit_bytes = speed_limit
            stop_checked = time.time()
            
            with open(save_path, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    
                    # 定期检查停止信号（每~2MB数据检查一次）
                    now = time.time()
                    if now - stop_checked > 2.0:
                        stop_checked = now
                        if stop_event is not None and getattr(stop_event, 'is_set', lambda: False)():
                            print('收到停止信号，中断下载:', save_path)
                            return False
                    
                    if speed_limit_bytes > 0:
                        chunk_size = len(chunk)
                        if chunk_size > speed_limit_bytes:
                            for i in range(0, chunk_size, speed_limit_bytes):
                                if stop_event is not None and getattr(stop_event, 'is_set', lambda: False)():
                                    return False
                                f.write(chunk[i:i+speed_limit_bytes])
                                time.sleep(1)
                        else:
                            f.write(chunk)
                            elapsed = time.time() - last_t
                            if elapsed < 1 and done - last_done >= speed_limit_bytes:
                                time.sleep(1 - elapsed)
                    else:
                        f.write(chunk)
                    
                    done += len(chunk)
                    if progress_callback:
                        if now - last_t >= 0.5:
                            delta_bytes = done - last_done
                            delta_t = max(now - last_t, 1e-6)
                            speed_mbps = (delta_bytes / 1024 / 1024) / delta_t
                            percent = int(done * 100 / total) if total > 0 else 0
                            info = f"{done/1024/1024:.1f}MB/{(total/1024/1024):.1f}MB" if total > 0 else f"{done/1024/1024:.1f}MB"
                            progress_callback(percent, speed_mbps, info)
                            last_t = now
                            last_done = done
            if progress_callback:
                progress_callback(100, 0.0, '完成')
        return True
    except requests.exceptions.Timeout:
        print('下载超时:', save_path)
        if progress_callback:
            try:
                progress_callback(0, 0.0, '下载超时')
            except Exception:
                pass
        return False
    except requests.exceptions.RequestException as e:
        print('下载出错:', e)
        if progress_callback:
            try:
                progress_callback(0, 0.0, f'失败: {e}')
            except Exception:
                pass
        return False
    except OSError as e:
        print('文件写入出错:', e)
        return False
    except Exception as e:
        print('下载异常:', e)
        return False


def get_course_details(driver):
    try:
        course_name = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, '/html/body/div[1]/div/div[2]/div/div[2]/p[1]/span[1]'))
        ).text
        course_name = course_name.replace('课程名称：', '').strip()
        return sanitize_filename(course_name)
    except Exception as e:
        print('获取课程名称失败:', e)
        return None


def get_download_info(driver, course_id, lesson_id):
    # ClassIn 新版页面使用独立的 /v/WebcastPlayback 路由，旧版
    # #/singlePage 路由已失效。直接打开页面地址也便于 SPA 在登录态下初始化。
    page_url = (
        "https://console.eeo.cn/saas/school/v/WebcastPlayback/recordLessonManagement"
        f"?courseId={course_id}&lessonId={lesson_id}&record=true&live=false"
    )
    driver.get(page_url)

    try:
        # 新页面在数据加载完成后渲染 videodata_numCount 和带 download 属性的直链。
        def video_list_ready(d):
            counters = d.find_elements(By.CSS_SELECTOR, ".videodata_numCount")
            links = d.find_elements(By.CSS_SELECTOR, "tr.el-table__row a[download][href]")
            if not counters:
                return bool(links)
            match = re.search(r"\((\d+)\)", counters[0].text or "")
            return len(links) >= int(match.group(1)) if match else bool(links)

        WebDriverWait(driver, 30).until(
            video_list_ready
        )
    except Exception as e:
        print(f'等待新版录课视频列表加载失败 (课程ID:{course_id}, 课堂ID:{lesson_id}):', e)
        return None, []

    video_rows = driver.find_elements(By.CSS_SELECTOR, "tr.el-table__row")
    videos = []
    course_name = None
    for row_idx, row in enumerate(video_rows):
        try:
            links = row.find_elements(By.CSS_SELECTOR, "a[download][href]")
            if not links:
                continue
            link = links[0]
            download_url = (link.get_attribute("href") or "").strip()
            if not download_url.startswith(("http://", "https://")):
                continue

            cells = row.find_elements(By.CSS_SELECTOR, "td")
            segment_title = sanitize_filename(cells[1].text.strip()) if len(cells) > 1 else None
            # 新版列顺序：片段、录制类型、录制位置、时间范围、时长、大小、状态、下载。
            record_type = cells[2].text.strip() if len(cells) > 2 else ""
            record_location = cells[3].text.strip() if len(cells) > 3 else ""
            record_method = sanitize_filename("_".join(x for x in (record_type, record_location) if x)) or "未知方式"
            record_time_text = cells[4].text.strip() if len(cells) > 4 else ""
            record_date = record_time_text.split()[0] if record_time_text else "未知日期"
            segment_title = segment_title or f'片段_{row_idx + 1}'

            # 新版页面把课程名放在下载链接 title 中（末尾带片段序号）。
            if not course_name:
                link_title = (link.get_attribute("title") or "").strip()
                course_name = re.sub(r"\s+_\s+\d+$", "", link_title).strip() or None

            print(f'[DEBUG] 第{row_idx}行: 标题={segment_title}, 日期={record_date}, 方式={record_method}, URL={download_url[:50]}...')
            videos.append({
                'download_url': download_url,
                'record_date': record_date,
                'record_method': record_method,
                'segment_title': segment_title,
            })
        except Exception as e:
            print(f'[DEBUG] 解析第{row_idx}行视频条目失败: {e}')

    if not course_name and videos:
        # 页面标题作为兜底；去掉站点名后再用于目录和文件名。
        page_title = (driver.title or "").strip()
        course_name = re.sub(r"^录课视频数据\(|\)\s*-\s*.*$", "", page_title).strip() or page_title
    course_name = sanitize_filename(course_name) if course_name else None
    print(f'[DEBUG] 找到 {len(videos)} 个视频 (课程ID:{course_id}, 课堂ID:{lesson_id}, 课程名:{course_name or "未知"})')
    return course_name, videos


def load_courses_from_csv(csv_file):
    def _read(encoding):
        courses = []
        with open(csv_file, newline='', encoding=encoding) as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                reader.fieldnames = [h.strip().lstrip('\ufeff') for h in reader.fieldnames]
            for row in reader:
                if not isinstance(row, dict):
                    continue
                lesson_id = (row.get('课堂ID') or '').strip()
                if not lesson_id:
                    continue
                courses.append({
                    'lesson_id': lesson_id,
                    'course_id': (row.get('班级ID') or '').strip(),
                    'lesson_name': (row.get('课堂名称') or '').strip(),
                    'class_name': (row.get('班级名称') or '').strip(),
                    'start_time': (row.get('开课时间') or '').strip(),
                    'creation_time': (row.get('创建时间') or '').strip(),
                })
        return courses
    try:
        return _read('utf-8-sig')
    except UnicodeDecodeError:
        return _read('gbk')
    except Exception as e:
        print('读取 CSV 文件失败:', e)
        return []


def parse_cookie_string(cookie_str):
    cookies = []
    for cookie in cookie_str.split(';'):
        if cookie.strip():
            try:
                key, value = cookie.strip().split('=', 1)
                cookies.append({'name': key.strip(), 'value': value.strip()})
            except Exception as e:
                print('解析 Cookie 失败：', e)
    return cookies


def check_cookie_validity(cookie_str, log_callback=None):
    """检查Cookie是否有效，返回(is_valid, message)"""
    def log(msg):
        if log_callback:
            log_callback(msg)
        print(msg)
    
    try:
        cookies = parse_cookie_string(cookie_str)
        if not cookies:
            return False, 'Cookie为空'
        
        # 尝试访问一个需要登录的API来验证Cookie
        headers = {
            'Cookie': cookie_str,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        # 访问用户信息接口验证登录状态
        test_url = 'https://console.eeo.cn/saas/school/index.html'
        response = requests.get(test_url, headers=headers, timeout=10, allow_redirects=False)
        
        # 检查响应状态
        if response.status_code == 200:
            # 检查是否被重定向到登录页
            if 'login' in response.text.lower() or '登录' in response.text:
                return False, 'Cookie已过期，请重新获取'
            return True, 'Cookie有效'
        elif response.status_code == 302:
            # 被重定向，可能是Cookie无效
            location = response.headers.get('Location', '')
            if 'login' in location.lower():
                return False, 'Cookie已过期，请重新获取'
            return True, 'Cookie可能有效'
        else:
            return False, f'验证失败，状态码: {response.status_code}'
            
    except requests.exceptions.Timeout:
        return False, '验证超时，请检查网络连接'
    except requests.exceptions.RequestException as e:
        return False, f'验证请求失败: {e}'
    except Exception as e:
        return False, f'验证异常: {e}'


def download_videos(save_dir, csv_file, cookie_str, driver_path=None, headless=False,
                    log_callback=None, pause_event=None, stop_event=None,
                    progress_callback=None, lesson_done_callback=None, driver_ref=None,
                    course_status_callback=None, courses_override=None, max_workers=4, resume=True,
                    speed_limit=0, skip_existing=True, video_status_callback=None):
    def log(msg):
        print(msg)
        try:
            if log_callback:
                log_callback(msg)
        except Exception:
            pass

    browser, bin_path = driver_auto.detect_browser_preference()
    if not browser:
        raise RuntimeError('未检测到可用浏览器：请安装 Microsoft Edge 或 Chrome/Chromium')
    log(f'检测到浏览器: {browser}')
    if driver_path and os.path.exists(driver_path):
        driver_exe = driver_path
        log(f'使用指定驱动: {driver_exe}')
    else:
        log('正在准备浏览器驱动...')
        driver_exe = driver_auto.ensure_driver(browser, base_dir=_get_base_dir())
        log(f'驱动就绪: {driver_exe}')
    log('正在启动浏览器...')
    driver = driver_auto.create_webdriver(browser, driver_exe, headless=headless, binary_path=bin_path)
    if driver_ref is not None:
        driver_ref['driver'] = driver
    log('浏览器启动成功')

    driver.get('https://www.eeo.cn')
    cookies = parse_cookie_string(cookie_str or '')
    for cookie in cookies:
        if 'domain' not in cookie:
            cookie['domain'] = '.eeo.cn'
        try:
            driver.add_cookie(cookie)
        except Exception as e:
            log(f'添加 cookie 失败: {e}')
    driver.refresh()
    time.sleep(3)

    save_dir = os.path.normpath(os.path.abspath(save_dir or os.path.join(os.path.expanduser('~'), 'videos')))
    os.makedirs(save_dir, exist_ok=True)

    if courses_override is not None:
        courses = courses_override
        log(f'使用 GUI 传入课程列表：{len(courses)} 条')
    else:
        csv_file = csv_file or os.path.join(_get_base_dir(), 'courses.csv')
        courses = load_courses_from_csv(csv_file)
        if not courses:
            log('未能加载到课程信息，请检查 CSV 文件。')
            try:
                driver.quit()
            finally:
                if driver_ref is not None:
                    driver_ref.pop('driver', None)
            return
        log(f'读取到 {len(courses)} 个课程')

    from collections import defaultdict
    class_earliest_start = defaultdict(lambda: None)
    for c in courses:
        cid = c.get('course_id')
        st = c.get('start_time', '')
        if cid and st:
            try:
                dt = st.replace('/', '-').replace(' ', 'T').split('T')[0]
                existing = class_earliest_start[cid]
                if existing is None or dt < existing:
                    class_earliest_start[cid] = dt
            except Exception:
                pass

    if progress_callback:
        try:
            progress_callback(len(courses), 0, None, None)
        except Exception:
            pass

    for course_index, course in enumerate(courses, start=1):
        try:
            if course_status_callback:
                course_status_callback(course_index, course, 'downloading', '开始处理')
        except Exception:
            pass

        if stop_event is not None and getattr(stop_event, 'is_set', lambda: False)():
            log('收到停止信号，终止下载。')
            break

        course_id = course['course_id']
        lesson_id = course['lesson_id']
        log(f'处理课程：课程ID {course_id} - 课堂ID {lesson_id}')
        if progress_callback:
            try:
                progress_callback(len(courses), course_index, course, None)
            except Exception:
                pass

        course_name, videos = get_download_info(driver, course_id, lesson_id)
        total_videos = len(videos) if videos else 0
        log(f'获取到 {total_videos} 个视频')

        if not course_name or not videos:
            log(f'课程 {course_id} - {lesson_id} 没有可下载的视频，跳过。')
            try:
                if course_status_callback:
                    course_status_callback(course_index, course, 'failed', '没有可下载的视频')
            except Exception:
                pass
            try:
                if lesson_done_callback:
                    lesson_done_callback({'course_id': course_id, 'lesson_id': lesson_id, 'course_name': course_name or course.get('class_name') or '未知班级', 'class_name': course.get('class_name') or '未知班级', 'lesson_name': course.get('lesson_name'), 'total_videos': 0, 'downloaded': 0, 'course_row_index': course_index, 'total_courses': len(courses)})
            except Exception:
                pass
            continue

        class_name_csv = course.get('class_name') or course.get('course_id') or '未知班级'
        lesson_name_csv = course.get('lesson_name') or course.get('lesson_id') or None
        start_time_csv = course.get('start_time', '')
        earliest_date = class_earliest_start.get(course_id) or ''
        formatted_earliest = f"{earliest_date.replace('-', '-')}" if earliest_date else ''
        class_name_part = sanitize_filename(class_name_csv) or '未知班级'
        class_dir_name = f'{formatted_earliest}_{class_name_part}' if formatted_earliest else class_name_part
        class_id_part = sanitize_filename(course_id) if course_id else ''
        if class_id_part:
            class_dir_name = f'{class_dir_name}_{class_id_part}'
        formatted_start = format_start_time_for_filename(start_time_csv)
        lesson_name_part = sanitize_filename(lesson_name_csv or course_name or '未知课堂')
        lesson_dir_name = f'{formatted_start}_{lesson_name_part}' if formatted_start else lesson_name_part
        lesson_id_part = sanitize_filename(lesson_id) if lesson_id else ''
        if lesson_id_part:
            lesson_dir_name = f'{lesson_dir_name}_{lesson_id_part}'
        folder_path = os.path.join(save_dir, class_dir_name, lesson_dir_name)
        os.makedirs(folder_path, exist_ok=True)

        downloaded_count = 0
        failed_count = 0
        download_lock = threading.Lock()
        
        def download_video_task(idx, video, course_name, folder_path, total_videos):
            """单个视频下载任务"""
            nonlocal downloaded_count, failed_count
            
            record_time = video['record_date']
            record_method = video['record_method']
            download_url = video['download_url']
            segment_title = video.get('segment_title') or f'片段{idx}'
            
            # [调试日志]
            print(f'[DEBUG] 开始下载视频{idx}/{total_videos}:')
            print(f'       标题: {segment_title}')
            print(f'       日期: {record_time}')
            print(f'       方式: {record_method}')
            print(f'       URL: {download_url[:80]}...')
            
            base_filename = f'{record_time}_{sanitize_filename(course_name)}_{record_method}_{sanitize_filename(segment_title)}'
            filename = generate_unique_filename(base_filename, '.mp4', folder_path)
            save_path = os.path.join(folder_path, filename)

            def notify_video(status, percent=0, error=''):
                if video_status_callback:
                    try:
                        video_status_callback({
                            'course': course, 'video': video, 'path': save_path,
                            'status': status, 'percent': percent, 'error': error,
                            'video_index': idx, 'total_videos': total_videos,
                        })
                    except Exception:
                        pass
            
            # 检查是否跳过已存在的文件
            if skip_existing and os.path.exists(save_path):
                existing_size = os.path.getsize(save_path)
                if existing_size > 0:
                    log(f'跳过已存在文件: {filename} ({existing_size/1024/1024:.1f}MB)')
                    notify_video('skipped', 100)
                    with download_lock:
                        downloaded_count += 1
                    if progress_callback:
                        try:
                            progress_callback(len(courses), course_index, course, idx)
                        except Exception:
                            pass
                    return
            
            log(f'下载中: {save_path}')
            notify_video('downloading', 0)
            
            # 修复lambda闭包问题 - 将变量作为默认参数传递
            def make_progress_callback(idx_val, total_val, course_val):
                def callback(percent, speed_mbps, info):
                    if progress_callback:
                        try:
                            progress_callback(len(courses), course_index, course_val, 
                                            {'percent': percent, 'speed_mbps': speed_mbps, 'info': info, 
                                             'video_index': idx_val, 'total_videos': total_val})
                        except Exception:
                            pass
                    notify_video('downloading', percent)
                return callback if progress_callback else None
            
            progress_cb_for_this_video = make_progress_callback(idx, total_videos, course)

            max_retries = 3
            success = False
            for attempt in range(1, max_retries + 1):
                if stop_event is not None and getattr(stop_event, 'is_set', lambda: False)():
                    break
                if attempt > 1:
                    log(f'重试下载 ({attempt}/{max_retries}): {filename}')
                    if os.path.exists(save_path):
                        try:
                            os.remove(save_path)
                        except Exception:
                            pass
                success = download_file(download_url, save_path, cookies=cookies,
                                        progress_callback=progress_cb_for_this_video, 
                                        resume=resume, speed_limit=speed_limit,
                                        stop_event=stop_event)
                if success:
                    break
                if attempt < max_retries:
                    log(f'下载失败，3秒后重试: {filename}')
                    time.sleep(3)

            with download_lock:
                if success:
                    downloaded_count += 1
                    log(f'下载成功: {save_path}')
                    notify_video('success', 100)
                else:
                    failed_count += 1
                    log(f'下载失败 (已重试{max_retries}次): {save_path}')
                    notify_video('failed', 0, f'下载失败（已重试 {max_retries} 次）')
                if progress_callback:
                    try:
                        progress_callback(len(courses), course_index, course, idx)
                    except Exception:
                        pass
        
        # 使用线程池并发下载（可配置的线程数）
        max_workers = max(1, min(8, max_workers))
        print(f'[DEBUG] 使用 {max_workers} 个线程进行并发下载')
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for idx, video in enumerate(videos, start=1):
                # 检查暂停/停止信号
                while pause_event is not None and getattr(pause_event, 'is_set', lambda: False)():
                    if stop_event is not None and getattr(stop_event, 'is_set', lambda: False)():
                        break
                    time.sleep(0.3)
                
                if stop_event is not None and getattr(stop_event, 'is_set', lambda: False)():
                    log('收到停止信号，取消剩余下载任务。')
                    executor.shutdown(wait=False)
                    break
                
                # 提交下载任务到线程池
                future = executor.submit(download_video_task, idx, video, course_name, folder_path, total_videos)
                futures[future] = idx
            
            # 等待所有任务完成
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    log(f'下载任务异常: {e}')

        try:
            if course_status_callback:
                st = 'success' if downloaded_count == total_videos and total_videos > 0 else ('failed' if total_videos == 0 or downloaded_count == 0 else 'success')
                msg = f'完成：{downloaded_count}/{total_videos}' if total_videos else '没有视频'
                course_status_callback(course_index, course, st, msg)
        except Exception:
            pass

        try:
            if lesson_done_callback:
                lesson_done_callback({'course_id': course_id, 'lesson_id': lesson_id, 'course_name': course_name, 'class_name': class_name_csv, 'lesson_name': lesson_name_csv, 'total_videos': total_videos, 'downloaded': downloaded_count, 'course_row_index': course_index, 'total_courses': len(courses)})
            if progress_callback:
                progress_callback(len(courses), course_index, course, None)
        except Exception:
            pass

    try:
        driver.quit()
    finally:
        if driver_ref is not None:
            driver_ref.pop('driver', None)


if __name__ == '__main__':
    pass
