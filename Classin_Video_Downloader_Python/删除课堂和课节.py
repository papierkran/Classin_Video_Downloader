import os

BASE_PATH = r"Y:\video"

def clean_name(name):
    # 删除 "课节名称_" 或 "课堂名称_"
    return name.replace("课节名称_", "").replace("课堂名称_", "").strip()

def clean_kongge(name):
    # 删除 第一个下划线后面的空格
    return name.replace(" ", "", 1)


def rename_in_directory(base_path):
    for root, dirs, files in os.walk(base_path, topdown=False):  # bottom-up to avoid path issues
        # 重命名文件
        for f in files:
            old_path = os.path.join(root, f)
            new_name = clean_name(f)
            # new_name = clean_kongge(new_name)
            if new_name != f:
                new_path = os.path.join(root, new_name)
                print(f"重命名文件: {old_path} -> {new_path}")
                try:
                    os.rename(old_path, new_path)
                except Exception as e:
                    print(f"文件重命名失败: {e}")

        # 重命名文件夹
        for d in dirs:
            old_path = os.path.join(root, d)
            new_name = clean_name(d)
            if new_name != d:
                new_path = os.path.join(root, new_name)
                print(f"重命名文件夹: {old_path} -> {new_path}")
                try:
                    os.rename(old_path, new_path)
                except Exception as e:
                    print(f"文件夹重命名失败: {e}")

if __name__ == "__main__":
    rename_in_directory(BASE_PATH)
