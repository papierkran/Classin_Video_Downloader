import os
import json
import threading
import tempfile
import subprocess
import sys
import time
import signal

signal.signal(signal.SIGINT, signal.SIG_IGN)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QSplitter,
    QFileDialog, QMessageBox, QFrame, QGridLayout, QAbstractItemView,
    QDialog, QDialogButtonBox, QFormLayout, QStyledItemDelegate, QTabWidget,
    QStyle
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QThread, QRect
from PySide6.QtGui import QFont, QColor, QPalette, QIcon, QAction

import videodownloader
import driver_auto


def _get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(_get_base_dir(), 'config.json')
TASK_STATE_PATH = os.path.join(_get_base_dir(), 'last_task_state.json')

BG_COLOR = '#f5f7fa'
CARD_BG = '#ffffff'
ACCENT = '#3182ce'
ACCENT_HOVER = '#2c5282'
TEXT_COLOR = '#2d3748'
TEXT_MUTED = '#718096'
BORDER_COLOR = '#e2e8f0'
SUCCESS_COLOR = '#38a169'
DANGER_COLOR = '#e53e3e'


class LogSignal(QObject):
    message = Signal(str)


class ProgressSignal(QObject):
    update = Signal(int, int, int, int)


class RowStatusSignal(QObject):
    update = Signal(int, str, str)


class DownloadDoneSignal(QObject):
    done = Signal()


class CheckResultSignal(QObject):
    result = Signal(list)


class CheckLogSignal(QObject):
    message = Signal(str)


class DriverPathSignal(QObject):
    ready = Signal(str)


class CookieValidSignal(QObject):
    result = Signal(bool, str)


class RowProgressSignal(QObject):
    update = Signal(int, int)


class RowMessageSignal(QObject):
    update = Signal(int, int, int, str)


class SaveStateSignal(QObject):
    trigger = Signal()


class ProgressDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._progress = {}
        self._status = {}

    def set_progress(self, row, pct):
        self._progress[row] = max(0, min(100, pct))

    def set_status(self, row, status):
        self._status[row] = status

    def clear_all(self):
        self._progress.clear()
        self._status.clear()

    def paint(self, painter, option, index):
        painter.save()
        row = index.row()
        pct = self._progress.get(row, 0)
        status = self._status.get(row, '')
        rect = option.rect

        if status == 'success':
            painter.fillRect(rect, QColor('#D1E7DD'))
        elif status == 'failed':
            painter.fillRect(rect, QColor('#F8D7DA'))
        elif status == 'downloading':
            if pct > 0:
                fill_rect = QRect(rect)
                fill_rect.setWidth(int(rect.width() * pct / 100))
                painter.fillRect(fill_rect, QColor('#bee3f8'))
            else:
                painter.fillRect(rect, QColor('#f0f7ff'))

        painter.restore()
        super().paint(painter, option, index)


def _make_stylesheet():
    return f"""
    QMainWindow {{
        background-color: {BG_COLOR};
    }}
    QFrame#card {{
        background-color: {CARD_BG};
        border: 1px solid {BORDER_COLOR};
        border-radius: 4px;
    }}
    QLabel {{
        color: {TEXT_COLOR};
        font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
        font-size: 10pt;
    }}
    QLabel#title {{
        color: {ACCENT};
        font-size: 16pt;
        font-weight: bold;
    }}
    QLabel#subtitle {{
        color: {TEXT_MUTED};
        font-size: 9pt;
    }}
    QLabel#muted {{
        color: {TEXT_MUTED};
        font-size: 9pt;
    }}
    QLineEdit {{
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        padding: 4px 8px;
        font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
        font-size: 10pt;
        background-color: #ffffff;
        color: {TEXT_COLOR};
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus {{
        border-color: {ACCENT};
    }}
    QPushButton {{
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        padding: 6px 14px;
        font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
        font-size: 10pt;
        background-color: {CARD_BG};
        color: {TEXT_COLOR};
    }}
    QPushButton:hover {{
        border-color: {ACCENT};
    }}
    QPushButton#primary {{
        background-color: {ACCENT};
        color: #ffffff;
        border: none;
    }}
    QPushButton#primary:hover {{
        background-color: {ACCENT_HOVER};
    }}
    QPushButton#danger {{
        background-color: {DANGER_COLOR};
        color: #ffffff;
        border: none;
    }}
    QPushButton#danger:hover {{
        background-color: #c53030;
    }}
    QPushButton:disabled {{
        background-color: #e2e8f0;
        color: #a0aec0;
    }}
    QTextEdit {{
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        background-color: #fafafa;
        color: {TEXT_COLOR};
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 9pt;
        padding: 8px;
    }}
    QComboBox {{
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        padding: 4px 8px;
        font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
        font-size: 10pt;
        background-color: #ffffff;
        color: {TEXT_COLOR};
        min-width: 80px;
    }}
    QComboBox:focus {{
        border-color: {ACCENT};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 20px;
    }}
    QComboBox QAbstractItemView {{
        border: 1px solid {BORDER_COLOR};
        background-color: #ffffff;
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
    }}
    QCheckBox {{
        color: {TEXT_COLOR};
        font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
        font-size: 10pt;
        spacing: 6px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        background-color: #ffffff;
    }}
    QCheckBox::indicator:checked {{
        background-color: {ACCENT};
        border-color: {ACCENT};
    }}
    QTableWidget {{
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        background-color: {CARD_BG};
        gridline-color: {BORDER_COLOR};
        font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
        font-size: 9pt;
        color: {TEXT_COLOR};
        selection-background-color: #bee3f8;
        selection-color: {TEXT_COLOR};
    }}
    QTableWidget::item {{
        padding: 4px 6px;
    }}
    QTableWidget::item:selected {{
        background-color: #bee3f8;
    }}
    QHeaderView::section {{
        background-color: #edf2f7;
        border: none;
        border-bottom: 1px solid {BORDER_COLOR};
        border-right: 1px solid {BORDER_COLOR};
        padding: 6px 8px;
        font-weight: bold;
        color: {TEXT_COLOR};
    }}
    QProgressBar {{
        border: 1px solid {BORDER_COLOR};
        border-radius: 3px;
        text-align: center;
        background-color: {BORDER_COLOR};
        height: 16px;
    }}
    QProgressBar::chunk {{
        background-color: {ACCENT};
        border-radius: 2px;
    }}
    QScrollBar:vertical {{
        border: none;
        background-color: {BG_COLOR};
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background-color: #cbd5e0;
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background-color: #a0aec0;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        border: none;
        background-color: {BG_COLOR};
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background-color: #cbd5e0;
        border-radius: 5px;
        min-width: 30px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background-color: #a0aec0;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
    QSplitter::handle {{
        background-color: {BORDER_COLOR};
        height: 2px;
    }}
    """


