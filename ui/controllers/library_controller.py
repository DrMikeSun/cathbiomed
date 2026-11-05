"""ui/controllers/library_controller.py — 文献库控制器（A2）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from PySide6.QtCore import QObject

from ui.config import PAGE_SCREENING
from ui.utils.formatting import (
    SOURCE_VALUE_TO_DISPLAY,
    all_sources,
    first_author,
    first_source,
    format_strategies_short,
    format_year,
)

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow

class LibraryController(QObject):

    SOURCE_DISPLAY_TO_VALUE = {
        f"含 {v}": k for k, v in SOURCE_VALUE_TO_DISPLAY.items()
    }

    STATUS_DISPLAY_TO_VALUES = {
        "未筛选": ("", "unscreened"),
        "已纳入": ("include",),
        "待定":   ("maybe",),
        "已排除": ("exclude",),
    }

    _SPECIAL_VISUALS = {
        "retrieval_noise": {
            "short": "🗑 噪声",
            "badge": "error",
            "text": "检索噪声",
        },
        "deleted_from_source": {
            "short": "⚠ 删除",
            "badge": "warning",
            "text": "数据源删除",
        },
    }

    def __init__(self, main_window: "MedLitFlowMainWindow"):
        super().__init__(main_window)
        self._mw = main_window
        self._library_records = None
        self._library_records_mtime = None
        self._literature_display_records = None
        self._zotero_import_display_records = None
        self._retrieval_search_saved: str = ""

    def load_library_records(self, force: bool = False):
        try:
            config = self._mw.get_config()
        except Exception:
            self._library_records = None
            return None

        try:
            library_root_raw = (config.get("library", {}) or {}).get(
                "root", ""
            )
            if not library_root_raw:
                self._library_records = None
                return None
            library_root = Path(library_root_raw)
            path = library_root / "records_final.parquet"

            def _safe_mtime(p):
                try:
                    return os.path.getmtime(p) if p.exists() else None
                except OSError:
                    return None

            fi_path_for_mtime = library_root / "file_index.parquet"
            current_mtimes = (
                _safe_mtime(path),
                _safe_mtime(fi_path_for_mtime),
            )

            if (not force
                    and self._library_records is not None
                    and self._library_records_mtime == current_mtimes):
                return self._library_records

            if not path.exists():
                self._library_records = None
                self._library_records_mtime = current_mtimes
                return None
            df = pd.read_parquet(path)
        except Exception as exc:
            try:
                self._mw.logs_view.append_log(
                    "error",
                    f"library records 加载失败：{exc}",
                    source="library",
                )
            except Exception:
                pass
            self._library_records = None
            return None

        try:
            fi_path = library_root / "file_index.parquet"
            if fi_path.exists():
                fi = pd.read_parquet(fi_path)
                if ("highest_priority_decision" in fi.columns
                        and "canonical_id" in fi.columns):
                    if "user_decision" in df.columns:
                        df = df.drop(columns=["user_decision"])
                    fi_map = fi.set_index("canonical_id")[
                        "highest_priority_decision"
                    ]
                    df["user_decision"] = (
                        df["canonical_id"].astype(str).map(fi_map)
                        .fillna("").astype(str).str.strip().str.lower()
                    )
        except Exception as exc:
            try:
                self._mw.logs_view.append_log(
                    "warning",
                    f"file_index 合并失败（用户决策显示可能过时）：{exc}",
                    source="library",
                )
            except Exception:
                pass

        self._library_records = df
        self._library_records_mtime = current_mtimes
        return df

    def get_detail_dict_for_record(self, record) -> dict:
        if record is None:
            return {}
        try:
            return {
                "source":   first_source(
                    record.get("source_databases", "")
                ),
                "title":    str(record.get("title", "") or ""),
                "authors":  str(record.get("authors", "") or ""),
                "journal":  str(record.get("journal", "") or ""),
                "year":     str(record.get("year", "") or ""),
                "doi":      str(record.get("doi", "") or ""),
                "pmid":     str(record.get("pmid", "") or ""),
                "status":      self._map_detail_status(record),
                "status_text": self._map_detail_status_text(record),
                "abstract":    str(
                    record.get("abstract_final", "") or ""
                ),
                "canonical_id":          str(
                    record.get("canonical_id", "") or ""
                ),
                "downloaded_file_path":  str(
                    record.get("downloaded_file_path", "") or ""
                ),
                "current_file_path":     str(
                    record.get("current_file_path", "") or ""
                ),
                "fulltext_url":          str(
                    record.get("fulltext_url", "") or ""
                ),
                "landing_page_url":      str(
                    record.get("landing_page_url", "") or ""
                ),
                "citation_count":             record.get("citation_count"),
                "reference_count":            record.get("reference_count"),
                "influential_citation_count": record.get(
                    "influential_citation_count"
                ),
                "journal_impact_factor":      record.get(
                    "journal_impact_factor"
                ),
                "journal_jcr_quartile":       record.get(
                    "journal_jcr_quartile"
                ),
                "author_count":               record.get("author_count"),
                "institution_count":          record.get(
                    "institution_count"
                ),
                "country_codes":              record.get("country_codes"),
                "publication_type_category":  record.get(
                    "publication_type_category"
                ),
                "open_access_flag":           record.get(
                    "open_access_flag", False
                ),
            }
        except Exception:
            return {}

    def maybe_reload_if_stale(self) -> None:
        try:
            config = self._mw.get_config()
            library_root_raw = (config.get("library", {}) or {}).get(
                "root", ""
            )
            if not library_root_raw:
                return
            library_root = Path(library_root_raw)

            def _safe_mtime(p):
                try:
                    return os.path.getmtime(p) if p.exists() else None
                except OSError:
                    return None

            current_mtimes = (
                _safe_mtime(library_root / "records_final.parquet"),
                _safe_mtime(library_root / "file_index.parquet"),
            )
            if current_mtimes == self._library_records_mtime:
                return
        except Exception:
            return

        self._mw._safe_refresh(
            "load_library_records",
            lambda: self.load_library_records(force=True),
        )
        self._mw._safe_refresh(
            "refresh_literature", self._refresh_literature,
        )
        self._mw._safe_refresh(
            "refresh_literature_tabs", self._refresh_literature_tabs,
        )
        self._mw._safe_refresh(
            "refresh_dashboard", self._mw._refresh_dashboard,
        )

    def on_filter_requested(self, filters):
        self._apply_literature_filter(filters or {})

    def on_reset_requested(self):
        try:
            self._mw.literature_view.special_toggle.set_current_key("")
        except Exception:
            pass
        self._apply_literature_filter({})

    def on_special_tab_changed(self, key):
        try:
            self._mw.literature_view.close_detail()
        except Exception:
            pass
        try:
            filters = self._mw.literature_view.filter_values()
        except Exception:
            filters = {}
        filters["special_tab"] = str(key or "")
        self._apply_literature_filter(filters)

    def on_tab_changed(self, tab_key: str):
        try:
            self._mw.literature_view.close_detail()
        except Exception:
            pass

        fb = self._mw.literature_view.filter_bar

        if tab_key == "retrieval":
            try:
                fb.set_values({"_search": self._retrieval_search_saved})
            except Exception as exc:
                try:
                    self._mw.logs_view.append_log(
                        "warning",
                        f"恢复检索关键词失败：{exc}",
                        source="literature",
                    )
                except Exception:
                    pass
            self._refresh_literature()
        else:
            try:
                self._retrieval_search_saved = str(
                    fb.values().get("_search", "") or ""
                )
            except Exception as exc:
                self._retrieval_search_saved = ""
                try:
                    self._mw.logs_view.append_log(
                        "warning",
                        f"读取当前检索关键词失败：{exc}",
                        source="literature",
                    )
                except Exception:
                    pass
            try:
                fb.set_values({"_search": ""})
            except Exception as exc:
                try:
                    self._mw.logs_view.append_log(
                        "warning",
                        f"清空检索关键词失败：{exc}",
                        source="literature",
                    )
                except Exception:
                    pass
            self._refresh_literature_tabs()

    def on_open_screening(self):
        self._mw.goto_page(PAGE_SCREENING)

    def on_row_clicked(self, row):
        tab = self._mw.literature_view.current_tab()
        if tab == "retrieval":
            display = self._literature_display_records
        else:
            display = self._zotero_import_display_records
        if display is None or len(display) == 0:
            return
        if row < 0 or row >= len(display):
            return
        try:
            r = display.iloc[row]
        except (IndexError, KeyError):
            return
        detail = self.get_detail_dict_for_record(r)
        if not detail:
            return
        self._mw.literature_view.set_detail(detail)
        cid = detail.get("canonical_id", "")
        zc = self._mw._zotero_controller
        if cid and zc is not None:
            try:
                zc.set_current_detail_cid(cid)
            except Exception:
                pass
            try:
                zdata = zc.get_zotero_section_data(cid, r)
                self._mw.literature_view.detail_panel.set_zotero_status(zdata)
            except Exception:
                pass

    def on_detail_action(self, action, data):
        if not isinstance(data, dict):
            return
        if action == "open":
            self._detail_action_open(data)
        elif action == "copy_doi":
            self._detail_action_copy_doi(data)
        elif action == "open_pdf":
            self._detail_action_open_pdf(data)
        elif action == "mark":
            self._detail_action_mark(data)

    def on_cleanup_orphans(self):
        if self._mw._has_running_worker():
            self._mw.notify_user(
                "warning", "有任务运行中，请先等待完成",
            )
            return

        try:
            config = self._mw.get_config()
            library_root = Path(config["library"]["root"])
        except Exception as exc:
            self._mw.notify_user("error", f"配置加载失败：{exc}")
            return

        records = self.load_library_records(force=True)
        orphans = self._compute_orphan_strategies(records)
        if not orphans:
            self._mw.notify_user("info", "无孤儿策略文献")
            return

        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QPushButton, QCheckBox, QScrollArea, QWidget,
        )
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QFrame as _QFrame

        dlg = QDialog(self._mw)
        dlg.setWindowTitle("清理孤儿策略文献")
        dlg.setMinimumWidth(560)

        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(10)

        total = sum(o["record_count"] for o in orphans)
        info = QLabel(
            f"检测到 <b>{len(orphans)}</b> 个孤儿策略，"
            f"共 <b>{total}</b> 篇文献。\n\n"
            f"这些策略已从归档删除（模式R），文献仍保留在库中。\n"
            f"勾选要清理的策略 → 将彻底删除对应文献（不可恢复）。"
        )
        info.setWordWrap(True)
        info.setTextFormat(_Qt.RichText)
        v.addWidget(info)
        v.addSpacing(6)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(_QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        list_container = QWidget()
        list_v = QVBoxLayout(list_container)
        list_v.setContentsMargins(0, 0, 0, 0)
        list_v.setSpacing(4)

        checkboxes = {}
        for o in orphans:
            cb = QCheckBox(
                f"{o['name']}  —  {o['record_count']} 篇"
            )
            cb.setChecked(True)
            checkboxes[o["name"]] = cb
            list_v.addWidget(cb)
        list_v.addStretch()
        scroll.setWidget(list_container)
        scroll.setMinimumHeight(200)
        v.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setMinimumWidth(90)
        cancel_btn.setCursor(_Qt.PointingHandCursor)
        confirm_btn = QPushButton("确认清理")
        confirm_btn.setObjectName("PrimaryCTA")
        confirm_btn.setMinimumWidth(110)
        confirm_btn.setCursor(_Qt.PointingHandCursor)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(confirm_btn)
        v.addLayout(btn_row)

        cancel_btn.clicked.connect(dlg.reject)
        confirm_btn.clicked.connect(dlg.accept)

        if dlg.exec() != QDialog.Accepted:
            self._mw.notify_user("info", "已取消清理")
            return

        selected = [
            o for o in orphans if checkboxes[o["name"]].isChecked()
        ]
        if not selected:
            self._mw.notify_user("info", "未选择任何策略")
            return

        self._mw._strategy_ctrl.launch_cleanup_orphans_worker(
            selected=selected,
            config=config,
            library_root=library_root,
        )

    def _refresh_literature(self):
        records = self._library_records
        if records is None:
            records = self.load_library_records()

        lv = self._mw.literature_view

        try:
            lv.set_year_options(
                self._compute_year_options(
                    records, data_origin_filter="retrieval",
                )
            )
        except Exception:
            pass

        if records is None:
            try:
                lv.set_literature([])
            except Exception:
                pass
            try:
                lv.set_special_tab_counts({})
            except Exception:
                pass
            try:
                lv.set_orphan_strategies([])
            except Exception:
                pass
            self._literature_display_records = None
            return

        try:
            orphans = self._compute_orphan_strategies(records)
            lv.set_orphan_strategies(orphans)
        except Exception:
            pass

        try:
            if "data_origin" in records.columns:
                _origin = (
                    records["data_origin"].fillna("retrieval").astype(str)
                )
                _retrieval_mask = _origin.eq("retrieval")
            else:
                _retrieval_mask = pd.Series(
                    [True] * len(records), index=records.index,
                )

            if "special_status" in records.columns:
                sp = (
                    records["special_status"].fillna("")
                    .astype(str).str.strip().str.lower()
                )
                counts = {
                    "": int((sp.eq("") & _retrieval_mask).sum()),
                    "retrieval_noise": int(
                        sp.eq("retrieval_noise").sum()
                    ),
                    "deleted_from_source": int(
                        sp.eq("deleted_from_source").sum()
                    ),
                }
            else:
                counts = {
                    "": int(_retrieval_mask.sum()),
                    "retrieval_noise": 0,
                    "deleted_from_source": 0,
                }
            lv.set_special_tab_counts(counts)
        except Exception:
            pass

        try:
            filters = lv.filter_values()
        except Exception:
            filters = {}
        try:
            filters["special_tab"] = lv.current_special_tab()
        except Exception:
            filters["special_tab"] = ""
        self._apply_literature_filter(filters)

    def _refresh_literature_tabs(self):
        records = self._library_records
        lv = self._mw.literature_view
        if records is None or len(records) == 0:
            lv.set_zotero_import_records([])
            self._zotero_import_display_records = None
            return

        if "data_origin" not in records.columns:
            origin = pd.Series(
                ["retrieval"] * len(records), index=records.index,
            )
        else:
            origin = records["data_origin"].fillna(
                "retrieval"
            ).astype(str)

        import_df = records[origin == "zotero_import"].reset_index(drop=True)

        collection_map: dict[str, str] = {}
        try:
            links_path = self._mw.get_library_root() / "zotero_links.parquet"
            if links_path.exists():
                links = pd.read_parquet(links_path)
                for lr in links.to_dict("records"):
                    lcid = str(lr.get("canonical_id", "") or "")
                    if not lcid:
                        continue
                    val = str(
                        lr.get("zotero_collection", "") or ""
                    ).strip()
                    if not val:
                        if lcid not in collection_map:
                            collection_map[lcid] = ""
                        continue
                    existing = collection_map.get(lcid, "")
                    if not existing:
                        collection_map[lcid] = val
                    else:
                        merged = set(
                            s.strip() for s in existing.split(";")
                            if s.strip()
                        )
                        for s in val.split(";"):
                            s_clean = s.strip()
                            if s_clean:
                                merged.add(s_clean)
                        collection_map[lcid] = "; ".join(sorted(merged))
        except Exception as exc:
            try:
                self._mw.logs_view.append_log(
                    "warning",
                    f"zotero_links 加载失败（分类列可能为空）：{exc}",
                    source="library",
                )
            except Exception:
                pass

        self._zotero_import_display_records = import_df
        lv.set_zotero_import_records(
            [
                self._record_to_zotero_import_row(r, collection_map)
                for r in import_df.to_dict("records")
            ],
        )

    def _apply_literature_filter(self, filters):
        lv = self._mw.literature_view
        running_preview = (
            self._mw._has_running_worker()
            and self._mw._running_preview_records is not None
            and len(self._mw._running_preview_records) > 0
        )
        if running_preview:
            records = self._mw._running_preview_records
        else:
            records = self._library_records
        if records is None or len(records) == 0:
            lv.set_literature([])
            self._literature_display_records = None
            return

        filters = filters or {}

        if "special_tab" not in filters:
            try:
                filters["special_tab"] = lv.current_special_tab()
            except Exception:
                filters["special_tab"] = ""

        special_tab = str(
            filters.get("special_tab", "") or ""
        ).strip().lower()

        if not running_preview and special_tab == "":
            if "data_origin" not in records.columns:
                origin = pd.Series(
                    ["retrieval"] * len(records), index=records.index,
                )
            else:
                origin = records["data_origin"].fillna(
                    "retrieval"
                ).astype(str)
            records = records[
                origin == "retrieval"
            ].reset_index(drop=True)
            if len(records) == 0:
                lv.set_literature([])
                self._literature_display_records = None
                return

        df = records

        if "special_status" in df.columns:
            sp = (
                df["special_status"].fillna("").astype(str)
                .str.strip().str.lower()
            )
            if special_tab == "":
                df = df[sp.eq("")]
            else:
                df = df[sp.eq(special_tab)]
        elif special_tab != "":
            df = df.iloc[0:0]

        total_for_filter = len(df)

        search = str(filters.get("_search", "") or "").strip()
        if search:
            mask = None
            for col in ("title", "authors", "doi"):
                if col not in df.columns:
                    continue
                m = df[col].fillna("").astype(str).str.contains(
                    search, case=False, na=False, regex=False,
                )
                mask = m if mask is None else (mask | m)
            if mask is not None:
                df = df[mask]

        source_filter = filters.get("source")
        if source_filter:
            src_value = self.SOURCE_DISPLAY_TO_VALUE.get(
                str(source_filter), ""
            )
            if src_value and "source_databases" in df.columns:
                pattern = rf"(?:^|;)\s*{re.escape(src_value)}\s*(?:;|$)"
                src_col = (
                    df["source_databases"].fillna("").astype(str)
                )
                df = df[src_col.str.contains(
                    pattern, regex=True, na=False, case=False,
                )]

        year_filter = filters.get("year")
        if year_filter and "year" in df.columns:
            target_year = str(year_filter).strip()
            try:
                target_int = int(target_year)
                year_numeric = pd.to_numeric(df["year"], errors="coerce")
                df = df[year_numeric == target_int]
            except (ValueError, TypeError):
                year_series = df["year"].apply(format_year)
                df = df[year_series == target_year]

        strategy_filter = filters.get("strategy")
        if strategy_filter:
            strat_pat = (
                rf"(?:^|;)\s*{re.escape(str(strategy_filter))}\s*(?:;|$)"
            )
            if "strategy_names" in df.columns:
                strat_col = df["strategy_names"].fillna("").astype(str)
                df = df[strat_col.str.contains(
                    strat_pat, regex=True, na=False,
                )]

        status_filter = filters.get("status")
        if status_filter:
            allowed = self.STATUS_DISPLAY_TO_VALUES.get(
                str(status_filter)
            )
            if allowed is not None:
                if "user_decision" in df.columns:
                    decision_norm = (
                        df["user_decision"].fillna("").astype(str)
                        .str.strip().str.lower()
                    )
                else:
                    decision_norm = pd.Series(
                        [""] * len(df), index=df.index,
                    )
                df = df[decision_norm.isin(allowed)]

        df = df.reset_index(drop=True)
        self._literature_display_records = df

        rows = [
            self._record_to_literature_row(r)
            for r in df.to_dict("records")
        ]
        if running_preview:
            for i in range(len(rows)):
                rows[i][-1] = "运行中"

        try:
            cids = df["canonical_id"].astype(str).tolist()
        except Exception:
            cids = None
        lv.set_literature(rows, cids)

        try:
            lv.set_filter_count(
                len(df), total_for_filter,
            )
        except Exception:
            pass

    # ============================================================
    # 记录 → 行
    # ============================================================
    def _record_to_table_row(self, record):
        return [
            str(record.get("title", "") or ""),
            first_author(record.get("authors", "")),
            format_year(record.get("year")),
            first_source(record.get("source_databases", "")),
            self._map_status_short(record),
        ]

    def _record_to_literature_row(self, record):
        return [
            format_strategies_short(
                record.get("strategy_names", "")
            ),
            str(record.get("title", "") or ""),
            first_author(record.get("authors", "")),
            str(record.get("journal", "") or ""),
            format_year(record.get("year")),
            str(record.get("doi", "") or ""),
            all_sources(record.get("source_databases", "")),
            self._map_status_short(record),
        ]

    def _record_to_zotero_import_row(self, record, collection_map: dict) -> list:
        cid = str(record.get("canonical_id", "") or "")
        _col_val = collection_map.get(cid, "")
        return [
            str(record.get("title", "") or ""),
            first_author(record.get("authors", "")),
            str(record.get("journal", "") or ""),
            format_year(record.get("year")),
            str(record.get("doi", "") or ""),
            "zotero",
            _col_val if _col_val else "（未分类）",
        ]

    def _resolve_special_visual(self, record):
        try:
            special = str(
                record.get("special_status", "") or ""
            ).strip().lower()
        except Exception:
            return None
        if not special:
            return None
        return self._SPECIAL_VISUALS.get(special)

    def _map_status_short(self, record):
        visual = self._resolve_special_visual(record)
        if visual:
            return visual["short"]

        decision = str(record.get("user_decision", "") or "").strip().lower()
        decision_map = {
            "include":   "已纳入",
            "exclude":   "已排除",
            "maybe":     "待定",
        }
        if decision in decision_map:
            return decision_map[decision]

        final = str(
            record.get("final_retrieval_status", "") or ""
        ).strip().lower()
        final_map = {
            "fulltext_pdf":  "全文PDF",
            "fulltext_xml":  "全文XML",
            "fulltext_html": "全文HTML",
            "abstract_only": "摘要",
            "metadata_only": "元数据",
        }
        return final_map.get(final, "—")

    def _map_detail_status(self, record):
        visual = self._resolve_special_visual(record)
        if visual:
            return visual["badge"]

        decision = str(record.get("user_decision", "") or "").strip().lower()
        if decision == "include":
            return "success"
        if decision == "exclude":
            return "error"
        if decision == "maybe":
            return "warning"

        final = str(
            record.get("final_retrieval_status", "") or ""
        ).strip().lower()
        if final in ("fulltext_pdf", "fulltext_xml", "fulltext_html"):
            return "info"
        if final == "abstract_only":
            return "warning"
        if final == "metadata_only":
            return "neutral"
        return "neutral"

    def _map_detail_status_text(self, record, override_decision=""):
        visual = self._resolve_special_visual(record)
        if visual:
            return visual["text"]

        decision = str(
            override_decision
            or record.get("user_decision", "")
            or ""
        ).strip().lower()
        decision_map = {
            "include":   "已纳入",
            "exclude":   "已排除",
            "maybe":     "待定",
        }
        if decision in decision_map:
            return decision_map[decision]

        final = str(
            record.get("final_retrieval_status", "") or ""
        ).strip().lower()
        final_map = {
            "fulltext_pdf":  "全文 PDF",
            "fulltext_xml":  "全文 XML",
            "fulltext_html": "全文 HTML",
            "abstract_only": "仅摘要",
            "metadata_only": "仅元数据",
        }
        return final_map.get(final, "未知")

    def _detail_action_open(self, data):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        url = self._resolve_open_url(data)
        if not url:
            self._mw.notify_user(
                "warning", "该记录缺少可打开的链接（DOI / PMID / URL）"
            )
            return
        QDesktopServices.openUrl(QUrl(url))

    def _detail_action_copy_doi(self, data):
        from PySide6.QtWidgets import QApplication

        doi = str(data.get("doi", "") or "").strip()
        if not doi:
            self._mw.notify_user("warning", "该记录无 DOI")
            return
        try:
            QApplication.clipboard().setText(doi)
            self._mw.notify_user("info", f"已复制 DOI：{doi}")
        except Exception as exc:
            self._mw.notify_user("error", f"复制失败：{exc}")

    def _detail_action_open_pdf(self, data):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from medlitflow import core, screening

        cid = str(data.get("canonical_id", "") or "").strip()

        if cid:
            try:
                config = self._mw.get_config()
                library_root_raw = (config.get("library", {}) or {}).get(
                    "root", ""
                )
                if library_root_raw:
                    library_root = Path(library_root_raw)
                    if screening.open_literature_file(cid, library_root):
                        self._mw.notify_user("info", "已打开文献文件")
                        return
            except Exception:
                pass

        for path_str in (
            str(data.get("current_file_path", "") or "").strip(),
            str(data.get("downloaded_file_path", "") or "").strip(),
        ):
            if not path_str:
                continue
            p = Path(path_str)
            if p.is_file():
                opened = core.open_file_with_default_app(p)
                if opened:
                    self._mw.notify_user("info", f"已打开文件：{p.name}")
                    return

        url = (
            str(data.get("landing_page_url", "") or "").strip()
            or str(data.get("fulltext_url", "") or "").strip()
        )
        if url:
            QDesktopServices.openUrl(QUrl(url))
            self._mw.notify_user(
                "info", "无本地文件，已在浏览器打开原文页面"
            )
            return

        self._mw.notify_user(
            "warning", "该记录尚未下载，且无可打开的原文链接"
        )

    def _detail_action_mark(self, data):
        cid = str(data.get("canonical_id", "") or "").strip()
        self._mw.goto_page(PAGE_SCREENING)
        if not cid:
            self._mw.notify_user(
                "warning",
                "该记录缺少 canonical_id，无法定位到筛选中心",
            )
            return
        self._mw.notify_user(
            "info",
            f"已定位到筛选中心；当前记录 canonical_id={cid}",
        )
        self._mw._locate_screening_cid(cid)

    def _resolve_open_url(self, data):
        doi = str(data.get("doi", "") or "").strip()
        if doi:
            for prefix in (
                "https://doi.org/",
                "http://doi.org/",
                "https://dx.doi.org/",
                "http://dx.doi.org/",
                "doi:",
            ):
                if doi.lower().startswith(prefix):
                    doi = doi[len(prefix):]
                    break
            return f"https://doi.org/{doi}"

        pmid = str(data.get("pmid", "") or "").strip()
        if pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        landing = str(data.get("landing_page_url", "") or "").strip()
        if landing.startswith(("http://", "https://")):
            return landing

        fulltext = str(data.get("fulltext_url", "") or "").strip()
        if fulltext.startswith(("http://", "https://")):
            return fulltext

        return ""

    def _compute_orphan_strategies(self, records):
        try:
            from medlitflow import screening as _scr
            alive = self._get_alive_strategies()
            if alive is None:
                return []
            return _scr.compute_orphan_strategies(
                records=records, alive_strategies=alive,
            )
        except Exception:
            return []

    def _get_alive_strategies(self):
        try:
            from medlitflow import archive, screening as _scr
            config = self._mw.get_config()
            archive._set_config(config)
            try:
                archive_root = archive._resolve_archive_root(config)
            finally:
                archive._set_config(None)
            return _scr.get_alive_strategies(archive_root)
        except Exception:
            return None

    @staticmethod
    def _compute_year_options(records, data_origin_filter=None):
        from datetime import datetime as _dt

        years = set()
        current_year = _dt.now().year
        for i in range(6):
            years.add(current_year - i)

        if records is None or len(records) == 0:
            return sorted(years, reverse=True)

        sub = records
        if data_origin_filter and "data_origin" in records.columns:
            origin = records["data_origin"].fillna(
                "retrieval"
            ).astype(str)
            sub = records[origin == data_origin_filter]

        if "year" in sub.columns:
            try:
                for v in sub["year"]:
                    if v is None:
                        continue
                    try:
                        y = int(float(v))
                        if 1900 <= y <= current_year + 1:
                            years.add(y)
                    except (TypeError, ValueError):
                        continue
            except Exception:
                pass

        return sorted(years, reverse=True)