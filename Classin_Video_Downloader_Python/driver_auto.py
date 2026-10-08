import os
import sys
import subprocess
import zipfile
import io


def _get_base_dir():
    """获取程序所在目录，兼容 PyInstaller 打包后的 exe"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def which(cmd: str):
    """跨平台 which。Windows 下也尽量工作。"""
    try:
        if os.name == 'nt':
            p = subprocess.run(["where", cmd], capture_output=True, text=True)
            out = (p.stdout or "").strip().splitlines()
            return out[0].strip() if out else None
        p = subprocess.run(["bash", "-lc", f"command -v {cmd}"], capture_output=True, text=True)
        out = (p.stdout or "").strip()
        return out if out else None
    except Exception:
        return None


def _windows_candidate_paths():
    pf = os.environ.get('ProgramFiles', r'C:\\Program Files')
    pfx86 = os.environ.get('ProgramFiles(x86)', r'C:\\Program Files (x86)')
    lad = os.environ.get('LocalAppData')

    edge = [
        os.path.join(pfx86, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(pf, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    ]
    if lad:
        edge.append(os.path.join(lad, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))

    chrome = [
        os.path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(pfx86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    ]
    if lad:
        chrome.append(os.path.join(lad, 'Google', 'Chrome', 'Application', 'chrome.exe'))

    return edge, chrome


def detect_browser_preference():
    """返回 (browser, binary_path)；browser in {'edge','chrome'}。

    Windows：优先用常见安装路径，其次 where。
    Linux/macOS：用 command -v。
    """
    if os.name == 'nt':
        edge_paths, chrome_paths = _windows_candidate_paths()
        for p in edge_paths:
            if p and os.path.exists(p):
                return 'edge', p
        for p in chrome_paths:
            if p and os.path.exists(p):
                return 'chrome', p

        # fallback: where
        for c in ('msedge', 'microsoft-edge', 'msedge.exe'):
            p = which(c)
            if p:
                return 'edge', p
        for c in ('chrome', 'google-chrome', 'chrome.exe'):
            p = which(c)
            if p:
                return 'chrome', p
        return None, None

    # 非 Windows：优先 Edge，再 Chrome
    for c in ("microsoft-edge", "microsoft-edge-stable"):
        p = which(c)
        if p:
            return "edge", p
    for c in ("google-chrome", "chromium-browser", "chromium"):
        p = which(c)
        if p:
            return "chrome", p
    return None, None


def _copy_to_program_dir(src_path: str, program_dir: str, dest_name: str):
    """把下载出来的 driver 复制到程序目录（exe 同级），便于 PyInstaller 场景使用。"""
    import shutil

    if not src_path or not os.path.exists(src_path):
        return src_path
    os.makedirs(program_dir, exist_ok=True)
    dest = os.path.join(program_dir, dest_name)
    try:
        shutil.copy2(src_path, dest)
        # 确保可执行
        try:
            os.chmod(dest, 0o755)
        except Exception:
            pass
        return dest
    except Exception:
        # 复制失败则退回原路径
        return src_path


def _get_edge_version(binary_path: str) -> str:
    """从 Edge 浏览器获取版本号（优先注册表）。"""
    import re

    def _is_valid_version(v: str) -> bool:
        return bool(re.match(r'^\d+\.\d+\.\d+\.\d+$', v.strip()))

    # 方案1：注册表（最可靠）
    try:
        import winreg
        for root_key in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(root_key, r'Software\Microsoft\Edge\BLBeacon')
                version, _ = winreg.QueryValueEx(key, 'version')
                winreg.CloseKey(key)
                if version and _is_valid_version(version):
                    return version.strip()
            except Exception:
                pass
    except Exception:
        pass

    # 方案2：读取二进制文件版本信息
    try:
        import struct
        with open(binary_path, 'rb') as f:
            data = f.read()
        # PE 文件中搜索版本模式 X.Y.Z.W
        match = re.search(rb'(\d+\.\d+\.\d+\.\d+)', data)
        if match:
            candidate = match.group(1).decode('ascii')
            if _is_valid_version(candidate):
                return candidate
    except Exception:
        pass

    return None


def _download_edge_driver_direct(version: str, base_dir: str) -> str:
    """直接从网络下载 Edge 驱动（备用方案）。"""
    import requests

    major = version.split('.')[0] if version else None
    if not major:
        return None

    dest_name = 'msedgedriver.exe' if os.name == 'nt' else 'msedgedriver'
    dest_path = os.path.join(base_dir, dest_name)

    url = f'https://msedgedriver.azureedge.net/{version}/edgedriver_win64.zip'
    mirrors = [
        f'https://msedgedriver.azureedge.net/{version}/edgedriver_win64.zip',
        f'https://npmmirror.com/mirrors/edgedriver/{version}/edgedriver_win64.zip',
    ]

    for url in mirrors:
        try:
            print(f'[驱动下载] 尝试: {url}')
            resp = requests.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                for name in zf.namelist():
                    if name.endswith('.exe') or name == 'msedgedriver':
                        with zf.open(name) as src, open(dest_path, 'wb') as dst:
                            dst.write(src.read())
                        break
            if os.path.exists(dest_path):
                print(f'[驱动下载] 成功: {dest_path}')
                return dest_path
        except Exception as e:
            print(f'[驱动下载] 失败: {e}')
            continue

    return None


def ensure_driver(browser: str, base_dir: str = None):
    """自动下载匹配 driver 到 base_dir，并尽量复制一份到程序目录返回。

    优先使用 webdriver_manager，失败后尝试直接下载。
    """
    base_dir = base_dir or _get_base_dir()
    os.makedirs(base_dir, exist_ok=True)

    if browser == "edge":
        dest_name = 'msedgedriver.exe' if os.name == 'nt' else 'msedgedriver'

        # 方案1：webdriver_manager
        try:
            from webdriver_manager.microsoft import EdgeChromiumDriverManager
            from webdriver_manager.core.driver_cache import DriverCacheManager
            cache = DriverCacheManager(root_dir=base_dir)
            src = EdgeChromiumDriverManager(cache_manager=cache).install()
            return _copy_to_program_dir(src, base_dir, dest_name)
        except Exception as e:
            print(f'[驱动下载] webdriver_manager 失败: {e}')

        # 方案2：直接下载
        _, bin_path = detect_browser_preference()
        version = _get_edge_version(bin_path) if bin_path else None
        if version:
            print(f'[驱动下载] 检测到 Edge 版本: {version}')
            result = _download_edge_driver_direct(version, base_dir)
            if result:
                return result

        raise RuntimeError('Edge 驱动下载失败，请检查网络或手动指定驱动路径。')

    if browser == "chrome":
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.driver_cache import DriverCacheManager
            cache = DriverCacheManager(root_dir=base_dir)
            src = ChromeDriverManager(cache_manager=cache).install()
            dest_name = 'chromedriver.exe' if os.name == 'nt' else 'chromedriver'
            return _copy_to_program_dir(src, base_dir, dest_name)
        except Exception as e:
            print(f'[驱动下载] Chrome driver 下载失败: {e}')
            raise RuntimeError('Chrome 驱动下载失败，请检查网络或手动指定驱动路径。')

    raise ValueError(f"Unsupported browser: {browser}")


def create_webdriver(browser: str, driver_path: str, headless: bool = False, binary_path: str = None):
    """创建 Selenium WebDriver。"""
    if browser == "edge":
        from selenium import webdriver
        from selenium.webdriver.edge.service import Service

        options = webdriver.EdgeOptions()
        if binary_path:
            options.binary_location = binary_path
        if headless:
            try:
                options.add_argument('--headless=new')
            except Exception:
                options.add_argument('--headless')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--window-size=1280,720')

        service = Service(executable_path=driver_path)
        return webdriver.Edge(service=service, options=options)

    if browser == "chrome":
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        options = webdriver.ChromeOptions()
        if binary_path:
            options.binary_location = binary_path
        if headless:
            try:
                options.add_argument('--headless=new')
            except Exception:
                options.add_argument('--headless')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--window-size=1280,720')

        service = Service(executable_path=driver_path)
        return webdriver.Chrome(service=service, options=options)

    raise ValueError(f"Unsupported browser: {browser}")
