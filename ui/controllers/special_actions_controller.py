"""删除功能控制器（special_status 4 操作）。

组合模式（非继承）；显式依赖 main_window 公开接口；
信号容错（view 未加信号时静默跳过）。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from medlitflow import robustness

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow

class SpecialActionsController(QObject):
    """special_status 删除功能控制器（组合模式）。"""

    def __init__(self, main_window: "MedLitFlowMainWindow"):
        super().__init__(main_window)
        self._mw = main_window
        self._connect_signals()

    def _connect_signals(self) -> None:
        lit = getattr(self._mw, "literature_view", None)
        scr = getattr(self._mw, "screening_view", None)

        if lit is not None:
            for sig_name, handler in (
                ("mark_noise_requested", self.mark_noise),
                ("mark_deleted_requested", self.mark_deleted),
                ("restore_special_requested", self.restore_special),
                ("hard_delete_requested", self.hard_delete),
            ):
                sig = getattr(lit, sig_name, None)
                if sig is not None:
                    try:
                        sig.connect(handler)
                    except Exception as exc:
                        print(
                            f"[SpecialActionsController] 连接 "
                            f"literature_view.{sig_name} 失败：{exc}",
                            file=sys.stderr,
                        )

        if scr is not None:
            for sig_name, handler in (
                ("mark_noise_requested", self.mark_noise),
                ("restore_special_requested", self.restore_special),
                ("hard_delete_requested", self.hard_delete),
            ):
                sig = getattr(scr, sig_name, None)
                if sig is not None:
                    try:
                        sig.connect(handler)
                    except Exception as exc:
                        print(
                            f"[SpecialActionsController] 连接 "
                            f"screening_view.{sig_name} 失败：{exc}",
                            file=sys.stderr,
                        )

    def mark_noise(self, cids: list) -> None:
        if not self._precheck(cids):
            return
        if not self._confirm_mark_noise(cids):
            return

        from medlitflow import screening
        self._execute(
            lambda config, root: screening.mark_as_retrieval_noise(
                cids, root, config,
            ),
            "标记为检索噪声",
            selected_cids=cids,
        )

    def mark_deleted(self, cids: list) -> None:
        if not self._precheck(cids):
            return
        if not self._confirm_mark_deleted(cids):
            return

        from medlitflow import screening
        self._execute(
            lambda config, root: screening.mark_as_deleted_from_source(
                cids, root, config,
            ),
            "标记为数据源删除",
            selected_cids=cids,
        )

    def restore_special(self, cids: list) -> None:
        if not self._precheck(cids):
            return
        if not self._confirm_restore(cids):
            return

        from medlitflow import screening
        self._execute(
            lambda config, root: screening.restore_special_status(
                cids, root, config,
            ),
            "恢复",
            selected_cids=cids,
        )

    def hard_delete(self, cids: list) -> None:
        if not self._precheck(cids):
            return

        valid_cids = self._filter_deleted_from_source(cids)
        if not valid_cids:
            self._mw.notify_user(
                "warning",
                f"所选 {len(cids)} 篇文献状态不是「数据源已删除」，"
                f"无法彻底删除",
            )
            return

        if len(valid_cids) < len(cids):
            skipped = len(cids) - len(valid_cids)
            self._mw.notify_user(
                "info",
                f"已跳过 {skipped} 篇状态不符的文献，只处理 "
                f"{len(valid_cids)} 篇「数据源已删除」文献",
            )

        if not self._confirm_hard_delete(valid_cids):
            return

        from medlitflow import screening
        self._execute(
            lambda config, root: screening.hard_delete_literature(
                valid_cids, root, config,
            ),
            "彻底删除",
            selected_cids=valid_cids,
        )


    def _filter_deleted_from_source(self, cids: list) -> list:
        if not cids:
            return []
        try:
            import pandas as pd
            library_root = self._mw.get_library_root()
            records_path = library_root / "records_final.parquet"
            if not records_path.exists():
                self._mw.notify_user(
                    "warning",
                    "records_final.parquet 不存在，已取消彻底删除",
                )
                return []
            records = pd.read_parquet(
                records_path, columns=["canonical_id", "special_status"],
            )
            if "special_status" not in records.columns:
                self._mw.notify_user(
                    "warning",
                    "records_final 缺少 special_status 列，已取消彻底删除",
                )
                return []
            sp = (
                records["special_status"].fillna("").astype(str).str.strip()
            )
            deleted_cids = set(
                records.loc[
                    sp.eq("deleted_from_source"), "canonical_id"
                ].astype(str).tolist()
            )
            return [cid for cid in cids if str(cid) in deleted_cids]
        except Exception as exc:
            self._mw.notify_user(
                "warning",
                f"读取文献状态失败（{type(exc).__name__}: {exc}），"
                f"已取消彻底删除",
            )
            return []

    def _precheck(self, cids) -> bool:
        if not cids:
            return False
        if self._mw.is_busy():
            self._mw.notify_user(
                "warning",
                "pipeline 运行中，删除操作暂不可用，请稍后再试",
            )
            return False
        return True

    def _confirm_mark_noise(self, cids) -> bool:
        from PySide6.QtWidgets import QMessageBox

        n = len(cids)
        reply = QMessageBox.question(
            self._mw,
            "标记为检索噪声",
            f"即将把 {n} 篇文献标记为检索噪声：\n\n"
            f"  · 删除已下载的全文（PDF/XML/HTML）\n"
            f"  · 删除摘要、引用、JCR、作者、机构等增强数据\n"
            f"  · 保留元数据（title/authors/journal/doi/pmid）供 PRISMA 追溯\n"
            f"  · 默认视图不再显示（切到「检索噪声」tab 可查看）\n\n"
            f"⚠ 若该文献属于多个策略，标记后所有策略下都会隐藏。\n\n"
            f"恢复时需重新检索获取全文和增强数据。\n\n"
            f"确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _confirm_mark_deleted(self, cids) -> bool:
        from PySide6.QtWidgets import QMessageBox

        n = len(cids)
        reply = QMessageBox.question(
            self._mw,
            "标记为数据源删除",
            f"即将把 {n} 篇文献标记为「数据源已删除」：\n\n"
            f"  · 不删除任何文件或数据\n"
            f"  · 默认视图不再显示（切到「数据源删除」tab 可查看）\n"
            f"  · 可随时恢复\n\n"
            f"确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        return reply == QMessageBox.Yes

    def _confirm_restore(self, cids) -> bool:
        from PySide6.QtWidgets import QMessageBox

        n = len(cids)
        reply = QMessageBox.question(
            self._mw,
            "恢复文献",
            f"即将恢复 {n} 篇文献到正常显示。\n\n"
            f"注意：若为检索噪声，已删除的全文和增强数据\n"
            f"需下次检索时重新获取（元数据已完整保留）。\n\n"
            f"确认恢复？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        return reply == QMessageBox.Yes

    def _confirm_hard_delete(self, cids) -> bool:
        from PySide6.QtWidgets import QMessageBox

        n = len(cids)
        reply = QMessageBox.warning(
            self._mw,
            "彻底删除文献",
            f"即将从所有表中彻底删除 {n} 篇文献：\n\n"
            f"  · records_final / file_index / screening_decisions 全部删行\n"
            f"  · 删除已下载的所有文件\n"
            f"  · archive 历史快照保留（PRISMA 追溯）\n\n"
            f"⚠ 此操作不可恢复。下次检索会重新抓取。\n\n"
            f"确认彻底删除？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _execute(
        self,
        fn,
        action_name: str,
        selected_cids: list | None = None,
    ) -> None:

        try:
            config = self._mw.get_config()
            library_root = self._mw.get_library_root()
            result = fn(config, library_root)
        except robustness.InterruptRequested:
            raise
        except Exception as exc:  # noqa: BLE001
            self._mw.notify_user("error", f"{action_name}失败：{exc}")
            return

        if not isinstance(result, dict) or not result.get("ok"):
            errors = (
                (result or {}).get("errors", [])
                if isinstance(result, dict) else []
            )
            err_text = "; ".join(str(e) for e in errors) if errors else "未知错误"
            self._mw.notify_user("error", f"{action_name}失败：{err_text}")
            return

        count = int(result.get("count", 0) or 0)
        freed_mb = int(result.get("freed_bytes", 0) or 0) / 1024 / 1024

        soft_errors = (
            (result or {}).get("errors", [])
            if isinstance(result, dict) else []
        )
        if soft_errors:
            err_text = "; ".join(str(e) for e in soft_errors)
            msg = f"已{action_name} {count} 篇（部分刷新失败：{err_text}）"
            if freed_mb > 0:
                msg += f"，释放 {freed_mb:.1f} MB"
            self._mw.notify_user("warning", msg)
        else:
            msg = f"已{action_name} {count} 篇"
            if selected_cids is not None:
                selected_n = len(selected_cids)
                if selected_n > count:
                    skipped = selected_n - count
                    msg += f"（跳过 {skipped} 篇，已是相同状态）"
            if freed_mb > 0:
                msg += f"，释放 {freed_mb:.1f} MB"
            self._mw.notify_user("info", msg)

        try:
            self._mw.refresh_after_data_change()
        except Exception:
            pass