class RowEditDialog(QDialog):
    def __init__(self, parent=None, title='编辑行', init=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.setMinimumHeight(300)
        self.init = init or {}

        layout = QFormLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 14, 18, 14)

        self.fields = [
            ('班级ID (course_id)', 'course_id'),
            ('课堂ID (lesson_id)', 'lesson_id'),
            ('班级名称 (class_name)', 'class_name'),
            ('课堂名称 (lesson_name)', 'lesson_name'),
            ('开课时间 (start_time)', 'start_time'),
        ]
        self.entries = {}
        for label, key in self.fields:
            le = QLineEdit()
            le.setText(self.init.get(key, ''))
            layout.addRow(label, le)
            self.entries[key] = le

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self._on_ok)
        btn_box.rejected.connect(self.reject)
        layout.addRow(btn_box)

    def _on_ok(self):
        cid = self.entries['course_id'].text().strip()
        lid = self.entries['lesson_id'].text().strip()
        if not cid or not lid:
            QMessageBox.warning(self, '提示', '班级ID 和 课堂ID 为必填')
            return
        self.accept()

    def get_data(self):
        return {k: self.entries[k].text().strip() for _, k in self.fields}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('ClassIn 视频下载器')
        self.setMinimumSize(960, 600)
        self.resize(1100, 780)

        self._is_downloading = False
        self._download_done = False
        self.total_items = 0
        self.success_count = 0
        self.fail_count = 0
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.driver_ref = {}
        self._download_session = 0

        self.log_signal = LogSignal()
        self.progress_signal = ProgressSignal()
        self.row_status_signal = RowStatusSignal()
        self.row_progress_signal = RowProgressSignal()
        self.row_message_signal = RowMessageSignal()
        self.download_done_signal = DownloadDoneSignal()
        self.driver_path_signal = DriverPathSignal()
        self.cookie_valid_signal = CookieValidSignal()
        self.check_result_signal = CheckResultSignal()
        self.check_log_signal = CheckLogSignal()
        self.save_state_signal = SaveStateSignal()
        self._row_progress = {}

        self.log_signal.message.connect(self._append_log_safe)
        self.progress_signal.update.connect(self._update_progress_safe)
        self.row_status_signal.update.connect(self._set_row_status_safe)
        self.row_progress_signal.update.connect(self._update_row_progress_safe)
        self.row_message_signal.update.connect(self._update_row_message_safe)
        self.download_done_signal.done.connect(self._on_download_done)
        self.driver_path_signal.ready.connect(self._on_driver_path_ready)
        self.cookie_valid_signal.result.connect(self._on_cookie_valid_result)
        self.check_result_signal.result.connect(self._on_check_result)
        self.save_state_signal.trigger.connect(self.save_task_state)
        self.check_log_signal.message.connect(self._append_check_log_safe)
        self._row_vid_msg = {}

        self.setStyleSheet(_make_stylesheet())
        self._build_ui()
        self.load_config()
        if not self.load_task_state():
            self.auto_load_videodata_folder()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(8)

        header_layout = QHBoxLayout()
        title_label = QLabel('ClassIn 视频下载器')
        title_label.setObjectName('title')
        header_layout.addWidget(title_label)
        subtitle_label = QLabel('批量下载课堂录播视频')
        subtitle_label.setObjectName('subtitle')
        header_layout.addWidget(subtitle_label)
        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #e2e8f0;
                border-radius: 4px;
                background-color: #ffffff;
            }
            QTabBar::tab {
                padding: 8px 20px;
                margin-right: 2px;
                border: 1px solid #e2e8f0;
                border-bottom: none;
                border-radius: 4px 4px 0 0;
                background-color: #edf2f7;
                color: #2d3748;
                font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif;
                font-size: 10pt;
            }
            QTabBar::tab:selected {
                background-color: #ffffff;
                border-bottom: 1px solid #ffffff;
                font-weight: bold;
            }
            QTabBar::tab:hover {
                background-color: #e2e8f0;
            }
        """)

        self._build_download_tab()
        self._build_check_tab()

        main_layout.addWidget(self.tab_widget, 1)

    def _build_download_tab(self):
        download_tab = QWidget()
        tab_layout = QVBoxLayout(download_tab)
        tab_layout.setContentsMargins(8, 6, 8, 6)
        tab_layout.setSpacing(4)

        config_card = QFrame()
        config_card.setObjectName('card')
        config_layout = QGridLayout(config_card)
        config_layout.setSpacing(4)
        config_layout.setContentsMargins(10, 6, 10, 6)

        row = 0
        config_layout.addWidget(QLabel('浏览器驱动'), row, 0)
        self.driver_entry = QLineEdit()
        config_layout.addWidget(self.driver_entry, row, 1)
        driver_btn_layout = QHBoxLayout()
        driver_btn_layout.setSpacing(4)
        self.btn_browse_driver = QPushButton('浏览')
        self.btn_browse_driver.clicked.connect(self.browse_driver)
        driver_btn_layout.addWidget(self.btn_browse_driver)
        self.btn_auto_detect = QPushButton('自动检测')
        self.btn_auto_detect.clicked.connect(self.auto_detect_driver)
        driver_btn_layout.addWidget(self.btn_auto_detect)
        self.btn_update_driver = QPushButton('更新')
        self.btn_update_driver.clicked.connect(self.update_driver)
        driver_btn_layout.addWidget(self.btn_update_driver)
        config_layout.addLayout(driver_btn_layout, row, 2)

        row = 1
        config_layout.addWidget(QLabel('保存目录'), row, 0)
        self.save_entry = QLineEdit()
        config_layout.addWidget(self.save_entry, row, 1)
        btn_save_dir = QPushButton('浏览')
        btn_save_dir.clicked.connect(self.browse_save)
        config_layout.addWidget(btn_save_dir, row, 2)

        row = 2
        config_layout.addWidget(QLabel('数据文件'), row, 0)
        self.csv_entry = QLineEdit()
        config_layout.addWidget(self.csv_entry, row, 1)
        btn_csv = QPushButton('浏览')
        btn_csv.clicked.connect(self.browse_csv)
        config_layout.addWidget(btn_csv, row, 2)

        row = 3
        config_layout.addWidget(QLabel('Cookie'), row, 0, Qt.AlignTop)
        self.cookie_text = QTextEdit()
        self.cookie_text.setMinimumHeight(40)
        self.cookie_text.setMaximumHeight(60)
        config_layout.addWidget(self.cookie_text, row, 1)
        cookie_btn_layout = QVBoxLayout()
        cookie_btn_layout.setSpacing(2)
        btn_get_cookie = QPushButton('获取')
        btn_get_cookie.clicked.connect(self.get_cookie)
        cookie_btn_layout.addWidget(btn_get_cookie)
        btn_verify_cookie = QPushButton('验证')
        btn_verify_cookie.clicked.connect(self.verify_cookie)
        cookie_btn_layout.addWidget(btn_verify_cookie)
        config_layout.addLayout(cookie_btn_layout, row, 2)

        row = 4
        config_layout.addWidget(QLabel('线程数'), row, 0)
        self.threads_entry = QLineEdit('4')
        self.threads_entry.setMaximumWidth(60)
        config_layout.addWidget(self.threads_entry, row, 1)
        lbl_threads_hint = QLabel('  速度限制')
        lbl_threads_hint.setObjectName('muted')
        config_layout.addWidget(lbl_threads_hint, row, 2)

        config_layout.setColumnStretch(1, 1)
        tab_layout.addWidget(config_card)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(6)
        self.btn_save = QPushButton('保存配置')
        self.btn_save.clicked.connect(self.save_config)
        action_layout.addWidget(self.btn_save)
        self.btn_start = QPushButton('开始下载')
        self.btn_start.setObjectName('primary')
        self.btn_start.clicked.connect(self.start_download)
        action_layout.addWidget(self.btn_start)
        self.btn_pause = QPushButton('暂停')
        self.btn_pause.clicked.connect(self.toggle_pause)
        action_layout.addWidget(self.btn_pause)
        self.btn_stop = QPushButton('停止')
        self.btn_stop.setObjectName('danger')
        self.btn_stop.clicked.connect(self.stop_download)
        action_layout.addWidget(self.btn_stop)
        action_layout.addStretch()

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(['不限制', '1 MB/s', '2 MB/s', '5 MB/s', '10 MB/s'])
        action_layout.addWidget(QLabel('速度:'))
        action_layout.addWidget(self.speed_combo)

        self.skip_existing_cb = QCheckBox('跳过已下载')
        self.skip_existing_cb.setChecked(True)
        action_layout.addWidget(self.skip_existing_cb)

        self.finish_combo = QComboBox()
        self.finish_combo.addItems(['无操作', '关闭程序', '休眠电脑', '关机'])
        action_layout.addWidget(QLabel('完成后:'))
        action_layout.addWidget(self.finish_combo)
        tab_layout.addLayout(action_layout)

        self._progress_row2_widget = QWidget()
        progress_row2 = QHBoxLayout(self._progress_row2_widget)
        progress_row2.setContentsMargins(0, 0, 0, 0)
        self.total_progress_label = QLabel('总进度: 0/0 | 成功 0 失败 0')
        progress_row2.addWidget(self.total_progress_label)
        self.total_progress_bar = QProgressBar()
        self.total_progress_bar.setRange(0, 100)
        self.total_progress_bar.setValue(0)
        self.total_progress_bar.setTextVisible(False)
        self.total_progress_bar.setMaximumHeight(18)
        progress_row2.addWidget(self.total_progress_bar, 1)
        tab_layout.addWidget(self._progress_row2_widget)

        self._progress_row2_widget.setVisible(False)

        list_header = QHBoxLayout()
        list_header.setSpacing(6)
        lbl_tasks = QLabel('任务列表')
        lbl_tasks.setStyleSheet('font-weight: bold;')
        list_header.addWidget(lbl_tasks)
        list_header.addStretch()

        list_header.addWidget(QLabel('排序:'))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(['默认', '班级ID', '课堂ID', '开课时间', '状态'])
        self.sort_combo.currentTextChanged.connect(self.on_sort_change)
        list_header.addWidget(self.sort_combo)

        list_header.addWidget(QLabel('筛选:'))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(['全部', '待下载', '下载中', '已完成', '失败'])
        self.filter_combo.currentTextChanged.connect(self.on_filter_change)
        list_header.addWidget(self.filter_combo)

        tab_layout.addLayout(list_header)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            '班级ID', '课堂ID', '班级名称', '课堂名称', '开课时间', '状态', '消息'
        ])
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._progress_delegate = ProgressDelegate(self.table)
        self.table.setItemDelegate(self._progress_delegate)
        tab_layout.addWidget(self.table, 3)

        table_btn_layout = QHBoxLayout()
        table_btn_layout.setSpacing(4)
        btn_import_table = QPushButton('导入CSV/XLSX')
        btn_import_table.clicked.connect(self.import_table_file)
        table_btn_layout.addWidget(btn_import_table)
        btn_add_row = QPushButton('新增行')
        btn_add_row.clicked.connect(self.add_row_dialog)
        table_btn_layout.addWidget(btn_add_row)
        btn_edit_row = QPushButton('编辑行')
        btn_edit_row.clicked.connect(self.edit_selected_row_dialog)
        table_btn_layout.addWidget(btn_edit_row)
        btn_delete_row = QPushButton('删除行')
        btn_delete_row.setObjectName('danger')
        btn_delete_row.clicked.connect(self.delete_selected_rows)
        table_btn_layout.addWidget(btn_delete_row)
        table_btn_layout.addStretch()
        btn_export_log = QPushButton('导出日志')
        btn_export_log.clicked.connect(self.export_log)
        table_btn_layout.addWidget(btn_export_log)
        tab_layout.addLayout(table_btn_layout)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setMaximumHeight(120)
        tab_layout.addWidget(self.log_area, 1)

        self.tab_widget.addTab(download_tab, '视频下载')

    def _build_check_tab(self):
        check_tab = QWidget()
        tab_layout = QVBoxLayout(check_tab)
        tab_layout.setContentsMargins(8, 6, 8, 6)
        tab_layout.setSpacing(4)

        config_card = QFrame()
        config_card.setObjectName('card')
        config_layout = QGridLayout(config_card)
        config_layout.setSpacing(4)
        config_layout.setContentsMargins(10, 6, 10, 6)

        row = 0
        config_layout.addWidget(QLabel('视频目录'), row, 0)
        self.check_dir_entry = QLineEdit()
        config_layout.addWidget(self.check_dir_entry, row, 1)
        btn_browse_check_dir = QPushButton('浏览')
        btn_browse_check_dir.clicked.connect(self.browse_check_dir)
        config_layout.addWidget(btn_browse_check_dir, row, 2)

        row = 1
        config_layout.addWidget(QLabel('文档文件'), row, 0)
        self.check_csv_entry = QLineEdit()
        config_layout.addWidget(self.check_csv_entry, row, 1)
        btn_browse_check_csv = QPushButton('浏览')
        btn_browse_check_csv.clicked.connect(self.browse_check_csv)
        config_layout.addWidget(btn_browse_check_csv, row, 2)

        config_layout.setColumnStretch(1, 1)
        tab_layout.addWidget(config_card)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(6)
        self.btn_check_scan = QPushButton('开始检查')
        self.btn_check_scan.setObjectName('primary')
        self.btn_check_scan.clicked.connect(self.start_check)
        action_layout.addWidget(self.btn_check_scan)
        self.btn_check_export = QPushButton('导出结果')
        self.btn_check_export.clicked.connect(self.export_check_result)
        action_layout.addWidget(self.btn_check_export)
        action_layout.addStretch()

        self.check_stats_label = QLabel('统计: 等待检查...')
        self.check_stats_label.setStyleSheet('font-weight: bold;')
        action_layout.addWidget(self.check_stats_label)

        tab_layout.addLayout(action_layout)

        self.check_table = QTableWidget()
        self.check_table.setColumnCount(6)
        self.check_table.setHorizontalHeaderLabels([
            '班级名称', '课堂名称', '开课时间', '状态', '文件夹', '详情'
        ])
        check_header = self.check_table.horizontalHeader()
        check_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        check_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        check_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        check_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        check_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        check_header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.check_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.check_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.check_table.verticalHeader().setVisible(False)
        self.check_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tab_layout.addWidget(self.check_table, 3)

        filter_layout = QVBoxLayout()
        filter_layout.setSpacing(2)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel('筛选:'))
        self.check_filter_combo = QComboBox()
        self.check_filter_combo.addItems(['全部', '未下载', '已下载', '部分下载'])
        self.check_filter_combo.currentTextChanged.connect(self.on_check_filter_change)
        filter_row.addWidget(self.check_filter_combo)
        filter_row.addStretch()
        filter_layout.addLayout(filter_row)

        self.check_log_area = QTextEdit()
        self.check_log_area.setReadOnly(True)
        self.check_log_area.setMaximumHeight(100)
        filter_layout.addWidget(self.check_log_area, 1)

        tab_layout.addLayout(filter_layout, 1)

        self.tab_widget.addTab(check_tab, '下载检查')

    def _append_log_safe(self, msg):
        try:
            self.log_area.append(str(msg))
            sb = self.log_area.verticalScrollBar()
            sb.setValue(sb.maximum())
        except KeyboardInterrupt:
            pass

    def _update_progress_safe(self, total_items, done_count, success_count, fail_count):
        try:
            self.total_items = max(self.total_items, int(total_items or 0))
            self.success_count = int(success_count or 0)
            self.fail_count = int(fail_count or 0)
            done = int(done_count or 0)
            self._progress_row2_widget.setVisible(True)
            self.total_progress_label.setText(
                f'总进度: {done}/{self.total_items} | 成功 {self.success_count} 失败 {self.fail_count}'
            )
            if self.total_items > 0:
                pct = int(done * 100 / self.total_items)
                self.total_progress_bar.setValue(min(pct, 100))
        except KeyboardInterrupt:
            pass

    def _update_row_progress_safe(self, row, pct):
        try:
            if row < 0 or row >= self.table.rowCount():
                return
            pct = max(0, min(100, pct))
            self._row_progress[row] = pct
            self._progress_delegate.set_progress(row, pct)
            self.table.viewport().update()
        except KeyboardInterrupt:
            pass

    def _update_row_message_safe(self, row, vid_idx, total_vids, msg):
        try:
            if row < 0 or row >= self.table.rowCount():
                return
            if row not in self._row_vid_msg:
                self._row_vid_msg[row] = {}
            self._row_vid_msg[row][vid_idx] = msg
            status_item = self.table.item(row, 5)
            if status_item and status_item.text() == 'downloading':
                parts = []
                for i in range(1, total_vids + 1):
                    if i in self._row_vid_msg[row]:
                        parts.append(self._row_vid_msg[row][i])
                combined = ' | '.join(parts)
                msg_item = self.table.item(row, 6)
                if msg_item:
                    msg_item.setText(combined)
        except KeyboardInterrupt:
            pass

    def _set_row_status_safe(self, row_index, status, message=''):
        try:
            if row_index <= 0 or row_index > self.table.rowCount():
                return
            row = row_index - 1
            item5 = self.table.item(row, 5)
            item6 = self.table.item(row, 6)
            if item5:
                item5.setText(status)
            if item6:
                item6.setText(message)
            self._progress_delegate.set_status(row, status)
            if status in ('success', 'failed'):
                self._row_progress[row] = 100 if status == 'success' else 0
                self._progress_delegate.set_progress(row, 100 if status == 'success' else 0)
            self.table.viewport().update()
        except KeyboardInterrupt:
            pass

    def _on_download_done(self):
        self.btn_start.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.sort_combo.setEnabled(True)
        self.filter_combo.setEnabled(True)
        if self.driver_ref:
            try:
                driver = self.driver_ref.get('driver')
                if driver is not None:
                    driver.quit()
                    self.driver_ref['driver'] = None
                    self.append_log('已关闭浏览器')
            except Exception as e:
                self.append_log(f'关闭浏览器时出错: {e}')
            self.driver_ref = {}
        self._is_downloading = False
        self.save_task_state()

    def _on_driver_path_ready(self, path):
        self.driver_entry.setText(path)
        self.save_config(silent=True)

    def _on_cookie_valid_result(self, is_valid, message):
        if is_valid:
            QMessageBox.information(self, '验证结果', f'Cookie有效！\n{message}')
            self.append_log('Cookie验证通过')
        else:
            QMessageBox.warning(self, '验证结果', f'Cookie无效！\n{message}')
            self.append_log(f'Cookie验证失败: {message}')

    def browse_driver(self):
        f, _ = QFileDialog.getOpenFileName(self, '选择驱动', '', 'Driver (*.exe);;All Files (*)')
        if f:
            self.driver_entry.setText(f)

    def auto_detect_driver(self):
        base_dir = _get_base_dir()
        self.append_log('正在检测浏览器...')
        browser, bin_path = driver_auto.detect_browser_preference()
        if not browser:
            QMessageBox.critical(self, '错误', '未检测到 Microsoft Edge 或 Chrome 浏览器，请手动指定驱动路径。')
            return
        self.append_log(f'检测到浏览器: {browser} ({bin_path})')

        default_name = 'msedgedriver.exe' if browser == 'edge' else 'chromedriver.exe'
        existing = os.path.join(base_dir, default_name)
        if os.path.exists(existing):
            self.driver_entry.setText(existing)
            self.save_config(silent=True)
            self.append_log(f'已存在驱动: {existing}')
            QMessageBox.information(self, '成功', f'已检测到驱动文件:\n{existing}')
            return

        def do_download():
            try:
                self.append_log(f'正在下载 {browser} 驱动，请稍候...')
                driver_path = driver_auto.ensure_driver(browser, base_dir=base_dir)
                if driver_path and os.path.exists(driver_path):
                    self.driver_path_signal.ready.emit(driver_path)
                    self.append_log(f'驱动已就绪: {driver_path}')
                    QTimer.singleShot(0, lambda: QMessageBox.information(
                        self, '成功', f'驱动已下载到:\n{driver_path}'))
                else:
                    self.append_log('驱动下载失败')
                    QTimer.singleShot(0, lambda: QMessageBox.critical(
                        self, '错误', '驱动下载失败，请手动指定驱动路径。'))
            except Exception as e:
                self.append_log(f'驱动下载异常: {e}')
                QTimer.singleShot(0, lambda: QMessageBox.critical(
                    self, '错误', f'驱动下载失败: {e}\n请手动指定驱动路径。'))

        threading.Thread(target=do_download, daemon=True).start()

    def update_driver(self):
        base_dir = _get_base_dir()
        self.append_log('正在检测浏览器...')
        browser, bin_path = driver_auto.detect_browser_preference()
        if not browser:
            QMessageBox.critical(self, '错误', '未检测到 Microsoft Edge 或 Chrome 浏览器。')
            return
        self.append_log(f'检测到浏览器: {browser} ({bin_path})')

        def do_update():
            try:
                self.append_log(f'正在下载最新 {browser} 驱动，请稍候...')
                driver_path = driver_auto.ensure_driver(browser, base_dir=base_dir)
                if driver_path and os.path.exists(driver_path):
                    self.driver_path_signal.ready.emit(driver_path)
                    self.append_log(f'驱动已更新: {driver_path}')
                    QTimer.singleShot(0, lambda: QMessageBox.information(
                        self, '成功', f'驱动已更新到最新版本:\n{driver_path}'))
                else:
                    self.append_log('驱动下载失败')
                    QTimer.singleShot(0, lambda: QMessageBox.critical(
                        self, '错误', '驱动下载失败，请手动指定驱动路径。'))
            except Exception as e:
                self.append_log(f'驱动更新异常: {e}')
                QTimer.singleShot(0, lambda: QMessageBox.critical(
                    self, '错误', f'驱动更新失败: {e}\n请手动指定驱动路径。'))

        threading.Thread(target=do_update, daemon=True).start()

    def browse_save(self):
        d = QFileDialog.getExistingDirectory(self, '选择保存目录')
        if d:
            self.save_entry.setText(d)

    def browse_csv(self):
        f, _ = QFileDialog.getOpenFileName(
            self, '选择数据文件', '',
            'CSV/XLSX Files (*.csv *.xlsx *.xls);;All Files (*)')
        if f:
            self.csv_entry.setText(f)
            self.clear_task_state()
            self.load_table_from_file(f)

    def save_config(self, silent=False):
        cfg = {
            'driver_path': self.driver_entry.text().strip(),
            'save_dir': self.save_entry.text().strip(),
            'csv_file': self.csv_entry.text().strip(),
            'cookie': self.cookie_text.toPlainText().strip(),
            'max_workers': self.threads_entry.text().strip() or '4',
            'speed_limit': self.speed_combo.currentText(),
            'finish_action': self.finish_combo.currentText(),
            'skip_existing': self.skip_existing_cb.isChecked()
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
                QMessageBox.information(self, '保存', '配置已保存到 config.json')
        except Exception as e:
            if not silent:
                QMessageBox.critical(self, '错误', f'保存配置失败: {e}')

    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
                self.driver_entry.setText(cfg.get('driver_path', ''))
                self.save_entry.setText(cfg.get('save_dir', ''))
                self.csv_entry.setText(cfg.get('csv_file', ''))
                self.cookie_text.setPlainText(cfg.get('cookie', ''))
                self.threads_entry.setText(cfg.get('max_workers', '4'))
                idx = self.speed_combo.findText(cfg.get('speed_limit', '不限制'))
                if idx >= 0:
                    self.speed_combo.setCurrentIndex(idx)
                idx = self.finish_combo.findText(cfg.get('finish_action', '无操作'))
                if idx >= 0:
                    self.finish_combo.setCurrentIndex(idx)
                self.skip_existing_cb.setChecked(cfg.get('skip_existing', True))
            except Exception:
                pass

    def save_task_state(self):
        try:
            courses = []
            for row in range(self.table.rowCount()):
                courses.append({
                    'course_id': self.table.item(row, 0).text() if self.table.item(row, 0) else '',
                    'lesson_id': self.table.item(row, 1).text() if self.table.item(row, 1) else '',
                    'class_name': self.table.item(row, 2).text() if self.table.item(row, 2) else '',
                    'lesson_name': self.table.item(row, 3).text() if self.table.item(row, 3) else '',
                    'start_time': self.table.item(row, 4).text() if self.table.item(row, 4) else '',
                    'status': self.table.item(row, 5).text() if self.table.item(row, 5) else 'pending',
                    'message': self.table.item(row, 6).text() if self.table.item(row, 6) else '',
                })
            state = {
                'courses': courses,
                'csv_file': self.csv_entry.text().strip(),
                'save_dir': self.save_entry.text().strip(),
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            }
            with open(TASK_STATE_PATH, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_task_state(self):
        if not os.path.exists(TASK_STATE_PATH):
            return False
        try:
            with open(TASK_STATE_PATH, 'r', encoding='utf-8') as f:
                state = json.load(f)
            courses = state.get('courses', [])
            if not courses:
                return False
            csv_file = state.get('csv_file', '')
            save_dir = state.get('save_dir', '')
            if csv_file:
                self.csv_entry.setText(csv_file)
            if save_dir:
                self.save_entry.setText(save_dir)
            self._clear_table()
            for c in courses:
                status = c.get('status', 'pending')
                if status == 'downloading':
                    status = 'pending'
                self._insert_course_row(c, status=status, message=c.get('message', ''))
            ts = state.get('timestamp', '')
            self.append_log(f'已恢复上次任务 ({ts})，共 {len(courses)} 条')
            return True
        except Exception:
            return False

    def clear_task_state(self):
        try:
            if os.path.exists(TASK_STATE_PATH):
                os.remove(TASK_STATE_PATH)
        except Exception:
            pass

    def export_config(self):
        try:
            cfg = {
                'driver_path': self.driver_entry.text().strip(),
                'save_dir': self.save_entry.text().strip(),
                'csv_file': self.csv_entry.text().strip(),
                'cookie': self.cookie_text.toPlainText().strip(),
                'max_workers': self.threads_entry.text().strip() or '4'
            }
            default_filename = f'配置备份_{time.strftime("%Y%m%d_%H%M%S")}.json'
            f, _ = QFileDialog.getSaveFileName(
                self, '导出配置', default_filename,
                'JSON文件 (*.json);;All Files (*)')
            if f:
                with open(f, 'w', encoding='utf-8') as file:
                    json.dump(cfg, file, ensure_ascii=False, indent=2)
                QMessageBox.information(self, '成功', f'配置已导出到:\n{f}')
                self.append_log(f'配置已导出: {f}')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'导出配置失败: {e}')
            self.append_log(f'导出配置失败: {e}')

    def import_config(self):
        try:
            f, _ = QFileDialog.getOpenFileName(
                self, '导入配置', '',
                'JSON文件 (*.json);;All Files (*)')
            if f:
                with open(f, 'r', encoding='utf-8') as file:
                    cfg = json.load(file)
                self.driver_entry.setText(cfg.get('driver_path', ''))
                self.save_entry.setText(cfg.get('save_dir', ''))
                self.csv_entry.setText(cfg.get('csv_file', ''))
                self.cookie_text.setPlainText(cfg.get('cookie', ''))
                self.threads_entry.setText(cfg.get('max_workers', '4'))
                QMessageBox.information(self, '成功', f'配置已从文件导入:\n{f}')
                self.append_log(f'配置已导入: {f}')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'导入配置失败: {e}')
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
        self.csv_entry.setText(target)
        self.load_table_from_file(target)
        self.append_log(f'已默认加载：{target}')

    def import_table_file(self):
        f, _ = QFileDialog.getOpenFileName(
            self, '选择数据文件', '',
            'CSV/XLSX Files (*.csv *.xlsx *.xls);;All Files (*)')
        if f:
            self.csv_entry.setText(f)
            self.clear_task_state()
            self.load_table_from_file(f)

    def load_table_from_file(self, path):
        try:
            courses = self._load_courses_any(path)
        except Exception as e:
            QMessageBox.critical(self, '错误', f'读取失败: {e}')
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

    def _clear_table(self):
        self.table.setRowCount(0)

    def _insert_course_row(self, course, status='pending', message=''):
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            course.get('course_id', ''),
            course.get('lesson_id', ''),
            course.get('class_name', ''),
            course.get('lesson_name', ''),
            course.get('start_time', ''),
            status,
            message
        ]
        for col, val in enumerate(values):
            item = QTableWidgetItem(str(val))
            self.table.setItem(row, col, item)
        self._progress_delegate.set_status(row, status)

    def add_row_dialog(self):
        self._row_dialog('新增行')

    def edit_selected_row_dialog(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, '提示', '请先选中一行')
            return
        row = selected[0].row()
        init = {}
        for col, key in enumerate(['course_id', 'lesson_id', 'class_name', 'lesson_name', 'start_time']):
            item = self.table.item(row, col)
            init[key] = item.text() if item else ''
        self._row_dialog('编辑行', edit_row=row, init=init)

    def _row_dialog(self, title, edit_row=None, init=None):
        dlg = RowEditDialog(self, title=title, init=init)
        if dlg.exec() == QDialog.Accepted:
            data = dlg.get_data()
            if edit_row is not None:
                for col, key in enumerate(['course_id', 'lesson_id', 'class_name', 'lesson_name', 'start_time']):
                    item = self.table.item(edit_row, col)
                    if item:
                        item.setText(data[key])
                item5 = self.table.item(edit_row, 5)
                item6 = self.table.item(edit_row, 6)
                if item5:
                    item5.setText('pending')
                if item6:
                    item6.setText('')
                self._progress_delegate.set_status(edit_row, 'pending')
                self._progress_delegate.set_progress(edit_row, 0)
                self._row_progress[edit_row] = 0
                if edit_row in self._row_vid_msg:
                    del self._row_vid_msg[edit_row]
                self.table.viewport().update()
            else:
                self._insert_course_row(data)

    def delete_selected_rows(self):
        selected = self.table.selectedItems()
        if not selected:
            QMessageBox.warning(self, '提示', '请先选中要删除的行')
            return
        del_rows = sorted(set(item.row() for item in selected), reverse=True)
        reply = QMessageBox.question(
            self, '确认', f'确认删除选中的 {len(del_rows)} 行吗？',
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            del_set = set(del_rows)
            old_count = self.table.rowCount()
            old2new = {}
            offset = 0
            for r in range(old_count):
                if r in del_set:
                    offset += 1
                else:
                    old2new[r] = r - offset
            for row in del_rows:
                self.table.removeRow(row)
            new_rp = {}
            new_dp = {}
            new_ds = {}
            new_rm = {}
            for old_r, new_r in old2new.items():
                if old_r in self._row_progress:
                    new_rp[new_r] = self._row_progress[old_r]
                if old_r in self._row_vid_msg:
                    new_rm[new_r] = self._row_vid_msg[old_r]
                if old_r in self._progress_delegate._progress:
                    new_dp[new_r] = self._progress_delegate._progress[old_r]
                if old_r in self._progress_delegate._status:
                    new_ds[new_r] = self._progress_delegate._status[old_r]
            self._row_progress = new_rp
            self._row_vid_msg = new_rm
            self._progress_delegate._progress = new_dp
            self._progress_delegate._status = new_ds

    def on_sort_change(self, sort_key):
        if sort_key == '默认':
            return
        items = []
        for row in range(self.table.rowCount()):
            values = []
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                values.append(item.text() if item else '')
            items.append((row, values))

        key_map = {'班级ID': 0, '课堂ID': 1, '开课时间': 4}
        status_order = {'pending': 0, 'downloading': 1, 'success': 2, 'failed': 3}

        if sort_key in key_map:
            items.sort(key=lambda x: x[1][key_map[sort_key]] if x[1][key_map[sort_key]] else '')
        elif sort_key == '状态':
            items.sort(key=lambda x: status_order.get(x[1][5], 4))

        old_progress = dict(self._row_progress)
        old_vid_msg = dict(self._row_vid_msg)
        old_delegate_progress = dict(self._progress_delegate._progress)
        old_delegate_status = dict(self._progress_delegate._status)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for orig_row, values in items:
            new_row = self.table.rowCount()
            self.table.insertRow(new_row)
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                self.table.setItem(new_row, col, item)
            self._progress_delegate.set_status(new_row, values[5])
            if orig_row in old_progress:
                self._row_progress[new_row] = old_progress[orig_row]
                self._progress_delegate.set_progress(new_row, old_delegate_progress.get(orig_row, 0))
            elif new_row in self._row_progress:
                del self._row_progress[new_row]
            if orig_row in old_vid_msg:
                self._row_vid_msg[new_row] = old_vid_msg[orig_row]
        self.append_log(f'任务列表已按 {sort_key} 排序')

    def on_filter_change(self, filter_key):
        if filter_key == '全部':
            for row in range(self.table.rowCount()):
                self.table.setRowHidden(row, False)
            return

        filter_map = {
            '待下载': 'pending',
            '下载中': 'downloading',
            '已完成': 'success',
            '失败': 'failed'
        }
        target_status = filter_map.get(filter_key)
        if target_status:
            for row in range(self.table.rowCount()):
                status_item = self.table.item(row, 5)
                status = status_item.text() if status_item else ''
                self.table.setRowHidden(row, status != target_status)
        self.append_log(f'任务列表已筛选: {filter_key}')

    def collect_courses_from_table(self):
        courses = []
        for row in range(self.table.rowCount()):
            courses.append({
                'course_id': self.table.item(row, 0).text() if self.table.item(row, 0) else '',
                'lesson_id': self.table.item(row, 1).text() if self.table.item(row, 1) else '',
                'class_name': self.table.item(row, 2).text() if self.table.item(row, 2) else '',
                'lesson_name': self.table.item(row, 3).text() if self.table.item(row, 3) else '',
                'start_time': self.table.item(row, 4).text() if self.table.item(row, 4) else '',
            })
        return courses

    def append_log(self, msg):
        self.log_signal.message.emit(str(msg))

    def export_log(self):
        try:
            log_content = self.log_area.toPlainText().strip()
            if not log_content:
                QMessageBox.warning(self, '提示', '日志内容为空')
                return
            default_filename = f'下载日志_{time.strftime("%Y%m%d_%H%M%S")}.txt'
            f, _ = QFileDialog.getSaveFileName(
                self, '导出日志', default_filename,
                '文本文件 (*.txt);;All Files (*)')
            if f:
                with open(f, 'w', encoding='utf-8') as file:
                    file.write(log_content)
                QMessageBox.information(self, '成功', f'日志已导出到:\n{f}')
                self.append_log(f'日志已导出: {f}')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'导出日志失败: {e}')
            self.append_log(f'导出日志失败: {e}')

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pause.setText('暂停')
            self.append_log('已继续下载')
        else:
            self.pause_event.set()
            self.btn_pause.setText('继续')
            self.append_log('已暂停下载')

    def stop_download(self):
        if not self._is_downloading:
            return
        self.stop_event.set()
        self.pause_event.clear()
        self.btn_pause.setText('暂停')
        self._is_downloading = False
        self.total_items = 0
        self.success_count = 0
        self.fail_count = 0
        self._row_progress.clear()
        self._row_vid_msg.clear()
        self._progress_delegate.clear_all()
        self.table.viewport().update()
        self.sort_combo.setEnabled(True)
        self.filter_combo.setEnabled(True)

        def close_driver_later():
            if self.driver_ref:
                try:
                    driver = self.driver_ref.get('driver')
                    if driver is not None:
                        driver.quit()
                        self.driver_ref['driver'] = None
                        self.append_log('已关闭浏览器')
                except Exception as e:
                    self.append_log(f'关闭浏览器时出错: {e}')
                self.driver_ref = {}
            self.save_task_state()
            self._download_done = True
            self.download_done_signal.done.emit()

        QTimer.singleShot(500, close_driver_later)

    def parse_speed_limit(self, speed_str):
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
        errors = []
        driver_path = self.driver_entry.text().strip()
        if driver_path and not os.path.exists(driver_path):
            errors.append('指定的驱动文件不存在')
        save_dir = self.save_entry.text().strip()
        if save_dir and len(save_dir) > 260:
            errors.append('保存目录路径过长')
        csv_file = self.csv_entry.text().strip()
        if csv_file and not os.path.exists(csv_file):
            errors.append('指定的数据文件不存在')
        cookie = self.cookie_text.toPlainText().strip()
        if not cookie:
            errors.append('Cookie不能为空')
        max_workers_str = self.threads_entry.text().strip()
        if max_workers_str:
            try:
                max_workers = int(max_workers_str)
                if max_workers < 1 or max_workers > 8:
                    errors.append('下载线程数必须在1-8之间')
            except ValueError:
                errors.append('下载线程数必须是数字')
        return errors

    def start_download(self):
        if self._is_downloading:
            QMessageBox.warning(self, '提示', '下载正在进行中，请勿重复点击')
            return

        errors = self.validate_inputs()
        if errors:
            QMessageBox.critical(self, '输入错误', '请修正以下问题:\n' + '\n'.join(errors))
            return

        driver_path = self.driver_entry.text().strip() or None
        save_dir = self.save_entry.text().strip() or None
        csv_file = self.csv_entry.text().strip() or None
        cookie = self.cookie_text.toPlainText().strip() or ''
        max_workers_str = self.threads_entry.text().strip() or '4'
        speed_limit = self.parse_speed_limit(self.speed_combo.currentText())
        skip_existing = self.skip_existing_cb.isChecked()
        finish_action = self.finish_combo.currentText()

        try:
            max_workers = max(1, min(8, int(max_workers_str)))
        except ValueError:
            max_workers = 4
            self.threads_entry.setText('4')

        if not cookie:
            QMessageBox.warning(self, '提示', 'Cookie不能为空，请先获取或输入Cookie')
            return

        courses_override = self.collect_courses_from_table()
        if not courses_override:
            QMessageBox.warning(self, '提示', '任务列表为空，请先导入或新增任务行')
            return

        self.save_config(silent=True)
        self.btn_start.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.sort_combo.setEnabled(False)
        self.filter_combo.setEnabled(False)
        self._is_downloading = True
        self.total_items = len(courses_override)
        self.success_count = 0
        self.fail_count = 0
        self.progress_signal.update.emit(self.total_items, 0, 0, 0)

        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.driver_ref = {}
        self._download_session += 1

        def log_cb(msg):
            self.log_signal.message.emit(msg)

        def progress_cb(total_courses, current_course_index, course, video_index):
            try:
                if isinstance(video_index, dict):
                    row = current_course_index - 1
                    vid_idx = video_index.get('video_index', 1)
                    total_vids = video_index.get('total_videos', 1) or 1
                    vid_pct = video_index.get('percent', 0)
                    row_pct = int(((vid_idx - 1) * 100 + vid_pct) / total_vids)
                    self.row_progress_signal.update.emit(row, row_pct)
                    speed = video_index.get('speed_mbps', 0.0)
                    info = video_index.get('info', '')
                    if speed > 0:
                        msg = f'{vid_idx}/{total_vids} | {speed:.2f} MB/s | {vid_pct}%'
                    else:
                        msg = f'{vid_idx}/{total_vids} | {vid_pct}%'
                    if info:
                        msg += f' | {info}'
                    self.row_message_signal.update.emit(row, vid_idx, total_vids, msg)
                else:
                    done = self.success_count + self.fail_count
                    self.progress_signal.update.emit(
                        self.total_items or total_courses, done, self.success_count, self.fail_count)
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
                    'total_courses': info.get('total_courses'),
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                self.save_state_signal.trigger.emit()
            except Exception as e:
                log_cb(f'记录 last_success 失败: {e}')

        self._download_done = False
        self._row_progress.clear()
        self._row_vid_msg.clear()
        self._progress_delegate.clear_all()

        def target():
            try:
                def course_status_cb(course_index, course, status, message):
                    if status == 'success':
                        self.success_count += 1
                    elif status == 'failed':
                        self.fail_count += 1
                    self.row_status_signal.update.emit(course_index, status, message)
                    done = self.success_count + self.fail_count
                    self.progress_signal.update.emit(
                        self.total_items, done, self.success_count, self.fail_count)

                videodownloader.download_videos(
                    save_dir, csv_file, cookie,
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
                log_cb('全部任务完成')
            except Exception as e:
                log_cb(f'下载任务异常: {e}')
            finally:
                self._download_done = True
                self.download_done_signal.done.emit()

        threading.Thread(target=target, daemon=True).start()

    def get_cookie(self):
        cookie_file = os.path.join(tempfile.gettempdir(), 'classin_cookies.json')
        driver_path = self.driver_entry.text().strip() or None

        browser, bin_path = driver_auto.detect_browser_preference()
        if not browser:
            QMessageBox.critical(self, '错误', '未检测到 Microsoft Edge 或 Chrome 浏览器，请先点击"自动检测驱动"。')
            return

        base_dir = _get_base_dir()
        default_name = 'msedgedriver.exe' if browser == 'edge' else 'chromedriver.exe'
        if not driver_path or not os.path.exists(driver_path):
            driver_path = os.path.join(base_dir, default_name)
        if not os.path.exists(driver_path):
            QMessageBox.warning(self, '提示', '未找到驱动文件，请先点击"自动检测驱动"下载驱动。')
            return

        import_str = ('from selenium.webdriver.edge.service import Service'
                      if browser == 'edge' else 'from selenium.webdriver.chrome.service import Service')
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
            subprocess.Popen(
                [sys.executable, tmp_py],
                creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, 'CREATE_NEW_CONSOLE') else 0)
        except Exception:
            subprocess.Popen([sys.executable, tmp_py])
        QMessageBox.information(
            self, '操作提示',
            '已弹出浏览器，请在新窗口登录后等待自动保存 Cookie。登录完成后点击"导入Cookie"按钮。')

        def import_cookie():
            if os.path.exists(cookie_file):
                try:
                    with open(cookie_file, 'r', encoding='utf-8') as f:
                        cookies = json.load(f)
                    cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
                    self.cookie_text.setPlainText(cookie_str)
                    QMessageBox.information(self, '成功', 'Cookie 已导入并填入文本框！')
                except Exception as e:
                    QMessageBox.critical(self, '错误', f'导入Cookie失败: {e}')
            else:
                QMessageBox.warning(self, '未找到', '未检测到 cookie 文件，请确认已登录并等待脚本完成。')

        dlg = QDialog(self)
        dlg.setWindowTitle('导入 Cookie')
        dlg.setMinimumWidth(360)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel('登录完成后点击下方按钮导入 Cookie：'))
        btn = QPushButton('导入 Cookie')
        btn.setObjectName('primary')
        btn.clicked.connect(import_cookie)
        btn.clicked.connect(dlg.accept)
        layout.addWidget(btn, alignment=Qt.AlignCenter)
        layout.addStretch()
        dlg.exec()

    def verify_cookie(self):
        cookie = self.cookie_text.toPlainText().strip()
        if not cookie:
            QMessageBox.warning(self, '提示', '请先输入或获取Cookie')
            return
        self.append_log('正在验证Cookie有效性...')

        def do_verify():
            try:
                is_valid, message = videodownloader.check_cookie_validity(cookie, log_callback=self.append_log)
                self.cookie_valid_signal.result.emit(is_valid, message)
            except Exception as e:
                self.cookie_valid_signal.result.emit(False, f'验证过程出错: {e}')

        threading.Thread(target=do_verify, daemon=True).start()

    def browse_check_dir(self):
        d = QFileDialog.getExistingDirectory(self, '选择视频目录')
        if d:
            self.check_dir_entry.setText(d)

    def browse_check_csv(self):
        f, _ = QFileDialog.getOpenFileName(
            self, '选择文档文件', '',
            'CSV/XLSX Files (*.csv *.xlsx *.xls);;All Files (*)')
        if f:
            self.check_csv_entry.setText(f)

    def start_check(self):
        check_dir = self.check_dir_entry.text().strip()
        csv_file = self.check_csv_entry.text().strip()

        if not check_dir or not os.path.isdir(check_dir):
            QMessageBox.warning(self, '提示', '请选择有效的视频目录')
            return
        if not csv_file or not os.path.exists(csv_file):
            QMessageBox.warning(self, '提示', '请选择有效的文档文件')
            return

        self.btn_check_scan.setEnabled(False)
        self.check_log_area.clear()
        self.check_table.setRowCount(0)
        self.check_stats_label.setText('统计: 正在检查...')
        self.append_check_log(f'开始检查目录: {check_dir}')
        self.append_check_log(f'对比文档: {csv_file}')

        def do_check():
            try:
                courses = self._load_courses_any(csv_file)
                if not courses:
                    self.append_check_log('文档中没有找到课程记录')
                    self.check_result_signal.result.emit([])
                    return

                self.append_check_log(f'文档中共 {len(courses)} 条课程记录')

                video_exts = ('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.ts')
                results = []

                for course in courses:
                    class_name = course.get('class_name', '').strip()
                    lesson_name = course.get('lesson_name', '').strip()
                    start_time = course.get('start_time', '').strip()

                    expected_folder = self._format_expected_folder(class_name, start_time)
                    actual_path = os.path.join(check_dir, expected_folder)

                    if not os.path.isdir(actual_path):
                        results.append({
                            'class_name': class_name,
                            'lesson_name': lesson_name,
                            'start_time': start_time,
                            'status': '未下载',
                            'folder': expected_folder,
                            'detail': f'文件夹不存在: {expected_folder}'
                        })
                        self.append_check_log(f'[未下载] {class_name} - {lesson_name}: 文件夹不存在')
                    else:
                        video_files = []
                        for root, dirs, files in os.walk(actual_path):
                            for fn in files:
                                if fn.lower().endswith(video_exts):
                                    video_files.append(os.path.join(root, fn))

                        if video_files:
                            total_size = sum(os.path.getsize(f) for f in video_files)
                            size_mb = total_size / (1024 * 1024)
                            results.append({
                                'class_name': class_name,
                                'lesson_name': lesson_name,
                                'start_time': start_time,
                                'status': '已下载',
                                'folder': expected_folder,
                                'detail': f'{len(video_files)} 个视频文件, 共 {size_mb:.1f} MB'
                            })
                            self.append_check_log(f'[已下载] {class_name} - {lesson_name}: {len(video_files)} 个视频')
                        else:
                            results.append({
                                'class_name': class_name,
                                'lesson_name': lesson_name,
                                'start_time': start_time,
                                'status': '部分下载',
                                'folder': expected_folder,
                                'detail': f'文件夹存在但无视频文件'
                            })
                            self.append_check_log(f'[部分下载] {class_name} - {lesson_name}: 文件夹存在但无视频文件')

                self.check_result_signal.result.emit(results)

            except Exception as e:
                self.append_check_log(f'检查过程出错: {e}')
                self.check_result_signal.result.emit([])

        threading.Thread(target=do_check, daemon=True).start()

    def _format_expected_folder(self, class_name, start_time):
        if start_time and len(start_time) >= 10:
            date_part = start_time[:10].replace('/', '-')
        else:
            date_part = '未知日期'
        return f'{date_part}_{class_name}'

    def _on_check_result(self, results):
        self.btn_check_scan.setEnabled(True)
        self.check_table.setRowCount(0)

        if not results:
            self.check_stats_label.setText('统计: 无检查结果')
            self.append_check_log('检查完成，无结果')
            return

        total = len(results)
        downloaded = sum(1 for r in results if r['status'] == '已下载')
        not_downloaded = sum(1 for r in results if r['status'] == '未下载')
        partial = sum(1 for r in results if r['status'] == '部分下载')

        self.check_stats_label.setText(
            f'统计: 共 {total} 条 | 已下载 {downloaded} | 未下载 {not_downloaded} | 部分下载 {partial}'
        )

        status_colors = {
            '已下载': QColor('#D1E7DD'),
            '未下载': QColor('#F8D7DA'),
            '部分下载': QColor('#FFF3CD'),
        }

        for r in results:
            row = self.check_table.rowCount()
            self.check_table.insertRow(row)
            values = [
                r['class_name'],
                r['lesson_name'],
                r['start_time'],
                r['status'],
                r['folder'],
                r['detail']
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                self.check_table.setItem(row, col, item)
                bg = status_colors.get(r['status'])
                if bg:
                    item.setBackground(bg)

        self.append_check_log(f'检查完成: 共 {total} 条，已下载 {downloaded}，未下载 {not_downloaded}，部分下载 {partial}')

    def on_check_filter_change(self, filter_key):
        if filter_key == '全部':
            for row in range(self.check_table.rowCount()):
                self.check_table.setRowHidden(row, False)
            return

        filter_map = {
            '已下载': '已下载',
            '未下载': '未下载',
            '部分下载': '部分下载'
        }
        target_status = filter_map.get(filter_key)
        if target_status:
            for row in range(self.check_table.rowCount()):
                status_item = self.check_table.item(row, 3)
                status = status_item.text() if status_item else ''
                self.check_table.setRowHidden(row, status != target_status)

    def append_check_log(self, msg):
        self.check_log_signal.emit(str(msg))

    def _append_check_log_safe(self, msg):
        self.check_log_area.append(str(msg))
        sb = self.check_log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

    def export_check_result(self):
        try:
            if self.check_table.rowCount() == 0:
                QMessageBox.warning(self, '提示', '没有可导出的检查结果')
                return

            default_filename = f'下载检查结果_{time.strftime("%Y%m%d_%H%M%S")}.csv'
            f, _ = QFileDialog.getSaveFileName(
                self, '导出检查结果', default_filename,
                'CSV文件 (*.csv);;All Files (*)')
            if f:
                import csv
                with open(f, 'w', encoding='utf-8-sig', newline='') as file:
                    writer = csv.writer(file)
                    headers = ['班级名称', '课堂名称', '开课时间', '状态', '文件夹', '详情']
                    writer.writerow(headers)
                    for row in range(self.check_table.rowCount()):
                        values = []
                        for col in range(self.check_table.columnCount()):
                            item = self.check_table.item(row, col)
                            values.append(item.text() if item else '')
                        writer.writerow(values)
                QMessageBox.information(self, '成功', f'检查结果已导出到:\n{f}')
                self.append_check_log(f'检查结果已导出: {f}')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'导出检查结果失败: {e}')
            self.append_check_log(f'导出检查结果失败: {e}')


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
