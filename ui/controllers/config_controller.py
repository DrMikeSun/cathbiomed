"""ui/controllers/config_controller.py — 配置控制器（A1）。

职责：配置加载 / 保存 / 用户 yaml 读写 / JCR 检查。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from PySide6.QtCore import QObject

from medlitflow import core

if TYPE_CHECKING:
    from ui.views.main_window import MedLitFlowMainWindow

class ConfigController(QObject):
    def __init__(self, main_window: "MedLitFlowMainWindow"):
        super().__init__(main_window)
        self._mw = main_window
        _p = self.user_yaml_path()
        self._app_config_path = str(_p) if _p.exists() else None
        self._config_cache: dict | None = None
        self._config_cache_mtime = None

    _UI_OWNED_KEYS = {
        "api": (
            "ncbi_api_key", "s2_api_key", "openalex_api_key", "email",
        ),
        "retrieval": (
            "sources", "max_results_per_query", "max_workers",
            "timeout_seconds", "max_attempts_per_record",
            "max_retries_per_candidate",
        ),
        "dedup": ("abstract_similarity_threshold",),
        "pipeline": ("max_hits_threshold",),
        "output": (
            "user_dir", "export_mode", "auto_export", "subdir_template",
        ),
        "project": (
            "output_root", "reviewer_id",
            "strategy_name", "run_name", "non_interactive",
        ),
        "zotero": (
            "library_type", "library_id", "link_mode", "stale_warning_days",
        ),
    }

    @property
    def app_config_path(self):
        return self._app_config_path

    def get_config(self) -> dict:
        return self._load_config()

    def get_library_root(self) -> Path:
        return Path(self._load_config()["library"]["root"])

    def user_yaml_path(self) -> Path:
        return core.user_data_root() / "config" / "user.yaml"

    @classmethod
    def _strip_ui_owned(cls, existing: dict) -> None:
        for section, keys in cls._UI_OWNED_KEYS.items():
            sec = existing.get(section)
            if not isinstance(sec, dict):
                continue
            for k in keys:
                sec.pop(k, None)
            if not sec:
                existing.pop(section, None)

    def _load_config(self, force: bool = False) -> dict:
        current_mtime = None
        if self._app_config_path:
            try:
                current_mtime = os.path.getmtime(self._app_config_path)
            except OSError:
                current_mtime = None

        if (not force
                and self._config_cache is not None
                and self._config_cache_mtime == current_mtime):
            return copy.deepcopy(self._config_cache)

        config = core.load_config(self._app_config_path)
        self._config_cache = config
        self._config_cache_mtime = current_mtime
        return copy.deepcopy(config)

    def on_settings_save(self, values) -> None:
        if not isinstance(values, dict):
            return

        user_config: dict = {}

        api_cfg: dict = {}
        ncbi_key = str(values.get("ncbi_api_key", "") or "").strip()
        if ncbi_key:
            api_cfg["ncbi_api_key"] = ncbi_key
        s2_key = str(values.get("s2_api_key", "") or "").strip()
        if s2_key:
            api_cfg["s2_api_key"] = s2_key
        oa_key = str(values.get("openalex_api_key", "") or "").strip()
        if oa_key:
            api_cfg["openalex_api_key"] = oa_key
        api_email = str(values.get("api_email", "") or "").strip()
        if api_email:
            api_cfg["email"] = api_email
        if api_cfg:
            user_config["api"] = api_cfg

        ret_cfg: dict = {}
        sources_cfg: dict = {}
        for ui_key, src in (
            ("pubmed_enabled", "pubmed"),
            ("pmc_enabled", "pmc"),
            ("europepmc_enabled", "europepmc"),
            ("crossref_enabled", "crossref"),
            ("openalex_enabled", "openalex"),
        ):
            if ui_key in values:
                sources_cfg[src] = {"enabled": bool(values[ui_key])}
        if sources_cfg:
            ret_cfg["sources"] = sources_cfg
        for ui_key, cfg_key in (
            ("default_limit", "max_results_per_query"),
            ("pdf_concurrency", "max_workers"),
            ("pdf_timeout", "timeout_seconds"),
            ("pdf_retries", "max_attempts_per_record"),
            ("retrieval_max_retries", "max_retries_per_candidate"),
        ):
            if ui_key in values:
                try:
                    ret_cfg[cfg_key] = int(values[ui_key])
                except (TypeError, ValueError):
                    pass
        if ret_cfg:
            user_config["retrieval"] = ret_cfg

        if "dedup_threshold" in values:
            try:
                user_config["dedup"] = {
                    "abstract_similarity_threshold": float(
                        values["dedup_threshold"]
                    )
                }
            except (TypeError, ValueError):
                pass

        if "max_hits_threshold" in values:
            try:
                user_config["pipeline"] = {
                    "max_hits_threshold": int(
                        values["max_hits_threshold"]
                    )
                }
            except (TypeError, ValueError):
                pass

        out_cfg: dict = {}
        user_dir = str(
            values.get("export_user_dir", "") or ""
        ).strip()
        if user_dir:
            out_cfg["user_dir"] = user_dir
        mode = str(values.get("export_mode", "") or "").lower()
        if mode in ("copy", "symlink", "none"):
            out_cfg["export_mode"] = mode
        if bool(values.get("export_auto", False)):
            out_cfg["auto_export"] = True
        tpl = str(
            values.get("export_subdir_template", "") or ""
        ).strip()
        if tpl and tpl != "{strategy}_{date}":
            out_cfg["subdir_template"] = tpl
        if out_cfg:
            user_config["output"] = out_cfg

        data_dir = str(values.get("data_dir", "") or "").strip()
        if data_dir:
            try:
                _data_norm = str(Path(data_dir).resolve())
            except OSError:
                _data_norm = data_dir
            _default_norm = str(
                (core.user_data_root() / "data").resolve()
            )
            if _data_norm == _default_norm:
                data_dir = ""
        reviewer_id = str(values.get("reviewer_id", "") or "").strip()
        project_cfg: dict = {}
        if data_dir:
            project_cfg["output_root"] = data_dir
        if reviewer_id:
            project_cfg["reviewer_id"] = reviewer_id
        if project_cfg:
            user_config["project"] = project_cfg

        zotero_cfg: dict = {}
        zotero_lib_type = str(
            values.get("zotero_library_type", "") or ""
        ).strip()
        if zotero_lib_type in ("user", "group"):
            zotero_cfg["library_type"] = zotero_lib_type
        zotero_lib_id = str(
            values.get("zotero_library_id", "") or ""
        ).strip()
        if zotero_lib_id:
            zotero_cfg["library_id"] = zotero_lib_id
        zotero_link_mode = str(
            values.get("zotero_link_mode", "") or ""
        ).strip()
        if zotero_link_mode in ("imported_file", "linked_file"):
            zotero_cfg["link_mode"] = zotero_link_mode
        try:
            stale_days = int(
                values.get("zotero_stale_warning_days", 0)
            )
        except (TypeError, ValueError):
            stale_days = 0
        if stale_days > 0:
            zotero_cfg["stale_warning_days"] = stale_days
        if zotero_cfg:
            user_config["zotero"] = zotero_cfg

        path = self.user_yaml_path()

        existing: dict = {}
        if path.exists():
            try:
                _raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                _raw = None
            if isinstance(_raw, dict):
                existing = _raw

        self._strip_ui_owned(existing)

        merged = (
            core._deep_merge(existing, user_config)
            if existing else user_config
        )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                yaml.safe_dump(
                    merged, allow_unicode=True, sort_keys=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except Exception as exc:
            self._mw.notify_user("error", f"配置保存失败：{exc}")
            return

        core.secure_file_permissions(path)

        self._app_config_path = str(path)

        parts = []
        if api_cfg:
            parts.append(f"API 密钥 {len(api_cfg)} 项")
        if out_cfg.get("auto_export") and out_cfg.get("user_dir"):
            parts.append(f"导出 {out_cfg.get('export_mode', 'symlink')}")
        summary = "、".join(parts) if parts else "已保存"
        self._mw.notify_user(
            "info", f"配置已保存到 config/user.yaml（{summary}）"
        )

        self.check_jcr_warning()

    def on_settings_reset(self) -> None:
        path = self.user_yaml_path()
        if path.exists():
            try:
                existing = yaml.safe_load(
                    path.read_text(encoding="utf-8")
                ) or {}
            except Exception:
                existing = {}
            if not isinstance(existing, dict):
                existing = {}

            self._strip_ui_owned(existing)

            if existing:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(path.name + ".tmp")
                    tmp.write_text(
                        yaml.safe_dump(
                            existing,
                            allow_unicode=True,
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    os.replace(tmp, path)
                    core.secure_file_permissions(path)
                except OSError:
                    pass
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
                self._app_config_path = None

        self._config_cache = None
        self._config_cache_mtime = None

        self._mw.notify_user(
            "info", "已恢复出厂默认（UI 配置项已清除）"
        )

    def on_settings_browse(self, field_key) -> None:
        from PySide6.QtWidgets import QFileDialog

        current = ""
        try:
            all_values = self._mw.settings_view.values()
            current = str(all_values.get(field_key, "") or "")
        except Exception:
            current = ""

        path = QFileDialog.getExistingDirectory(
            self._mw,
            "选择目录",
            current or "",
            QFileDialog.ShowDirsOnly,
        )
        if not path:
            return

        try:
            self._mw.settings_view.set_field_value(field_key, path)
        except Exception as exc:
            self._mw.notify_user("error", f"回填失败：{exc}")

    def load_config_into_settings_view(self) -> None:
        try:
            config = self.get_config()
        except Exception:
            return

        ui_values: dict = {}

        api_cfg = config.get("api", {}) or {}
        ui_values["ncbi_api_key"] = str(
            api_cfg.get("ncbi_api_key", "") or ""
        )
        ui_values["s2_api_key"] = str(
            api_cfg.get("s2_api_key", "") or ""
        )
        ui_values["openalex_api_key"] = str(
            api_cfg.get("openalex_api_key", "") or ""
        )
        ui_values["api_email"] = str(api_cfg.get("email", "") or "")

        ret_cfg = config.get("retrieval", {}) or {}
        sources_cfg = ret_cfg.get("sources", {}) or {}
        for ui_key, src in (
            ("pubmed_enabled", "pubmed"),
            ("pmc_enabled", "pmc"),
            ("europepmc_enabled", "europepmc"),
            ("crossref_enabled", "crossref"),
            ("openalex_enabled", "openalex"),
        ):
            ui_values[ui_key] = bool(
                (sources_cfg.get(src, {}) or {}).get("enabled", True)
            )
        ui_values["default_limit"] = int(
            ret_cfg.get("max_results_per_query", 1000)
        )
        ui_values["pdf_concurrency"] = int(
            ret_cfg.get("max_workers", 4)
        )
        ui_values["pdf_timeout"] = int(
            ret_cfg.get("timeout_seconds", 30)
        )
        ui_values["pdf_retries"] = int(
            ret_cfg.get("max_attempts_per_record", 3)
        )
        ui_values["retrieval_max_retries"] = int(
            ret_cfg.get("max_retries_per_candidate", 1)
        )

        dedup_cfg = config.get("dedup", {}) or {}
        ui_values["dedup_threshold"] = float(
            dedup_cfg.get("abstract_similarity_threshold", 0.05)
        )

        pipeline_cfg = config.get("pipeline", {}) or {}
        ui_values["max_hits_threshold"] = int(
            pipeline_cfg.get("max_hits_threshold", 5000)
        )

        out_cfg = config.get("output", {}) or {}
        ui_values["export_user_dir"] = str(
            out_cfg.get("user_dir", "") or ""
        )
        ui_values["export_mode"] = str(
            out_cfg.get("export_mode", "symlink") or "symlink"
        )
        ui_values["export_auto"] = bool(
            out_cfg.get("auto_export", False)
        )
        ui_values["export_subdir_template"] = str(
            out_cfg.get("subdir_template", "{strategy}_{date}")
            or "{strategy}_{date}"
        )

        project_cfg = config.get("project", {}) or {}
        _out_raw = str(
            project_cfg.get("output_root", "") or ""
        ).strip()
        _default_out = str(
            (core.user_data_root() / "data").resolve()
        )
        ui_values["data_dir"] = (
            "" if _out_raw == _default_out else _out_raw
        )
        ui_values["reviewer_id"] = str(
            project_cfg.get("reviewer_id", "") or ""
        )

        zotero_cfg = config.get("zotero", {}) or {}
        ui_values["zotero_library_type"] = str(
            zotero_cfg.get("library_type", "user") or "user"
        )
        ui_values["zotero_library_id"] = str(
            zotero_cfg.get("library_id", "0") or "0"
        )
        ui_values["zotero_link_mode"] = str(
            zotero_cfg.get("link_mode", "imported_file")
            or "imported_file"
        )
        try:
            ui_values["zotero_stale_warning_days"] = int(
                zotero_cfg.get("stale_warning_days", 90)
            )
        except (TypeError, ValueError):
            ui_values["zotero_stale_warning_days"] = 90

        try:
            self._mw.settings_view.set_values(ui_values)
        except Exception:
            pass

    def check_jcr_warning(self) -> None:
        try:
            config = self.get_config()
        except Exception:
            return

        try:
            jcr_cfg = (config.get("enrichment", {}) or {}).get(
                "journal_metrics", {}
            ) or {}
            active_year = int(jcr_cfg.get("active_year", 2025))
        except Exception:
            return

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        current_year = now.year
        month = now.month

        if month >= 9:
            expected = current_year - 1
        else:
            expected = current_year - 2

        if active_year >= expected:
            return

        try:
            self._mw.logs_view.append_log(
                "warning",
                f"JCR active_year={active_year} 已过时"
                f"（期望 {expected}）；请在 "
                f"config/default.yaml 的 "
                f"enrichment.journal_metrics.active_year 中更新",
            )
        except Exception:
            pass