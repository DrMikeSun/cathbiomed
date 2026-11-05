"""UI bridge Worker 集合。

所有 Worker 均 QThread 后台执行，run() 入口 reset_shutdown，finally 清理 interactor。"""

from __future__ import annotations

import copy
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from PySide6.QtCore import QThread, Signal

from medlitflow import core, enrichment, pipeline, robustness
from ui.bridge.interactor import UIBridgeInteractor

class PipelineWorker(QThread):
    """后台执行 pipeline.run_pipeline。"""
    finished_ok = Signal(str)       # (run_root)
    failed = Signal(str)            # (error_message)
    interrupted = Signal()          # 中断已响应

    def __init__(
        self,
        config_path: str | None,
        stage: str | None = None,
        resume: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._original_config_path: str | None = config_path
        self._effective_config_path: str | None = None
        self.stage = stage
        self.resume = resume
        self._interactor: UIBridgeInteractor | None = None
        self._tmpdir: str | None = None
        self._health_executor: ThreadPoolExecutor | None = None

    def run(self) -> None:
        robustness.reset_shutdown()
        if self._interactor is None:
            self._interactor = UIBridgeInteractor()
        interactor = self._interactor

        base_config = self._load_config_safe()

        before = self._snapshot_runs(base_config)

        try:
            self._effective_config_path = self._force_non_interactive(base_config)
            pipeline.set_interactor(interactor)
            self._inject_health_check_future(self._effective_config_path)

            interactor.emit_started()

            pipeline.run_pipeline(
                self._effective_config_path,
                self.stage,
                resume=self.resume,
            )

            run_root = self._resolve_new_run_root(before, base_config)
            result = str(run_root) if run_root else "OK"
            interactor.emit_finished(result)
            self.finished_ok.emit(result)

        except robustness.InterruptRequested:
            interactor.emit_interrupted()
            self.interrupted.emit()

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            interactor.emit_failed(err)
            self.failed.emit(err)

        finally:
            try:
                if pipeline.get_interactor() is interactor:
                    pipeline.set_interactor(None)
            except Exception:
                pass
            self._cleanup_tmpdir()
            self._shutdown_health_executor()

    def get_interactor(self) -> UIBridgeInteractor:
        if self._interactor is None:
            self._interactor = UIBridgeInteractor()
        return self._interactor

    def request_interrupt(self) -> None:
        robustness.request_shutdown(detail="UI 取消")

    def abandon(self, run_name: str) -> None:
        pipeline.abandon_run(self._original_config_path, run_name)

    def _load_config_safe(self):
        try:
            return core.load_config(self._original_config_path)
        except Exception:
            return None

    def _force_non_interactive(self, base_config=None) -> str:
        if base_config is None:
            config = core.load_config(self._original_config_path)
        else:
            config = copy.deepcopy(base_config)
        config.setdefault("project", {})["non_interactive"] = True

        self._tmpdir = tempfile.mkdtemp(prefix="medlitflow_ui_")
        tmp_path = Path(self._tmpdir) / "config_ui.yaml"
        tmp_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return str(tmp_path)

    def _inject_health_check_future(self, config_path: str) -> None:
        try:
            config = core.load_config(config_path)
        except Exception:
            return
        self._health_executor = ThreadPoolExecutor(max_workers=1)
        future = self._health_executor.submit(
            robustness.startup_health_check,
            config,
        )
        pipeline._set_health_check_future(future)

    def _snapshot_runs(self, config=None) -> set[str]:
        if config is None:
            config = self._load_config_safe()
        if config is None:
            return set()
        try:
            runs_dir = Path(config["project"]["output_root"]) / "runs"
            if not runs_dir.exists():
                return set()
            return {d.name for d in runs_dir.iterdir() if d.is_dir()}
        except Exception:
            return set()

    def _resolve_new_run_root(self, before: set[str], config=None) -> Path | None:
        if config is None:
            config = self._load_config_safe()
        if config is None:
            return None
        try:
            runs_dir = Path(config["project"]["output_root"]) / "runs"
            if not runs_dir.exists():
                return None
            after = {d.name for d in runs_dir.iterdir() if d.is_dir()}
            new_names = after - before
            if not new_names:
                return None

            new_dirs = [runs_dir / name for name in new_names]
            if len(new_dirs) == 1:
                return new_dirs[0]

            with_metadata = [
                d for d in new_dirs
                if (d / "output" / "run_metadata.json").exists()
            ]
            if with_metadata:
                return max(
                    with_metadata,
                    key=lambda p: (p.stat().st_mtime, p.name),
                )
            return max(new_dirs, key=lambda p: (p.stat().st_mtime, p.name))
        except Exception:
            return None

    def _cleanup_tmpdir(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def _shutdown_health_executor(self) -> None:
        if self._health_executor is not None:
            self._health_executor.shutdown(wait=False)
            self._health_executor = None

class EnrichmentRefreshWorker(QThread):
    UNLIMITED_QUOTA = 10 ** 9

    finished_ok = Signal(int)       # (processed_count)
    failed = Signal(str)            # (error_message)
    interrupted = Signal()          # 中断已响应

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self._config = copy.deepcopy(config) if config else {}
        self._interactor: UIBridgeInteractor | None = None

    def request_interrupt(self) -> None:
        robustness.request_shutdown(detail="更新指标取消")

    def get_interactor(self) -> UIBridgeInteractor:
        if self._interactor is None:
            self._interactor = UIBridgeInteractor()
        return self._interactor

    def run(self) -> None:
        robustness.reset_shutdown()
        interactor = self.get_interactor()

        try:
            config = self._config
            config.setdefault("project", {})["non_interactive"] = True
            enrich_cfg = config.setdefault("enrichment", {})
            citation_cfg = enrich_cfg.setdefault("citation", {})
            citation_cfg["max_records_per_run"] = self.UNLIMITED_QUOTA

            library_root = Path(config["library"]["root"])

            pipeline.set_interactor(interactor)
            interactor.emit_started()

            before_count = self._estimate_processed_count(library_root)
            enrichment.trigger_citation_enrichment_for_confirmed_strategies(
                library_root, config,
            )
            after_count = self._estimate_processed_count(library_root)
            processed = max(0, after_count - before_count)

            interactor.emit_finished("OK")
            self.finished_ok.emit(processed)

        except robustness.InterruptRequested:
            interactor.emit_interrupted()
            self.interrupted.emit()

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            interactor.emit_failed(err)
            self.failed.emit(err)

        finally:
            try:
                if pipeline.get_interactor() is interactor:
                    pipeline.set_interactor(None)
            except Exception:
                pass

    @staticmethod
    def _estimate_processed_count(library_root: Path) -> int:
        try:
            import pandas as pd
            p = Path(library_root) / "citation_processed.parquet"
            if not p.exists():
                return 0
            df = pd.read_parquet(p)
            return int(len(df))
        except Exception:
            return 0

class EnrichmentMetricsWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)
    interrupted = Signal()

    def __init__(self, config, cids, strategy_name="", parent=None):
        super().__init__(parent)
        self._config = copy.deepcopy(config) if config else {}
        self._cids = list(cids) if cids else []
        self._strategy_name = str(strategy_name or "")
        self._interactor: UIBridgeInteractor | None = None

    def get_interactor(self) -> UIBridgeInteractor:
        if self._interactor is None:
            self._interactor = UIBridgeInteractor()
        return self._interactor

    def request_interrupt(self) -> None:
        robustness.request_shutdown(detail="更新指标取消")

    def run(self) -> None:
        robustness.reset_shutdown()
        interactor = self.get_interactor()

        try:
            config = self._config
            config.setdefault("project", {})["non_interactive"] = True

            library_root = Path(config["library"]["root"])

            pipeline.set_interactor(interactor)
            interactor.emit_started()

            result = enrichment.refresh_metrics_for_cids(
                self._cids,
                library_root,
                config,
                only_missing=True,
            )

            interactor.emit_finished("OK")
            self.finished_ok.emit(result)

        except robustness.InterruptRequested:
            interactor.emit_interrupted()
            self.interrupted.emit()

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            interactor.emit_failed(err)
            self.failed.emit(err)

        finally:
            try:
                if pipeline.get_interactor() is interactor:
                    pipeline.set_interactor(None)
            except Exception:
                pass

class StrategyDeleteWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)
    interrupted = Signal()

    def __init__(
        self,
        action: str,
        payload: dict,
        config: dict,
        library_root: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._action = str(action or "")
        self._payload = dict(payload or {})
        self._config = copy.deepcopy(config) if config else {}
        self._library_root = Path(library_root)

    def request_interrupt(self) -> None:
        robustness.request_shutdown(
            detail=f"策略删除取消（{self._action}）"
        )

    def run(self) -> None:
        robustness.reset_shutdown()
        try:
            result = self._do_action()
            self.finished_ok.emit(result)
        except robustness.InterruptRequested:
            self.interrupted.emit()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    def _do_action(self) -> dict:
        if self._action == "purge":
            return self._do_purge()
        if self._action == "cleanup_orphans":
            return self._do_cleanup_orphans()
        raise ValueError(f"未知 action：{self._action!r}")

    def _do_purge(self) -> dict:
        from medlitflow import screening
        strategy_name = str(
            self._payload.get("strategy_name", "") or ""
        )
        prescan_result = self._payload.get("prescan_result")
        result = screening.delete_strategy_cascade(
            strategy_name, self._library_root, self._config,
            delete_archive=True,
            prescan_result=prescan_result,
        )
        return {
            "action": "purge",
            "strategy_name": strategy_name,
            "result": result,
        }

    def _do_cleanup_orphans(self) -> dict:
        from medlitflow import screening
        strategies = self._payload.get("strategies", []) or []
        ok_count = 0
        failed: list[dict] = []
        for o in strategies:
            robustness.check_shutdown()
            name = str(o.get("name", "") or "")
            if not name:
                continue
            try:
                result = screening.delete_strategy_cascade(
                    name, self._library_root, self._config,
                    delete_archive=True,
                )
                if result.get("ok"):
                    ok_count += 1
                else:
                    failed.append({
                        "name": name,
                        "errors": list(result.get("errors", []) or []),
                    })
            except robustness.InterruptRequested:
                raise
            except Exception as exc:
                failed.append({
                    "name": name,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        return {
            "action": "cleanup_orphans",
            "ok_count": ok_count,
            "total": int(len(strategies)),
            "failed": failed,
        }

class StartupCleanupWorker(QThread):
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, library_root: Path, parent=None) -> None:
        super().__init__(parent)
        self._library_root = Path(library_root)
        self._cancelled = False

    def request_interrupt(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        robustness.reset_shutdown()
        merged_result: dict = {
            "cleaned": 0,
            "cids": [],
            "backfilled": 0,
        }

        try:
            if not self._cancelled:
                from medlitflow import zotero_import as _zimp
                r1 = _zimp.cleanup_orphan_zotero_imports(
                    self._library_root,
                )
                if isinstance(r1, dict):
                    merged_result["cleaned"] = int(
                        r1.get("cleaned", 0) or 0
                    )
                    merged_result["cids"] = list(
                        r1.get("cids", []) or []
                    )
        except Exception:
            pass

        try:
            if not self._cancelled:
                from medlitflow import screening as _screening
                r2 = _screening.backfill_file_index_decisions(
                    self._library_root,
                )
                if isinstance(r2, dict):
                    merged_result["backfilled"] = int(
                        r2.get("backfilled", 0) or 0
                    )
        except Exception:
            pass

        self.finished_ok.emit(merged_result)