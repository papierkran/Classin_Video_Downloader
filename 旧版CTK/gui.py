import os
import json
import threading
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import tempfile
import subprocess
import sys
import time

import videodownloader
import driver_auto
from tkinter import ttk


def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(_get_base_dir(), 'config.json')

BG_COLOR = '#f5f7fa'
CARD_BG = '#ffffff'
ACCENT = '#3182ce'
ACCENT_HOVER = '#2c5282'
TEXT_COLOR = '#2d3748'
TEXT_MUTED = '#718096'
BORDER_COLOR = '#e2e8f0'
SUCCESS_COLOR = '#38a169'
DANGER_COLOR = '#e53e3e'


def _make_entry(parent, **kw):
    e = tk.Entry(parent, **kw)
    e.config(font=('Microsoft YaHei UI', 10), relief='flat', highlightthickness=1,
             highlightbackground=BORDER_COLOR, highlightcolor=ACCENT, bd=0,
             bg='#fff', fg=TEXT_COLOR, insertbackground=ACCENT)
    return e


def _make_label(parent, text, **kw):
    return tk.Label(parent, text=text, fg=TEXT_COLOR, font=('Microsoft YaHei UI', 10), **kw)


def _make_btn(parent, text, command, style='primary', **kw):
    bg = ACCENT if style == 'primary' else CARD_BG
    fg = '#fff' if style == 'primary' else TEXT_COLOR
    btn = tk.Button(parent, text=text, command=command, font=('Microsoft YaHei UI', 10),
                    bg=bg, fg=fg, activebackground=ACCENT_HOVER if style == 'primary' else BORDER_COLOR,
                    activeforeground='#fff' if style == 'primary' else TEXT_COLOR,
                    relief='flat', bd=0, cursor='hand2', padx=14, pady=6, **kw)
    if style == 'danger':
        btn.config(bg=DANGER_COLOR, activebackground='#c53030')
    return btn


