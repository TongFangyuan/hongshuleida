from __future__ import annotations

import asyncio
import csv
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QRadioButton,
    QPlainTextEdit, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)
from PySide6.QtCore import QUrl

from core.collector import ProductCollector
from core.config import SHANGHAI_TZ, app_paths
from core.database import Database
from core.services import MonitoringService

TZ = ZoneInfo(SHANGHAI_TZ)
GREEN = "#25864b"


class AsyncWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: Callable[[], object]):
        super().__init__(); self.operation = operation

    def run(self) -> None:
        try:
            result = self.operation()
            if asyncio.iscoroutine(result): result = asyncio.run(result)
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def item(value: object) -> QTableWidgetItem:
    cell = QTableWidgetItem("—" if value is None else str(value))
    cell.setToolTip(cell.text())
    return cell


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        paths = app_paths()
        self.db = Database(paths.database)
        settings = self.db.settings()
        self.collector = ProductCollector(paths.browser_profile, float(settings.get("interval_min_seconds", "0.1")), float(settings.get("interval_max_seconds", "0.3")))
        self.service = MonitoringService(self.db, self.collector)
        self.setWindowTitle("红薯雷达")
        self.resize(1280, 780); self.setMinimumSize(1050, 650)
        self._build(); self.refresh_all()

    def _build(self) -> None:
        root = QWidget(); layout = QVBoxLayout(root); layout.setContentsMargins(18, 12, 18, 12)
        top = QHBoxLayout()
        warning = QLabel("仅供学习交流，严禁用于商业用途，请于24小时内删除。")
        warning.setStyleSheet("color:#7a7f83")
        self.status = QLabel("运行状态：待命")
        self.usage = QLabel()
        author = QLabel('<a href="https://scys.com/personal/3941891?number=201255&tab=posts">by：冬青</a>')
        author.setOpenExternalLinks(True)
        top.addWidget(warning); top.addStretch(); top.addWidget(self.usage); top.addWidget(self.status); top.addWidget(author)
        layout.addLayout(top)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        self.tabs.addTab(self._dashboard_tab(), "竞品看板")
        self.tabs.addTab(self._add_tab(), "添加商品")
        self.tabs.addTab(self._failures_tab(), "失败列表")
        self.tabs.addTab(self._delisted_tab(), "下架列表")
        self.tabs.addTab(self._shops_tab(), "店铺分析")
        self.tabs.addTab(self._settings_tab(), "设置")
        self.tabs.addTab(self._logs_tab(), "运行日志")
        self.setCentralWidget(root)

    def _dashboard_tab(self) -> QWidget:
        tab = QWidget(); box = QVBoxLayout(tab); tools = QHBoxLayout()
        tools.addWidget(QLabel("竞品监控看板")); self.search = QLineEdit(); self.search.setPlaceholderText("搜索商品、店铺或商品 ID")
        self.search.textChanged.connect(lambda: self.refresh_dashboard()); tools.addWidget(self.search); tools.addStretch()
        for title, handler in [("添加商品", lambda: self.tabs.setCurrentIndex(1)), ("智能去重", self.show_duplicates), ("立即采集", self.collect_selected), ("刷新", self.refresh_all)]:
            button = QPushButton(title); button.clicked.connect(handler); tools.addWidget(button)
        box.addLayout(tools)
        self.dashboard = QTableWidget(); self.dashboard.setColumnCount(9); self.dashboard.setHorizontalHeaderLabels(["商品名称", "店铺", "当前价格", "今日销量", "昨日销量", "上小时销量", "累计已售", "最近采集", "状态"])
        self.dashboard.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.dashboard.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); self.dashboard.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dashboard.doubleClicked.connect(self.show_product); box.addWidget(self.dashboard)
        return tab

    def _add_tab(self) -> QWidget:
        tab = QWidget(); box = QVBoxLayout(tab); box.addWidget(QLabel("每行输入一个小红书分享口令、xhslink.com 短链、商品链接或 24 位商品 ID。"))
        self.add_input = QPlainTextEdit(); self.add_input.setPlaceholderText("https://www.xiaohongshu.com/goods-detail/...\n一行一个")
        box.addWidget(self.add_input); buttons = QHBoxLayout(); preview = QPushButton("采集预览")
        preview.clicked.connect(self.preview_products); buttons.addWidget(preview); buttons.addStretch(); box.addLayout(buttons)
        self.preview_table = QTableWidget(); self.preview_table.setColumnCount(6); self.preview_table.setHorizontalHeaderLabels(["加入", "商品", "店铺", "价格", "累计已售", "结果"]); box.addWidget(self.preview_table)
        confirm = QPushButton("确认加入勾选商品"); confirm.clicked.connect(self.confirm_products); box.addWidget(confirm)
        self.preview_rows: list[dict] = []
        return tab

    def _failures_tab(self) -> QWidget:
        tab = QWidget(); box = QVBoxLayout(tab); refresh = QPushButton("刷新"); refresh.clicked.connect(self.refresh_failures); box.addWidget(refresh)
        self.failures = QTableWidget(); self.failures.setColumnCount(5); self.failures.setHorizontalHeaderLabels(["商品名称", "店铺", "商品 ID", "最近失败时间", "失败原因"]); box.addWidget(self.failures); return tab

    def _delisted_tab(self) -> QWidget:
        tab = QWidget(); box = QVBoxLayout(tab); self.delisted = QTableWidget(); self.delisted.setColumnCount(5); self.delisted.setHorizontalHeaderLabels(["商品名称", "店铺", "商品 ID", "下架时间", "下架状态"]); self.delisted.doubleClicked.connect(self.show_product); box.addWidget(self.delisted); return tab

    def _shops_tab(self) -> QWidget:
        tab = QWidget(); box = QVBoxLayout(tab); box.addWidget(QLabel("店铺分析（店铺销量为已监控商品销量之和）")); self.shops = QTableWidget(); self.shops.setColumnCount(6); self.shops.setHorizontalHeaderLabels(["店铺", "监控商品数", "累计已售", "今日销量", "昨日销量", "上小时销量"]); box.addWidget(self.shops); return tab

    def _settings_tab(self) -> QWidget:
        content = QWidget(); form = QFormLayout(content)
        self.period = QSpinBox(); self.period.setRange(1, 1440); self.period.setValue(int(self.db.get_setting("collection_period_minutes", "60"))); self.period.setSuffix(" 分钟")
        self.min_delay = QLineEdit(self.db.get_setting("interval_min_seconds", "0.1")); self.max_delay = QLineEdit(self.db.get_setting("interval_max_seconds", "0.3"))
        public = self.service.settings_public()
        self.wecom_webhook = QLineEdit(public.get("wecom_webhook") or ""); self.feishu_webhook = QLineEdit(public.get("feishu_webhook") or "")
        self.channel_group = QButtonGroup(content)
        self.channel_wecom = QRadioButton("企业微信"); self.channel_feishu = QRadioButton("飞书")
        self.channel_group.addButton(self.channel_wecom); self.channel_group.addButton(self.channel_feishu)
        feishu_selected = self.db.get_setting("notify_channel") == "feishu"
        self.channel_feishu.setChecked(feishu_selected); self.channel_wecom.setChecked(not feishu_selected)
        channels = QWidget(); channels_box = QHBoxLayout(channels); channels_box.setContentsMargins(0, 0, 0, 0)
        channels_box.addWidget(self.channel_wecom); channels_box.addWidget(self.channel_feishu); channels_box.addStretch()
        self.enabled = QCheckBox("启用通知"); self.enabled.setChecked(self.db.get_setting("notify_enabled", self.db.get_setting("wecom_enabled")) == "true")
        form.addRow("采集周期", self.period); form.addRow("商品间隔最小秒数", self.min_delay); form.addRow("商品间隔最大秒数", self.max_delay); form.addRow("通知渠道（二选一）", channels); form.addRow("企业微信 Webhook", self.wecom_webhook); form.addRow("飞书 Webhook", self.feishu_webhook); form.addRow("通知", self.enabled)
        save = QPushButton("保存设置"); save.clicked.connect(self.save_settings); test = QPushButton("发送测试通知"); test.clicked.connect(self.test_notify); form.addRow(save, test)
        form.addRow(QLabel("MCP 只读：使用 mcp_server.py 或打包后的 红薯雷达MCP，和本程序读取同一个数据目录中的 monitor.db。"))
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(content); return scroll

    def _logs_tab(self) -> QWidget:
        tab = QWidget(); box = QVBoxLayout(tab); bar = QHBoxLayout(); refresh = QPushButton("刷新"); refresh.clicked.connect(self.refresh_logs); clear = QPushButton("清空日志")
        clear.clicked.connect(self.clear_logs); bar.addWidget(refresh); bar.addWidget(clear); bar.addStretch(); box.addLayout(bar)
        self.logs = QTableWidget(); self.logs.setColumnCount(6); self.logs.setHorizontalHeaderLabels(["时间", "级别", "来源", "商品", "摘要", "详情"]); box.addWidget(self.logs); return tab

    def run_worker(self, operation: Callable[[], object], done: Callable[[object], None] | None = None) -> None:
        thread = QThread(self); worker = AsyncWorker(operation); worker.moveToThread(thread)
        thread.started.connect(worker.run); worker.finished.connect(lambda result: (done and done(result), thread.quit(), worker.deleteLater()))
        worker.failed.connect(lambda message: (QMessageBox.warning(self, "任务失败", message), thread.quit(), worker.deleteLater()))
        thread.finished.connect(thread.deleteLater); thread.start()

    def preview_products(self) -> None:
        entries = self.add_input.toPlainText().splitlines()
        self.status.setText("运行状态：正在采集预览")
        self.run_worker(lambda: self.service.preview_inputs(entries), self._render_preview)

    def _render_preview(self, result: dict) -> None:
        self.preview_rows = result["items"]; self.preview_table.setRowCount(len(self.preview_rows))
        for i, row in enumerate(self.preview_rows):
            product = row.get("product") or {}; check = QCheckBox(); check.setChecked(row["state"] == "success"); self.preview_table.setCellWidget(i, 0, check)
            for col, key in enumerate(("title", "shop_name", "price", "sold"), 1): self.preview_table.setItem(i, col, item(product.get(key)))
            self.preview_table.setItem(i, 5, item(row["state"] if not row.get("reason") else f"{row['state']}：{row['reason']}"))
        self.status.setText("运行状态：待确认")
        QMessageBox.information(self, "批量预览", "\n".join(f"{k}: {v}" for k, v in result["summary"].items() if v))

    def confirm_products(self) -> None:
        selected = [row["product"] for i, row in enumerate(self.preview_rows) if row.get("product") and self.preview_table.cellWidget(i, 0).isChecked()]
        count = self.service.confirm_products(selected); QMessageBox.information(self, "添加完成", f"已加入 {count} 个商品"); self.refresh_all()

    def collect_selected(self) -> None:
        ids = [self.dashboard.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in self.dashboard.selectionModel().selectedRows()]
        self.status.setText("运行状态：正在采集")
        self.run_worker(lambda: self.service.collect_products(ids or None), lambda result: (self.status.setText(f"运行状态：{result['state']}"), self.refresh_all()))

    def refresh_dashboard(self) -> None:
        rows = self.service.dashboard(self.search.text())
        self.dashboard.setRowCount(len(rows))
        for i, row in enumerate(rows):
            fields = ("title", "shop_name", "price", "today_sales", "yesterday_sales", "last_hour_sales", "cumulative_sold", "data_at", "status")
            for j, key in enumerate(fields):
                cell = item(row.get(key)); self.dashboard.setItem(i, j, cell)
            self.dashboard.item(i, 0).setData(Qt.ItemDataRole.UserRole, row["id"])
        self.dashboard.resizeColumnsToContents()

    def refresh_failures(self) -> None:
        rows = [p for p in self.db.products() if p.get("last_error")]
        self.failures.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("title", "shop_name", "id", "created_at", "last_error")): self.failures.setItem(i, j, item(row.get(key)))

    def refresh_delisted(self) -> None:
        rows = [p for p in self.db.products(True) if not p["active"]]
        self.delisted.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("title", "shop_name", "id", "delisted_at", "delisted_reason")): self.delisted.setItem(i, j, item(row.get(key)))
            self.delisted.item(i, 0).setData(Qt.ItemDataRole.UserRole, row["id"])

    def refresh_shops(self) -> None:
        rows = self.service.shops(); self.shops.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("shop_name", "product_count", "cumulative_sold", "today_sales", "yesterday_sales", "last_hour_sales")): self.shops.setItem(i, j, item(row.get(key)))

    def refresh_logs(self) -> None:
        rows = self.db.logs()
        self.logs.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("created_at", "level", "source", "product_title", "message", "detail")): self.logs.setItem(i, j, item(row.get(key)))

    def refresh_all(self) -> None:
        self.refresh_dashboard(); self.refresh_failures(); self.refresh_delisted(); self.refresh_shops(); self.refresh_logs()
        usage = self.db.data_usage(); self.usage.setText(f"数据占用：{usage['bytes'] / 1024 / 1024:.1f}MB · {usage['snapshots']:,}条")

    def save_settings(self) -> None:
        from core.notify import is_masked, notification_error
        try:
            values = {"collection_period_minutes": str(self.period.value()), "interval_min_seconds": str(float(self.min_delay.text())), "interval_max_seconds": str(float(self.max_delay.text())), "notify_channel": "feishu" if self.channel_feishu.isChecked() else "wecom", "notify_enabled": str(self.enabled.isChecked()).lower()}
            for field, key in ((self.wecom_webhook, "wecom_webhook"), (self.feishu_webhook, "feishu_webhook")):
                if field.text() and not is_masked(field.text()): values[key] = field.text()
            error = notification_error({**self.db.settings(), **values})
            if error: QMessageBox.warning(self, "设置", error); return
            for key, value in values.items(): self.db.set_setting(key, value)
            QMessageBox.information(self, "设置", "设置已保存")
        except ValueError: QMessageBox.warning(self, "设置", "间隔必须是有效数字")

    def test_notify(self) -> None:
        from core.notify import resolve_sender, test_message
        resolved = resolve_sender(self.db.settings())
        if not resolved: QMessageBox.warning(self, "通知", "请先启用通知并配置所选渠道的 Webhook"); return
        channel, webhook, sender = resolved
        self.run_worker(lambda: sender(webhook, test_message(channel)), lambda _: QMessageBox.information(self, "通知", "测试发送成功"))

    def clear_logs(self) -> None:
        if QMessageBox.question(self, "确认", "确定清空全部运行日志？") == QMessageBox.StandardButton.Yes: self.db.clear_logs(); self.refresh_logs()

    def show_product(self) -> None:
        source = self.sender(); index = source.currentIndex() if hasattr(source, "currentIndex") else None
        product_id = source.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) if index else None
        product = self.db.product(product_id) if product_id else None
        if not product: return
        dashboard_rows = self.service.dashboard(product_id)
        text = "\n".join(f"{k}: {v}" for k, v in {**product, **(dashboard_rows[0] if dashboard_rows else {})}.items())
        QMessageBox.information(self, "单品数据", text)

    def show_duplicates(self) -> None:
        groups = self.service.smart_duplicates()
        if not groups: QMessageBox.information(self, "智能去重", "没有符合全部严格条件的重复商品。"); return
        message = "\n\n".join("\n".join(f"{x['title']} | ¥{x['price']} | {x['id']}" for x in group) for group in groups)
        QMessageBox.information(self, "重复候选（每组第一条为保留项）", message)

    def closeEvent(self, event) -> None:
        asyncio.run(self.collector.close()); event.accept()


def run() -> None:
    app = QApplication(sys.argv); app.setStyleSheet(f"QPushButton {{ background:{GREEN}; color:white; padding:6px 12px; border-radius:4px; }} QTableWidget {{ gridline-color:#e1e5e2; }}")
    window = MainWindow(); window.show(); sys.exit(app.exec())
