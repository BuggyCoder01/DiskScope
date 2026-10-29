"""Qt user interface. The core package never imports PySide6."""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..cancellation import CancelToken
from ..models import DirNode, FileKind, Progress, SpaceScanResult, format_size
from ..space import (
    ConfirmationRequired,
    TrashError,
    UnsafePathError,
    move_to_recycle_bin,
    node_for_path,
    scan,
)

_POPULATED_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class ScanWorker(QObject):
    """Runs a core scan in a worker thread and relays plain data."""

    progressed = Signal(int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, root: str, top_n: int, allocated: bool) -> None:
        super().__init__()
        self._root = root
        self._top_n = top_n
        self._allocated = allocated
        self._token = CancelToken()

    def cancel(self) -> None:
        self._token.cancel()

    def run(self) -> None:
        try:
            result = scan(
                self._root,
                cancel=self._token,
                progress=self._on_progress,
                top_n=self._top_n,
                allocated=self._allocated,
            )
        except Exception as exc:  # noqa: BLE001 - relay the message to the UI
            self.failed.emit(str(exc))
            return
        self.completed.emit(result)

    def _on_progress(self, update: Progress) -> None:
        self.progressed.emit(update.done, update.detail)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DiskScope")
        self.resize(1100, 700)
        self._root: str | None = None
        self._result: SpaceScanResult | None = None
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        controls = QHBoxLayout()
        self.choose_button = QPushButton("Choose folder...")
        self.path_label = QLabel("No folder selected")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.scan_button = QPushButton("Scan")
        self.scan_button.setEnabled(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All types", None)
        for kind in FileKind:
            self.filter_combo.addItem(kind.label, kind)
        self.top_spin = QSpinBox()
        self.top_spin.setRange(10, 1000)
        self.top_spin.setValue(100)
        self.top_spin.setPrefix("Top ")
        self.allocated_check = QCheckBox("Allocated size (slow)")
        self.allocated_check.setToolTip(
            "Measure size on disk with GetCompressedFileSizeW instead of logical size."
        )
        self.delete_button = QPushButton("Move to Recycle Bin")
        self.delete_button.setEnabled(False)

        controls.addWidget(self.choose_button)
        controls.addWidget(self.path_label, 1)
        controls.addWidget(self.scan_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.filter_combo)
        controls.addWidget(self.top_spin)
        controls.addWidget(self.allocated_check)
        controls.addWidget(self.delete_button)
        layout.addLayout(controls)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("Folder", "Logical size"))
        self.tree.setColumnWidth(0, 340)
        self.tree.setAlternatingRowColors(True)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("File", "Size", "Type", "Path"))
        self.table.setColumnWidth(0, 260)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 100)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        splitter.addWidget(self.tree)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setMaximumWidth(220)
        self.progress_bar.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().showMessage("Choose a folder to begin.")

        self.choose_button.clicked.connect(self._choose_folder)
        self.scan_button.clicked.connect(self._start_scan)
        self.cancel_button.clicked.connect(self._cancel_scan)
        self.filter_combo.currentIndexChanged.connect(self._refresh_table)
        self.top_spin.valueChanged.connect(self._refresh_table)
        self.tree.itemExpanded.connect(self._on_tree_expanded)
        self.table.itemSelectionChanged.connect(self._update_delete_button)
        self.delete_button.clicked.connect(self._delete_selected)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose a folder to scan",
            self._root or "",
        )
        if folder:
            self._root = folder
            self.path_label.setText(folder)
            self.scan_button.setEnabled(True)
            self.statusBar().showMessage("Ready to scan.")

    def _start_scan(self) -> None:
        if self._thread is not None or not self._root:
            return
        self._result = None
        self.tree.clear()
        self.table.setRowCount(0)
        self.delete_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.allocated_check.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.statusBar().showMessage("Scanning...")

        thread = QThread(self)
        worker = ScanWorker(
            self._root,
            self.top_spin.value(),
            self.allocated_check.isChecked(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _cancel_scan(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Cancelling...")

    def _on_progress(self, done: int, detail: str) -> None:
        self.statusBar().showMessage(f"Scanning: {done:,} entries - {detail}")

    def _on_completed(self, result: object) -> None:
        if not isinstance(result, SpaceScanResult):
            self._on_failed("scan returned an unexpected result")
            return
        self._result = result
        self._populate_tree(result)
        self._refresh_table()
        if result.cancelled:
            message = "Scan cancelled - results are partial."
        elif not result.complete:
            message = f"Scan incomplete - {len(result.skipped):,} item(s) skipped."
        else:
            message = (
                f"Scan complete - {format_size(result.total_size)} in {result.file_count:,} files."
            )
        if result.allocated:
            if result.total_allocated_size is None:
                message += " Allocated size unavailable for some files."
            else:
                message += f" Allocated: {format_size(result.total_allocated_size)}."
        self.statusBar().showMessage(message)

    def _on_failed(self, message: str) -> None:
        self.progress_bar.setVisible(False)
        self.statusBar().showMessage("Scan failed.")
        QMessageBox.critical(self, "Scan failed", message)

    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.choose_button.setEnabled(True)
        self.scan_button.setEnabled(self._root is not None)
        self.cancel_button.setEnabled(False)
        self.allocated_check.setEnabled(True)
        self.progress_bar.setVisible(False)

    def _populate_tree(self, result: SpaceScanResult) -> None:
        self.tree.clear()
        root = result.root
        item = QTreeWidgetItem((root.name or root.path, format_size(root.size)))
        item.setData(0, Qt.ItemDataRole.UserRole, root.path)
        self.tree.addTopLevelItem(item)
        self._populate_tree_item(item, root)
        item.setExpanded(True)

    def _populate_tree_item(self, item: QTreeWidgetItem, node: object) -> None:
        if item.data(0, _POPULATED_ROLE):
            return
        if not isinstance(node, DirNode):
            return
        item.takeChildren()
        for child in node.sorted_children():
            child_item = QTreeWidgetItem((child.name, format_size(child.size)))
            child_item.setData(0, Qt.ItemDataRole.UserRole, child.path)
            if child.children:
                child_item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
                )
            item.addChild(child_item)
        item.setData(0, _POPULATED_ROLE, True)

    def _on_tree_expanded(self, item: QTreeWidgetItem) -> None:
        if self._result is None:
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(path, str):
            return
        try:
            node = node_for_path(self._result, path)
        except (KeyError, UnsafePathError):
            return
        self._populate_tree_item(item, node)

    def _refresh_table(self) -> None:
        self.table.setRowCount(0)
        if self._result is None:
            return
        kind = self.filter_combo.currentData()
        entries = self._result.top_files(kind, self.top_spin.value())
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QTableWidgetItem(entry.name)
            name_item.setData(Qt.ItemDataRole.UserRole, entry.path)
            size_item = QTableWidgetItem(format_size(entry.size))
            kind_item = QTableWidgetItem(entry.kind.label)
            path_item = QTableWidgetItem(entry.path)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, size_item)
            self.table.setItem(row, 2, kind_item)
            self.table.setItem(row, 3, path_item)
        self._update_delete_button()

    def _update_delete_button(self) -> None:
        self.delete_button.setEnabled(self._result is not None and self.table.currentRow() >= 0)

    def _delete_selected(self) -> None:
        if self._result is None:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(path, str):
            return
        answer = QMessageBox.question(
            self,
            "Move to Recycle Bin",
            f"Move this item to the Recycle Bin?\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            moved = move_to_recycle_bin(
                path,
                scan_root=self._result.root.path,
                confirmed=True,
            )
        except (OSError, UnsafePathError, ConfirmationRequired, TrashError) as exc:
            QMessageBox.critical(self, "Recycle Bin failed", str(exc))
            return
        for index in range(self.table.rowCount()):
            candidate = self.table.item(index, 0)
            if candidate is not None and candidate.data(Qt.ItemDataRole.UserRole) == str(moved):
                self.table.removeRow(index)
                break
        self.delete_button.setEnabled(False)
        self.statusBar().showMessage(f"Moved to Recycle Bin: {moved}. Rescan to refresh totals.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None and self._thread.isRunning():
            if self._worker is not None:
                self._worker.cancel()
            self._thread.quit()
            if not self._thread.wait(5000):
                QMessageBox.warning(
                    self,
                    "Scan still running",
                    "The scan is still stopping. Try again in a moment.",
                )
                event.ignore()
                return
        event.accept()


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