class App:
    def center_window(self, win, width, height):
        try:
            self.root.update_idletasks()
            win.update_idletasks()
            rx = self.root.winfo_rootx()
            ry = self.root.winfo_rooty()
            rw = self.root.winfo_width()
            rh = self.root.winfo_height()
            x = rx + (rw - width) // 2
            y = ry + (rh - height) // 2
            win.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            try:
                win.geometry(f"{width}x{height}")
            except Exception:
                pass

    def show_notification(self, title, message):
        """显示系统通知"""
        try:
            # Windows系统通知
            if sys.platform == 'win32':
                try:
                    from win10toast import ToastNotifier
                    toaster = ToastNotifier()
                    toaster.show_toast(title, message, duration=5, threaded=True)
                    return
                except ImportError:
                    pass
            
            # 跨平台方案：使用tkinter messagebox
            self.root.after(0, lambda: messagebox.showinfo(title, message))
        except Exception as e:
            print(f'显示通知失败: {e}')

    def execute_finish_action(self):
        """执行下载完成后的操作"""
        action = self.finish_action_var.get()
        if action == '无操作':
            return
        
        self.append_log(f'执行完成操作: {action}')
        
        if action == '关闭程序':
            self.root.after(2000, self.root.destroy)
        elif action == '休眠电脑':
            try:
                if sys.platform == 'win32':
                    subprocess.run(['rundll32.exe', 'powrprof.dll,SetSuspendState', '0,1,0'], check=True)
                else:
                    subprocess.run(['systemctl', 'suspend'], check=True)
            except Exception as e:
                self.append_log(f'休眠失败: {e}')
                messagebox.showerror('错误', f'休眠电脑失败: {e}')
        elif action == '关机':
            try:
                if sys.platform == 'win32':
                    subprocess.run(['shutdown', '/s', '/t', '60'], check=True)
                    messagebox.showinfo('提示', '电脑将在60秒后关机，如需取消请运行 shutdown /a')
                else:
                    subprocess.run(['shutdown', '-h', '+1'], check=True)
            except Exception as e:
                self.append_log(f'关机失败: {e}')
                messagebox.showerror('错误', f'关机失败: {e}')

    def __init__(self, root):
        self.root = root
        root.title('ClassIn 视频下载器')
        root.configure(bg=BG_COLOR)
        root.minsize(960, 680)
        root.geometry('1050x750')

        self.log_q = queue.Queue()
        self.total_items = 0
        self.success_count = 0
        self.fail_count = 0
        self._is_downloading = False
        self._download_done = False

        main = tk.Frame(root, bg=BG_COLOR, padx=16, pady=12)
        main.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(main, bg=BG_COLOR)
        header.pack(fill=tk.X, pady=(0, 10))
        tk.Label(header, text='ClassIn 视频下载器', font=('Microsoft YaHei UI', 16, 'bold'),
                 fg=ACCENT, bg=BG_COLOR).pack(side=tk.LEFT)
        tk.Label(header, text='批量下载课堂录播视频', font=('Microsoft YaHei UI', 9),
                 fg=TEXT_MUTED, bg=BG_COLOR).pack(side=tk.LEFT, padx=(10, 0), pady=4)

        config_card = tk.Frame(main, bg=CARD_BG, relief='flat', padx=14, pady=10)
        config_card.pack(fill=tk.X, pady=(0, 8))
        config_card.config(highlightbackground=BORDER_COLOR, highlightthickness=1)

        _make_label(config_card, '浏览器驱动').grid(row=0, column=0, sticky='w', pady=(0, 4))
        self.driver_entry = _make_entry(config_card, width=50)
        self.driver_entry.grid(row=0, column=1, padx=(10, 6), pady=(0, 4), sticky='we')
        driver_btn_frame = tk.Frame(config_card, bg=CARD_BG)
        driver_btn_frame.grid(row=0, column=2, pady=(0, 4), sticky='w')
        _make_btn(driver_btn_frame, '浏览', self.browse_driver, style='secondary').pack(side=tk.LEFT)
        _make_btn(driver_btn_frame, '自动检测驱动', self.auto_detect_driver, style='secondary').pack(side=tk.LEFT, padx=(4, 0))
        _make_btn(driver_btn_frame, '更新驱动', self.update_driver, style='secondary').pack(side=tk.LEFT, padx=(4, 0))

        _make_label(config_card, '保存目录').grid(row=1, column=0, sticky='w', pady=4)
        self.save_entry = _make_entry(config_card, width=50)
        self.save_entry.grid(row=1, column=1, padx=(10, 6), pady=4, sticky='we')
        _make_btn(config_card, '浏览', self.browse_save, style='secondary').grid(row=1, column=2, pady=4)

        _make_label(config_card, '数据文件').grid(row=2, column=0, sticky='w', pady=4)
        self.csv_entry = _make_entry(config_card, width=50)
        self.csv_entry.grid(row=2, column=1, padx=(10, 6), pady=4, sticky='we')
        _make_btn(config_card, '浏览', self.browse_csv, style='secondary').grid(row=2, column=2, pady=4)

        _make_label(config_card, 'Cookie').grid(row=3, column=0, sticky='nw', pady=(4, 0))
        self.cookie_text = tk.Text(config_card, height=3, width=50, font=('Consolas', 9),
                                    relief='flat', highlightthickness=1, highlightbackground=BORDER_COLOR,
                                    highlightcolor=ACCENT, bd=0, bg='#fff', fg=TEXT_COLOR,
                                    insertbackground=ACCENT, wrap=tk.WORD)
        self.cookie_text.grid(row=3, column=1, padx=(10, 6), pady=4, sticky='we')
        cookie_btn_frame = tk.Frame(config_card, bg=CARD_BG)
        cookie_btn_frame.grid(row=3, column=2, pady=4, sticky='n')
        _make_btn(cookie_btn_frame, '获取Cookie', self.get_cookie, style='secondary').pack(side=tk.LEFT)
        _make_btn(cookie_btn_frame, '验证Cookie', self.verify_cookie, style='secondary').pack(side=tk.LEFT, padx=(4, 0))

        _make_label(config_card, '下载线程数').grid(row=4, column=0, sticky='w', pady=4)
        self.threads_entry = _make_entry(config_card, width=50)
        self.threads_entry.grid(row=4, column=1, padx=(10, 6), pady=4, sticky='we')
        self.threads_entry.insert(0, '4')
        tk.Label(config_card, text='(1-8)', fg=TEXT_MUTED, font=('Microsoft YaHei UI', 9)).grid(row=4, column=2, pady=4, sticky='w', padx=6)

        _make_label(config_card, '速度限制').grid(row=5, column=0, sticky='w', pady=4)
        speed_frame = tk.Frame(config_card, bg=CARD_BG)
        speed_frame.grid(row=5, column=1, padx=(10, 6), pady=4, sticky='we')
        self.speed_limit_var = tk.StringVar(value='不限制')
        speed_options = ['不限制', '1 MB/s', '2 MB/s', '5 MB/s', '10 MB/s']
        speed_combo = ttk.Combobox(speed_frame, textvariable=self.speed_limit_var, values=speed_options, width=10, state='readonly')
        speed_combo.pack(side=tk.LEFT)
        tk.Label(config_card, text='(限制单个视频下载速度)', fg=TEXT_MUTED, font=('Microsoft YaHei UI', 9)).grid(row=5, column=2, pady=4, sticky='w', padx=6)

        _make_label(config_card, '完成操作').grid(row=6, column=0, sticky='w', pady=4)
        self.finish_action_var = tk.StringVar(value='无操作')
        finish_options = ['无操作', '关闭程序', '休眠电脑', '关机']
        finish_combo = ttk.Combobox(config_card, textvariable=self.finish_action_var, values=finish_options, width=10, state='readonly')
        finish_combo.grid(row=6, column=1, padx=(10, 6), pady=4, sticky='w')
        tk.Label(config_card, text='(下载全部完成后执行)', fg=TEXT_MUTED, font=('Microsoft YaHei UI', 9)).grid(row=6, column=2, pady=4, sticky='w', padx=6)

        self.skip_existing_var = tk.BooleanVar(value=True)
        skip_check = tk.Checkbutton(config_card, text='跳过已存在的文件', variable=self.skip_existing_var,
                                     bg=CARD_BG, fg=TEXT_COLOR, font=('Microsoft YaHei UI', 10),
                                     selectcolor=ACCENT, activebackground=CARD_BG)
        skip_check.grid(row=7, column=1, padx=(10, 6), pady=4, sticky='w')

        config_card.columnconfigure(1, weight=1)

        action_frame = tk.Frame(main, bg=BG_COLOR)
        action_frame.pack(fill=tk.X, pady=(0, 8))
        self.save_btn = _make_btn(action_frame, '保存配置', self.save_config, style='secondary')
        self.save_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.start_btn = _make_btn(action_frame, '开始下载', self.start_download, style='primary')
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.pause_btn = _make_btn(action_frame, '暂停', self.toggle_pause, style='secondary')
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = _make_btn(action_frame, '停止', self.stop_download, style='danger')
        self.stop_btn.pack(side=tk.LEFT)
        
        # 配置导入/导出按钮
        config_btn_frame = tk.Frame(action_frame, bg=BG_COLOR)
        config_btn_frame.pack(side=tk.RIGHT)
        _make_btn(config_btn_frame, '导出配置', self.export_config, style='secondary').pack(side=tk.LEFT, padx=(0, 6))
        _make_btn(config_btn_frame, '导入配置', self.import_config, style='secondary').pack(side=tk.LEFT)

        progress_frame = tk.Frame(main, bg=BG_COLOR)
        progress_frame.pack(fill=tk.X, pady=(0, 6))
        row1 = tk.Frame(progress_frame, bg=BG_COLOR)
        row1.pack(fill=tk.X)
        self.video_progress_label = _make_label(row1, '当前视频: 0% | 0.00 MB/s')
        self.video_progress_label.pack(side=tk.LEFT, padx=(0, 10))
        self.video_progress_var = tk.IntVar()
        pb_style = ttk.Style()
        try:
            pb_style.configure('Video.Horizontal.TProgressbar', troughcolor=BORDER_COLOR, background=ACCENT)
            video_style = 'Video.Horizontal.TProgressbar'
        except tk.TclError:
            video_style = 'Horizontal.TProgressbar'
        self.video_progress_bar = ttk.Progressbar(row1, variable=self.video_progress_var, maximum=100,
                                                  style=video_style, length=300)
        self.video_progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        row2 = tk.Frame(progress_frame, bg=BG_COLOR)
        row2.pack(fill=tk.X, pady=(4, 0))
        self.total_progress_label = _make_label(row2, '总进度: 0/0 | 成功 0 失败 0')
        self.total_progress_label.pack(side=tk.LEFT, padx=(0, 10))
        self.total_canvas = tk.Canvas(row2, height=14, bg=BORDER_COLOR, highlightthickness=0)
        self.total_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.total_canvas.bind('<Configure>', lambda e: self._redraw_total_bar())
        row1.pack_forget()
        row2.pack_forget()
        self._progress_row1 = row1
        self._progress_row2 = row2

        list_frame = tk.Frame(main, bg=BG_COLOR)
        list_frame.pack(fill=tk.BOTH, expand=True)
        list_header = tk.Frame(list_frame, bg=BG_COLOR)
        list_header.pack(fill=tk.X, pady=(0, 4))
        _make_label(list_header, '任务列表').pack(side=tk.LEFT)
        
        # 排序和筛选控件
        filter_frame = tk.Frame(list_header, bg=BG_COLOR)
        filter_frame.pack(side=tk.RIGHT)
        
        _make_label(filter_frame, '排序:').pack(side=tk.LEFT, padx=(0, 4))
        self.sort_var = tk.StringVar(value='默认')
        sort_options = ['默认', '班级ID', '课堂ID', '开课时间', '状态']
        sort_combo = ttk.Combobox(filter_frame, textvariable=self.sort_var, values=sort_options, width=8, state='readonly')
        sort_combo.pack(side=tk.LEFT, padx=(0, 4))
        sort_combo.bind('<<ComboboxSelected>>', self.on_sort_change)
        
        _make_label(filter_frame, '筛选:').pack(side=tk.LEFT, padx=(10, 4))
        self.filter_var = tk.StringVar(value='全部')
        filter_options = ['全部', '待下载', '下载中', '已完成', '失败']
        filter_combo = ttk.Combobox(filter_frame, textvariable=self.filter_var, values=filter_options, width=8, state='readonly')
        filter_combo.pack(side=tk.LEFT)
        filter_combo.bind('<<ComboboxSelected>>', self.on_filter_change)
        
        # 使用PanedWindow分割任务列表和日志区域
        paned = ttk.PanedWindow(list_frame, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)
        
        # 任务列表区域
        table_frame = tk.Frame(paned, bg=BG_COLOR)
        table_card = tk.Frame(table_frame, bg=CARD_BG, relief='flat')
        table_card.pack(fill=tk.BOTH, expand=True)
        table_card.config(highlightbackground=BORDER_COLOR, highlightthickness=1)

        columns = ('course_id', 'lesson_id', 'class_name', 'lesson_name', 'start_time', 'status', 'message')
        self.tree = ttk.Treeview(table_card, columns=columns, show='headings', height=6)
        self.tree.heading('course_id', text='班级ID')
        self.tree.heading('lesson_id', text='课堂ID')
        self.tree.heading('class_name', text='班级名称')
        self.tree.heading('lesson_name', text='课堂名称')
        self.tree.heading('start_time', text='开课时间')
        self.tree.heading('status', text='状态')
        self.tree.heading('message', text='消息')
        self.tree.column('course_id', width=100, anchor='w')
        self.tree.column('lesson_id', width=100, anchor='w')
        self.tree.column('class_name', width=120, anchor='w')
        self.tree.column('lesson_name', width=120, anchor='w')
        self.tree.column('start_time', width=130, anchor='w')
        self.tree.column('status', width=70, anchor='center')
        self.tree.column('message', width=180, anchor='w')
        vsb = ttk.Scrollbar(table_card, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.tag_configure('pending', background='#ffffff')
        self.tree.tag_configure('downloading', background='#FFF3CD')
        self.tree.tag_configure('success', background='#D1E7DD')
        self.tree.tag_configure('failed', background='#F8D7DA')

        table_actions = tk.Frame(table_frame, bg=BG_COLOR)
        table_actions.pack(fill=tk.X, pady=(6, 6))
        _make_btn(table_actions, '导入CSV/XLSX', self.import_table_file, style='secondary').pack(side=tk.LEFT, padx=(0, 6))
        _make_btn(table_actions, '新增行', self.add_row_dialog, style='secondary').pack(side=tk.LEFT, padx=(0, 6))
        _make_btn(table_actions, '编辑行', self.edit_selected_row_dialog, style='secondary').pack(side=tk.LEFT, padx=(0, 6))
        _make_btn(table_actions, '删除行', self.delete_selected_rows, style='danger').pack(side=tk.LEFT)
        
        paned.add(table_frame, weight=2)

        # 日志区域
        log_frame = tk.Frame(paned, bg=BG_COLOR)
        log_header = tk.Frame(log_frame, bg=BG_COLOR)
        log_header.pack(fill=tk.X, pady=(0, 4))
        _make_label(log_header, '任务日志').pack(side=tk.LEFT)
        _make_btn(log_header, '导出日志', self.export_log, style='secondary').pack(side=tk.RIGHT)
        log_card = tk.Frame(log_frame, bg=CARD_BG, relief='flat')
        log_card.pack(fill=tk.BOTH, expand=True)
        log_card.config(highlightbackground=BORDER_COLOR, highlightthickness=1)
        self.log_area = ScrolledText(log_card, height=8, width=80, state='disabled',
                                      font=('Consolas', 9), bg='#fafafa', fg=TEXT_COLOR,
                                      relief='flat', padx=10, pady=8, wrap=tk.WORD)
        self.log_area.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        
        paned.add(log_frame, weight=1)

        self.load_config()
        self.auto_load_videodata_folder()
        self.root.after(200, self.flush_log_queue)

    def browse_driver(self):
        f = filedialog.askopenfilename(filetypes=[('Driver', '*.exe'), ('All Files', '*.*')])
        if f:
            self.driver_entry.delete(0, tk.END)
            self.driver_entry.insert(0, f)

    def auto_detect_driver(self):
        base_dir = _get_base_dir()
        self.append_log('正在检测浏览器...')
        browser, bin_path = driver_auto.detect_browser_preference()
        if not browser:
            messagebox.showerror('错误', '未检测到 Microsoft Edge 或 Chrome 浏览器，请手动指定驱动路径。')
            return
        self.append_log(f'检测到浏览器: {browser} ({bin_path})')

        default_name = 'msedgedriver.exe' if browser == 'edge' else 'chromedriver.exe'
        existing = os.path.join(base_dir, default_name)
        if os.path.exists(existing):
            self.driver_entry.delete(0, tk.END)
            self.driver_entry.insert(0, existing)
            self.save_config(silent=True)
            self.append_log(f'已存在驱动: {existing}')
            messagebox.showinfo('成功', f'已检测到驱动文件:\n{existing}')
            return

        def do_download():
            try:
                self.append_log(f'正在下载 {browser} 驱动，请稍候...')
                driver_path = driver_auto.ensure_driver(browser, base_dir=base_dir)
                if driver_path and os.path.exists(driver_path):
                    self.root.after(0, lambda: self.driver_entry.delete(0, tk.END))
                    self.root.after(0, lambda: self.driver_entry.insert(0, driver_path))
                    self.root.after(0, lambda: self.save_config(silent=True))
                    self.append_log(f'驱动已就绪: {driver_path}')
                    self.root.after(0, lambda: messagebox.showinfo('成功', f'驱动已下载到:\n{driver_path}'))
                else:
                    self.append_log('驱动下载失败')
                    self.root.after(0, lambda: messagebox.showerror('错误', '驱动下载失败，请手动指定驱动路径。'))
            except Exception as e:
                self.append_log(f'驱动下载异常: {e}')
                self.root.after(0, lambda e=e: messagebox.showerror('错误', f'驱动下载失败: {e}\n请手动指定驱动路径。'))

        threading.Thread(target=do_download, daemon=True).start()

    def update_driver(self):
        base_dir = _get_base_dir()
        self.append_log('正在检测浏览器...')
        browser, bin_path = driver_auto.detect_browser_preference()
        if not browser:
            messagebox.showerror('错误', '未检测到 Microsoft Edge 或 Chrome 浏览器。')
            return
        self.append_log(f'检测到浏览器: {browser} ({bin_path})')

        def do_update():
            try:
                self.append_log(f'正在下载最新 {browser} 驱动，请稍候...')
                driver_path = driver_auto.ensure_driver(browser, base_dir=base_dir)
                if driver_path and os.path.exists(driver_path):
                    self.root.after(0, lambda: self.driver_entry.delete(0, tk.END))
                    self.root.after(0, lambda: self.driver_entry.insert(0, driver_path))
                    self.root.after(0, lambda: self.save_config(silent=True))
                    self.append_log(f'驱动已更新: {driver_path}')
                    self.root.after(0, lambda: messagebox.showinfo('成功', f'驱动已更新到最新版本:\n{driver_path}'))
                else:
                    self.append_log('驱动下载失败')
                    self.root.after(0, lambda: messagebox.showerror('错误', '驱动下载失败，请手动指定驱动路径。'))
            except Exception as e:
                self.append_log(f'驱动更新异常: {e}')
                self.root.after(0, lambda e=e: messagebox.showerror('错误', f'驱动更新失败: {e}\n请手动指定驱动路径。'))

        threading.Thread(target=do_update, daemon=True).start()

    def browse_save(self):
        d = filedialog.askdirectory()
        if d:
            self.save_entry.delete(0, tk.END)
            self.save_entry.insert(0, d)

    def browse_csv(self):
        f = filedialog.askopenfilename(filetypes=[('CSV/XLSX Files', '*.csv;*.xlsx;*.xls'), ('All Files', '*.*')])
        if f:
            self.csv_entry.delete(0, tk.END)
            self.csv_entry.insert(0, f)
            self.load_table_from_file(f)

    def save_config(self, silent=False):
        cfg = {
            'driver_path': self.driver_entry.get().strip(),
            'save_dir': self.save_entry.get().strip(),
            'csv_file': self.csv_entry.get().strip(),
            'cookie': self.cookie_text.get('1.0', tk.END).strip(),
            'max_workers': self.threads_entry.get().strip() or '4',
            'speed_limit': self.speed_limit_var.get(),
            'finish_action': self.finish_action_var.get(),
            'skip_existing': self.skip_existing_var.get()
        }
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    old = json.load(f)
                for key in ['last_success', 'download_history', 'last_update_time']:
                    if key in old:
                        cfg[key] = old[key]
        except Exception:
            pass
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            if not silent:
                messagebox.showinfo('保存', '配置已保存到 config.json')
        except Exception as e:
            if not silent:
                messagebox.showerror('错误', f'保存配置失败: {e}')

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                self.driver_entry.delete(0, tk.END)
                self.driver_entry.insert(0, cfg.get('driver_path', ''))
                self.save_entry.delete(0, tk.END)
                self.save_entry.insert(0, cfg.get('save_dir', ''))
                self.csv_entry.delete(0, tk.END)
                self.csv_entry.insert(0, cfg.get('csv_file', ''))
                self.cookie_text.delete('1.0', tk.END)
                self.cookie_text.insert('1.0', cfg.get('cookie', ''))
                self.threads_entry.delete(0, tk.END)
                self.threads_entry.insert(0, cfg.get('max_workers', '4'))
                self.speed_limit_var.set(cfg.get('speed_limit', '不限制'))
                self.finish_action_var.set(cfg.get('finish_action', '无操作'))
                self.skip_existing_var.set(cfg.get('skip_existing', True))
            except Exception:
                pass

    def export_config(self):
        """导出配置到文件"""
        try:
            cfg = {
                'driver_path': self.driver_entry.get().strip(),
                'save_dir': self.save_entry.get().strip(),
                'csv_file': self.csv_entry.get().strip(),
                'cookie': self.cookie_text.get('1.0', tk.END).strip(),
                'max_workers': self.threads_entry.get().strip() or '4'
            }
            
            default_filename = f'配置备份_{time.strftime("%Y%m%d_%H%M%S")}.json'
            
            f = filedialog.asksaveasfilename(
                defaultextension='.json',
                filetypes=[('JSON文件', '*.json'), ('所有文件', '*.*')],
                initialfile=default_filename
            )
            
            if f:
                with open(f, 'w', encoding='utf-8') as file:
                    json.dump(cfg, file, ensure_ascii=False, indent=2)
                messagebox.showinfo('成功', f'配置已导出到:\n{f}')
                self.append_log(f'配置已导出: {f}')
        except Exception as e:
            messagebox.showerror('错误', f'导出配置失败: {e}')
            self.append_log(f'导出配置失败: {e}')

    def import_config(self):
        """从文件导入配置"""
        try:
            f = filedialog.askopenfilename(
                filetypes=[('JSON文件', '*.json'), ('所有文件', '*.*')]
            )
            
            if f:
                with open(f, 'r', encoding='utf-8') as file:
                    cfg = json.load(file)
                
                # 更新界面
                self.driver_entry.delete(0, tk.END)
                self.driver_entry.insert(0, cfg.get('driver_path', ''))
                self.save_entry.delete(0, tk.END)
                self.save_entry.insert(0, cfg.get('save_dir', ''))
                self.csv_entry.delete(0, tk.END)
                self.csv_entry.insert(0, cfg.get('csv_file', ''))
                self.cookie_text.delete('1.0', tk.END)
                self.cookie_text.insert('1.0', cfg.get('cookie', ''))
                self.threads_entry.delete(0, tk.END)
                self.threads_entry.insert(0, cfg.get('max_workers', '4'))
                
                messagebox.showinfo('成功', f'配置已从文件导入:\n{f}')
                self.append_log(f'配置已导入: {f}')
        except Exception as e:
            messagebox.showerror('错误', f'导入配置失败: {e}')
            self.append_log(f'导入配置失败: {e}')

    def auto_load_videodata_folder(self):
        folder = os.path.join(_get_base_dir(), 'videodata')
        if not os.path.isdir(folder):
            return
        candidates = []
        for fn in os.listdir(folder):
            if fn.lower().endswith(('.xlsx', '.xls', '.csv')):
                full = os.path.join(folder, fn)
                if os.path.isfile(full):
                    candidates.append(full)
        if not candidates:
            return
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        target = candidates[0]
        self.csv_entry.delete(0, tk.END)
        self.csv_entry.insert(0, target)
        self.load_table_from_file(target)
        self.append_log(f'已默认加载：{target}')

    def import_table_file(self):
        f = filedialog.askopenfilename(filetypes=[('CSV/XLSX Files', '*.csv;*.xlsx;*.xls'), ('All Files', '*.*')])
        if f:
            self.csv_entry.delete(0, tk.END)
            self.csv_entry.insert(0, f)
            self.load_table_from_file(f)

    def _clear_table(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)

    def _insert_course_row(self, course: dict, status='pending', message=''):
        values = (
            course.get('course_id', ''), course.get('lesson_id', ''), course.get('class_name', ''),
            course.get('lesson_name', ''), course.get('start_time', ''), status, message
        )
        return self.tree.insert('', tk.END, values=values, tags=(status,))

    def load_table_from_file(self, path):
        try:
            courses = self._load_courses_any(path)
        except Exception as e:
            messagebox.showerror('错误', f'读取失败: {e}')
            return
        self._clear_table()
        for c in courses:
            self._insert_course_row(c)
        self.append_log(f'已加载 {len(courses)} 条任务')

    def _load_courses_any(self, path):
        if path.lower().endswith('.csv'):
            return videodownloader.load_courses_from_csv(path)
        return self._load_courses_from_xlsx(path)

    def _load_courses_from_xlsx(self, path):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(x).strip().lstrip('\ufeff') if x is not None else '' for x in rows[0]]
        out = []
        for r in rows[1:]:
            row = {headers[i]: ('' if i >= len(r) or r[i] is None else str(r[i]).strip()) for i in range(len(headers))}
            lesson_id = (row.get('课堂ID') or '').strip()
            if not lesson_id:
                continue
            out.append({
                'lesson_id': lesson_id,
                'course_id': (row.get('班级ID') or '').strip(),
                'lesson_name': (row.get('课堂名称') or '').strip(),
                'class_name': (row.get('班级名称') or '').strip(),
                'start_time': (row.get('开课时间') or '').strip(),
                'creation_time': (row.get('创建时间') or '').strip(),
            })
        return out

    def add_row_dialog(self):
        self._row_dialog('新增行')

    def edit_selected_row_dialog(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning('提示', '请先选中一行')
            return
        iid = sel[0]
        vals = self.tree.item(iid, 'values')
        init = {'course_id': vals[0], 'lesson_id': vals[1], 'class_name': vals[2], 'lesson_name': vals[3], 'start_time': vals[4]}
        self._row_dialog('编辑行', iid=iid, init=init)

    def _row_dialog(self, title, iid=None, init=None):
        init = init or {}
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG_COLOR)
        self.center_window(win, 460, 320)
        frm = tk.Frame(win, bg=BG_COLOR, padx=18, pady=14)
        frm.pack(fill=tk.BOTH, expand=True)
        fields = [('班级ID(course_id)', 'course_id'), ('课堂ID(lesson_id)', 'lesson_id'), ('班级名称(class_name)', 'class_name'), ('课堂名称(lesson_name)', 'lesson_name'), ('开课时间(start_time)', 'start_time')]
        entries = {}
        for i, (label, key) in enumerate(fields):
            _make_label(frm, label, bg=BG_COLOR).grid(row=i, column=0, sticky='w', pady=8)
            e = _make_entry(frm, width=36)
            e.grid(row=i, column=1, sticky='we', pady=8)
            e.insert(0, init.get(key, ''))
            entries[key] = e
        frm.columnconfigure(1, weight=1)

        def on_ok():
            course = {k: entries[k].get().strip() for _, k in fields}
            if not course.get('course_id') or not course.get('lesson_id'):
                messagebox.showwarning('提示', '班级ID 和 课堂ID 为必填')
                return
            values = (course['course_id'], course['lesson_id'], course['class_name'], course['lesson_name'], course['start_time'], 'pending', '')
            if iid is None:
                self.tree.insert('', tk.END, values=values, tags=('pending',))
            else:
                self.tree.item(iid, values=values, tags=('pending',))
            win.destroy()

        btns = tk.Frame(frm, bg=BG_COLOR)
        btns.grid(row=len(fields), column=0, columnspan=2, pady=(18, 0), sticky='e')
        _make_btn(btns, '取消', win.destroy, style='secondary').pack(side=tk.RIGHT, padx=(8, 0))
        _make_btn(btns, '确定', on_ok, style='primary').pack(side=tk.RIGHT)

    def delete_selected_rows(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning('提示', '请先选中要删除的行')
            return
        if not messagebox.askyesno('确认', f'确认删除选中的 {len(sel)} 行吗？'):
            return
        for iid in sel:
            self.tree.delete(iid)

    def on_sort_change(self, event=None):
        """排序变更回调"""
        sort_key = self.sort_var.get()
        if sort_key == '默认':
            return
        
        # 获取所有项目
        items = []
        for iid in self.tree.get_children():
            values = self.tree.item(iid, 'values')
            items.append((iid, values))
        
        # 根据排序键进行排序
        if sort_key == '班级ID':
            items.sort(key=lambda x: x[1][0] if x[1][0] else '')
        elif sort_key == '课堂ID':
            items.sort(key=lambda x: x[1][1] if x[1][1] else '')
        elif sort_key == '开课时间':
            items.sort(key=lambda x: x[1][4] if x[1][4] else '')
        elif sort_key == '状态':
            status_order = {'pending': 0, 'downloading': 1, 'success': 2, 'failed': 3}
            items.sort(key=lambda x: status_order.get(x[1][5], 4))
        
        # 重新排列项目
        for index, (iid, values) in enumerate(items):
            self.tree.move(iid, '', index)
        
        self.append_log(f'任务列表已按 {sort_key} 排序')

    def on_filter_change(self, event=None):
        """筛选变更回调"""
        filter_key = self.filter_var.get()
        
        # 显示所有项目
        for iid in self.tree.get_children():
            self.tree.reattach(iid, '', 'end')
        
        if filter_key == '全部':
            return
        
        # 根据筛选条件隐藏项目
        filter_map = {
            '待下载': 'pending',
            '下载中': 'downloading',
            '已完成': 'success',
            '失败': 'failed'
        }
        
        target_status = filter_map.get(filter_key)
        if target_status:
            for iid in self.tree.get_children():
                values = self.tree.item(iid, 'values')
                status = values[5] if len(values) > 5 else ''
                if status != target_status:
                    self.tree.detach(iid)
        
        self.append_log(f'任务列表已筛选: {filter_key}')

    def set_row_status(self, course_index, status, message=''):
        iids = self.tree.get_children()
        if course_index <= 0 or course_index > len(iids):
            return
        iid = iids[course_index - 1]
        vals = list(self.tree.item(iid, 'values'))
        vals[5] = status
        vals[6] = message
        self.tree.item(iid, values=vals, tags=(status,))

    def collect_courses_from_table(self):
        courses = []
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, 'values')
            courses.append({'course_id': vals[0], 'lesson_id': vals[1], 'class_name': vals[2], 'lesson_name': vals[3], 'start_time': vals[4]})
        return courses

    def append_log(self, msg):
        self.log_area.configure(state='normal')
        self.log_area.insert(tk.END, str(msg) + '\n')
        self.log_area.see(tk.END)
        self.log_area.configure(state='disabled')

    def export_log(self):
        """导出日志到文件"""
        try:
            log_content = self.log_area.get('1.0', tk.END).strip()
            if not log_content:
                messagebox.showwarning('提示', '日志内容为空')
                return
            
            # 生成默认文件名
            default_filename = f'下载日志_{time.strftime("%Y%m%d_%H%M%S")}.txt'
            
            f = filedialog.asksaveasfilename(
                defaultextension='.txt',
                filetypes=[('文本文件', '*.txt'), ('所有文件', '*.*')],
                initialfile=default_filename
            )
            
            if f:
                with open(f, 'w', encoding='utf-8') as file:
                    file.write(log_content)
                messagebox.showinfo('成功', f'日志已导出到:\n{f}')
                self.append_log(f'日志已导出: {f}')
        except Exception as e:
            messagebox.showerror('错误', f'导出日志失败: {e}')
            self.append_log(f'导出日志失败: {e}')

    def flush_log_queue(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.append_log(msg)
        except queue.Empty:
            pass
        if getattr(self, '_download_done', False) and self._is_downloading:
            self.start_btn.config(state='normal')
            self.save_btn.config(state='normal')
            if hasattr(self, 'driver_ref'):
                self.driver_ref.clear()
            self._is_downloading = False
        interval = 100 if self._is_downloading else 1000
        self.root.after(interval, self.flush_log_queue)

    def toggle_pause(self):
        if not hasattr(self, 'pause_event'):
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text='暂停')
            self.append_log('已继续下载')
        else:
            self.pause_event.set()
            self.pause_btn.config(text='继续')
            self.append_log('已暂停下载')

    def stop_download(self):
        if not hasattr(self, 'stop_event'):
            return
        if not self._is_downloading:
            return

        self.stop_event.set()
        self.append_log('已发送停止信号，等待线程退出...')
        self._download_done = True
        
        # 重置暂停事件状态
        if hasattr(self, 'pause_event'):
            self.pause_event.clear()
            self.pause_btn.config(text='暂停')
        
        # 立即重置UI状态，不等线程退出
        self._is_downloading = False
        self.start_btn.config(state='normal')
        self.save_btn.config(state='normal')
        self.video_progress_var.set(0)
        self.video_progress_label.config(text='当前视频: 0% | 0.00 MB/s')
        self.total_progress_label.config(text='总进度: 0/0 | 成功 0 失败 0')
        self._progress_row1.pack_forget()
        self._progress_row2.pack_forget()
        self.total_items = 0
        self.success_count = 0
        self.fail_count = 0
        self._redraw_total_bar()
        
        # 延迟关闭driver，给下载线程一点时间退出
        def close_driver_later():
            if hasattr(self, 'driver_ref') and self.driver_ref:
                try:
                    driver = self.driver_ref.get('driver')
                    if driver is not None:
                        driver.quit()
                        self.driver_ref['driver'] = None
                        self.append_log('已关闭浏览器')
                except Exception as e:
                    self.append_log(f'关闭浏览器时出错: {e}')
            self.driver_ref = {}
        
        self.root.after(1000, close_driver_later)

    def _redraw_total_bar(self):
        try:
            w = int(self.total_canvas.winfo_width())
            h = int(self.total_canvas.winfo_height())
            if w <= 2 or h <= 2:
                return
            total = max(int(self.total_items or 0), 1)
            succ = min(max(int(self.success_count or 0), 0), total)
            fail = min(max(int(self.fail_count or 0), 0), total - succ)
            succ_w = int(w * (succ / total))
            fail_w = int(w * (fail / total))
            self.total_canvas.delete('all')
            self.total_canvas.create_rectangle(0, 0, w, h, fill='#E2E8F0', outline='#CBD5E0')
            if succ_w > 0:
                self.total_canvas.create_rectangle(0, 0, succ_w, h, fill=SUCCESS_COLOR, outline=SUCCESS_COLOR)
            if fail_w > 0:
                self.total_canvas.create_rectangle(succ_w, 0, succ_w + fail_w, h, fill=DANGER_COLOR, outline=DANGER_COLOR)
        except Exception:
            pass

    def update_progress(self, total_items, done_count, success_count, fail_count):
        try:
            self.total_items = max(self.total_items, int(total_items or 0))
            self.success_count = int(success_count or 0)
            self.fail_count = int(fail_count or 0)
            done = int(done_count or 0)
            self._progress_row1.pack(fill=tk.X)
            self._progress_row2.pack(fill=tk.X, pady=(6, 0))
            self.total_progress_label.config(text=f'总进度: {done}/{self.total_items} | 成功 {self.success_count} 失败 {self.fail_count}')
            self._redraw_total_bar()
        except Exception:
            pass

    def update_video_progress(self, percent, speed_mbps, info=''):
        try:
            self._progress_row1.pack(fill=tk.X)
            p = int(max(0, min(100, percent or 0)))
            self.video_progress_var.set(p)
            sp = float(speed_mbps or 0.0)
            text = f'当前视频: {p}% | {sp:.2f} MB/s'
            if info:
                text += f' | {info}'
            self.video_progress_label.config(text=text)
        except Exception:
            pass

    def get_cookie(self):
        cookie_file = os.path.join(tempfile.gettempdir(), 'classin_cookies.json')
        driver_path = self.driver_entry.get().strip() or None

        browser, bin_path = driver_auto.detect_browser_preference()
        if not browser:
            messagebox.showerror('错误', '未检测到 Microsoft Edge 或 Chrome 浏览器，请先点击"自动检测驱动"。')
            return

        base_dir = _get_base_dir()
        default_name = 'msedgedriver.exe' if browser == 'edge' else 'chromedriver.exe'
        if not driver_path or not os.path.exists(driver_path):
            driver_path = os.path.join(base_dir, default_name)
        if not os.path.exists(driver_path):
            messagebox.showwarning('提示', '未找到驱动文件，请先点击"自动检测驱动"下载驱动。')
            return

        import_str = 'from selenium.webdriver.edge.service import Service' if browser == 'edge' else 'from selenium.webdriver.chrome.service import Service'
        driver_class = 'webdriver.Edge' if browser == 'edge' else 'webdriver.Chrome'

        script = (
            f'import json\n'
            f'import time\n'
            f'from selenium import webdriver\n'
            f'{import_str}\n'
            f'service = Service(executable_path=r"{driver_path}")\n'
            f'driver = {driver_class}(service=service)\n'
            f'driver.get("https://www.eeo.cn/cn/login")\n'
            f'print("请登录后等待保存 cookie")\n'
            f'time.sleep(40)\n'
            f'cookies = driver.get_cookies()\n'
            f'driver.quit()\n'
            f'with open(r"{cookie_file}", "w", encoding="utf-8") as f:\n'
            f'    json.dump(cookies, f, ensure_ascii=False)\n'
            f'print("cookie 已保存")\n'
        )
        tmp_py = os.path.join(tempfile.gettempdir(), 'get_cookie_tmp.py')
        with open(tmp_py, 'w', encoding='utf-8') as f:
            f.write(script)
        try:
            subprocess.Popen([sys.executable, tmp_py], creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, 'CREATE_NEW_CONSOLE') else 0)
        except Exception:
            subprocess.Popen([sys.executable, tmp_py])
        messagebox.showinfo('操作提示', '已弹出浏览器，请在新窗口登录后等待自动保存 Cookie。登录完成后点击"导入Cookie"按钮。')

        def import_cookie():
            if os.path.exists(cookie_file):
                try:
                    with open(cookie_file, 'r', encoding='utf-8') as f:
                        cookies = json.load(f)
                    cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
                    self.cookie_text.delete('1.0', tk.END)
                    self.cookie_text.insert('1.0', cookie_str)
                    messagebox.showinfo('成功', 'Cookie 已导入并填入文本框！')
                except Exception as e:
                    messagebox.showerror('错误', f'导入Cookie失败: {e}')
            else:
                messagebox.showwarning('未找到', '未检测到 cookie 文件，请确认已登录并等待脚本完成。')

        import_win = tk.Toplevel(self.root)
        import_win.title('导入 Cookie')
        import_win.configure(bg=BG_COLOR)
        self.center_window(import_win, 360, 140)
        tk.Label(import_win, text='登录完成后点击下方按钮导入 Cookie：', font=('Microsoft YaHei UI', 10), fg=TEXT_COLOR, bg=BG_COLOR).pack(padx=24, pady=20)
        _make_btn(import_win, '导入 Cookie', import_cookie, style='primary').pack(pady=(0, 20))

    def verify_cookie(self):
        """验证Cookie是否有效"""
        cookie = self.cookie_text.get('1.0', tk.END).strip()
        if not cookie:
            messagebox.showwarning('提示', '请先输入或获取Cookie')
            return
        
        self.append_log('正在验证Cookie有效性...')
        
        def do_verify():
            try:
                is_valid, message = videodownloader.check_cookie_validity(cookie, log_callback=self.append_log)
                if is_valid:
                    self.root.after(0, lambda: messagebox.showinfo('验证结果', f'Cookie有效！\n{message}'))
                    self.root.after(0, lambda: self.append_log('Cookie验证通过'))
                else:
                    self.root.after(0, lambda: messagebox.showwarning('验证结果', f'Cookie无效！\n{message}'))
                    self.root.after(0, lambda: self.append_log(f'Cookie验证失败: {message}'))
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror('错误', f'验证过程出错: {e}'))
                self.root.after(0, lambda e=e: self.append_log(f'Cookie验证异常: {e}'))
        
        threading.Thread(target=do_verify, daemon=True).start()

    def parse_speed_limit(self, speed_str):
        """解析速度限制字符串，返回每秒字节数"""
        if speed_str == '不限制':
            return 0
        try:
            if 'MB/s' in speed_str:
                mbps = float(speed_str.replace(' MB/s', ''))
                return int(mbps * 1024 * 1024)
        except Exception:
            pass
        return 0

    def validate_inputs(self):
        """验证输入参数"""
        errors = []
        
        driver_path = self.driver_entry.get().strip()
        if driver_path and not os.path.exists(driver_path):
            errors.append('指定的驱动文件不存在')
        
        save_dir = self.save_entry.get().strip()
        if save_dir and len(save_dir) > 260:
            errors.append('保存目录路径过长')
        
        csv_file = self.csv_entry.get().strip()
        if csv_file and not os.path.exists(csv_file):
            errors.append('指定的数据文件不存在')
        
        cookie = self.cookie_text.get('1.0', tk.END).strip()
        if not cookie:
            errors.append('Cookie不能为空')
        
        max_workers_str = self.threads_entry.get().strip()
        if max_workers_str:
            try:
                max_workers = int(max_workers_str)
                if max_workers < 1 or max_workers > 8:
                    errors.append('下载线程数必须在1-8之间')
            except ValueError:
                errors.append('下载线程数必须是数字')
        
        return errors

    def start_download(self):
        print('[DEBUG] start_download 被调用')
        # 防止多次快速点击导致多个下载同时进行
        if hasattr(self, '_is_downloading') and self._is_downloading:
            messagebox.showwarning('提示', '下载正在进行中，请勿重复点击')
            return
        
        # 验证输入
        print('[DEBUG] 验证输入...')
        errors = self.validate_inputs()
        if errors:
            messagebox.showerror('输入错误', '请修正以下问题:\n' + '\n'.join(errors))
            return
        
        print('[DEBUG] 读取配置...')
        driver_path = self.driver_entry.get().strip() or None
        save_dir = self.save_entry.get().strip() or None
        csv_file = self.csv_entry.get().strip() or None
        cookie = self.cookie_text.get('1.0', tk.END).strip() or ''
        max_workers_str = self.threads_entry.get().strip() or '4'
        speed_limit = self.parse_speed_limit(self.speed_limit_var.get())
        skip_existing = self.skip_existing_var.get()
        finish_action = self.finish_action_var.get()
        
        try:
            max_workers = max(1, min(8, int(max_workers_str)))
        except ValueError:
            max_workers = 4
            self.threads_entry.delete(0, tk.END)
            self.threads_entry.insert(0, '4')
        
        # 验证Cookie是否为空
        if not cookie:
            messagebox.showwarning('提示', 'Cookie不能为空，请先获取或输入Cookie')
            return
        
        courses_override = self.collect_courses_from_table()
        if not courses_override:
            messagebox.showwarning('提示', '任务列表为空，请先导入或新增任务行')
            return
        self.save_config(silent=True)
        self.start_btn.config(state='disabled')
        self.save_btn.config(state='disabled')
        self._is_downloading = True
        self.total_items = len(courses_override)
        self.success_count = 0
        self.fail_count = 0
        self.update_progress(self.total_items, 0, 0, 0)
        self.update_video_progress(0, 0.0, '')

        def log_cb(msg):
            self.log_q.put(msg)

        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.driver_ref = {}
        self._download_session = getattr(self, '_download_session', 0) + 1

        def progress_cb(total_courses, current_course_index, course, video_index):
            try:
                if isinstance(video_index, dict):
                    self.root.after(0, lambda: self.update_video_progress(video_index.get('percent', 0), video_index.get('speed_mbps', 0.0), video_index.get('info', '')))
                    return
                done = self.success_count + self.fail_count
                self.root.after(0, lambda: self.update_progress(self.total_items or total_courses, done, self.success_count, self.fail_count))
            except Exception:
                pass

        def lesson_done_cb(info):
            try:
                cfg = {}
                if os.path.exists(CONFIG_PATH):
                    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                        cfg = json.load(f)
                cfg['last_success'] = {
                    'course_id': info.get('course_id'), 'lesson_id': info.get('lesson_id'),
                    'course_name': info.get('course_name'), 'class_name': info.get('class_name'),
                    'lesson_name': info.get('lesson_name'), 'total_videos': info.get('total_videos'),
                    'downloaded': info.get('downloaded'), 'course_row_index': info.get('course_row_index'),
                    'total_courses': info.get('total_courses'), 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except Exception as e:
                log_cb(f'记录 last_success 失败: {e}')

        self._download_done = False

        def target():
            try:
                print('[DEBUG] 下载线程已启动')
                def course_status_cb(course_index, course, status, message):
                    def _apply():
                        self.set_row_status(course_index, status, message)
                        if status == 'success':
                            self.success_count += 1
                        elif status == 'failed':
                            self.fail_count += 1
                        done = self.success_count + self.fail_count
                        self.update_progress(self.total_items, done, self.success_count, self.fail_count)
                    self.root.after(0, _apply)

                print('[DEBUG] 调用 download_videos...')
                videodownloader.download_videos(save_dir, csv_file, cookie,
                                                driver_path=driver_path,
                                                headless=True,
                                                log_callback=log_cb,
                                                pause_event=self.pause_event,
                                                stop_event=self.stop_event,
                                                progress_callback=progress_cb,
                                                lesson_done_callback=lesson_done_cb,
                                                driver_ref=self.driver_ref,
                                                course_status_callback=course_status_cb,
                                                courses_override=courses_override,
                                                max_workers=max_workers,
                                                speed_limit=speed_limit,
                                                skip_existing=skip_existing)
                print('[DEBUG] download_videos 返回')
                log_cb('全部任务完成')
            except Exception as e:
                print(f'[DEBUG] 下载线程异常: {e}')
                log_cb(f'下载任务异常: {e}')
            finally:
                self._download_done = True
                print('[DEBUG] 下载线程结束')

        print('[DEBUG] 启动下载线程...')
        threading.Thread(target=target, daemon=True).start()
        print('[DEBUG] 下载线程已启动，主线程继续')


if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    root.mainloop()
