import os
import re
import shutil

# BASE_PATH = r"Y:\video"
BASE_PATH = r"C:\Users\WINNAS\Desktop\video"

pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})[_ ]+(.+?)(?:\s*-\s*(\d+))?$")

def normalize_name(name):
    name = name.replace("（", "(").replace("）", ")")
    return name.strip()

def find_first_level_folders(base_path):
    return [os.path.join(base_path, d) for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]

def main():
    folders = find_first_level_folders(BASE_PATH)

    grouped = {}
    move_plan = []  # 存储 (源路径, 目标路径)

    # 分组
    for folder_path in folders:
        folder_name = os.path.basename(folder_path)
        norm_name = normalize_name(folder_name)
        m = pattern.match(norm_name)
        if not m:
            continue
        date, course_name, number = m.group(1), m.group(2).strip(), m.group(3)

        if course_name not in grouped:
            grouped[course_name] = {
                "date_for_num1": None,
                "folders": {}
            }

        if number == '1':
            grouped[course_name]["date_for_num1"] = date

        num_key = int(number) if number else 0
        grouped[course_name]["folders"][num_key] = folder_path

    # 生成移动计划
    for course_name, info in grouped.items():
        date_for_num1 = info["date_for_num1"]
        if not date_for_num1:
            print(f"课程 {course_name} 没有找到课节1，跳过")
            continue

        parent_folder_name = f"{date_for_num1}_{course_name}"
        parent_folder_path = os.path.join(BASE_PATH, parent_folder_name)

        for num, folder_path in info["folders"].items():
            # 如果不想移动课节1，可以取消注释
            # if num == 1:
            #     continue

            dest_path = os.path.join(parent_folder_path, os.path.basename(folder_path))

            abs_src = os.path.abspath(folder_path)
            abs_dest = os.path.abspath(dest_path)
            if os.path.commonpath([abs_src, abs_dest]) == abs_src:
                print(f"跳过，目标路径是源路径的子目录: {folder_path}")
                continue

            move_plan.append((folder_path, dest_path))

    # 打印预览
    print("\n=== 移动预览 ===")
    for src, dest in move_plan:
        print(f"{src} -> {dest}")

    if not move_plan:
        print("没有需要移动的文件夹。")
        return

    # 确认
    confirm = input("\n按 1 确认执行移动，其他键取消: ")
    if confirm.strip() != "1":
        print("已取消。")
        return

    # 执行移动
    for src, dest in move_plan:
        parent_dir = os.path.dirname(dest)
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir)

        print(f"移动: {src} -> {dest}")
        try:
            shutil.move(src, dest)
        except Exception as e:
            print(f"移动失败: {src}，错误: {e}")

if __name__ == "__main__":
    main()
