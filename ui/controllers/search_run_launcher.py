"""ui/controllers/search_run_launcher.py — 检索启动辅助（A4 第二组）。"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import yaml

class SearchRunLauncher:
    @staticmethod
    def parse_search_queries(raw) -> list[str]:
        if not raw:
            return []
        text = str(raw)
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_quote = False
        quote_char = ""
        for ch in text:
            if in_quote:
                buf.append(ch)
                if ch == quote_char:
                    in_quote = False
                continue
            if ch in ("'", '"'):
                in_quote = True
                quote_char = ch
                buf.append(ch)
                continue
            if ch == "(":
                depth += 1
                buf.append(ch)
                continue
            if ch == ")":
                if depth > 0:
                    depth -= 1
                buf.append(ch)
                continue
            if ch in (";", "；", "\n") and depth == 0:
                segment = "".join(buf).strip()
                if segment:
                    parts.append(segment)
                buf = []
                continue
            buf.append(ch)
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts

    @staticmethod
    def build_queue(queries_raw, pipeline_mode: str) -> list[dict]:
        from medlitflow import pipeline as _pipeline
        queries = SearchRunLauncher.parse_search_queries(queries_raw)
        return [
            {
                "name": _pipeline._derive_strategy_name(q),
                "pubmed": q,
                "pipeline_mode": pipeline_mode,
            }
            for q in queries
        ]

    @staticmethod
    def write_ui_run_config(config: dict, tmp_files: list) -> str:
        tmpdir = Path(tempfile.gettempdir()) / "medlitflow_ui"
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmpdir / f"ui_run_config_{uuid.uuid4().hex[:8]}.yaml"
        tmp_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        tmp_files.append(str(tmp_path))
        return str(tmp_path)

    @staticmethod
    def cleanup_tmp_files(tmp_files: list) -> None:
        for f in list(tmp_files):
            try:
                Path(f).unlink(missing_ok=True)
            except OSError:
                pass
        tmp_files.clear()

    @staticmethod
    def prepare_search_worker(
        config_path: str,
        resume: bool,
        main_window,
        on_finished,
        on_failed,
        on_interrupted,
    ):
        from ui.bridge.worker import PipelineWorker

        worker = PipelineWorker(config_path, stage=None, resume=resume)
        interactor = worker.get_interactor()
        main_window.search_view.bind_interactor(interactor)
        interactor.progressed.connect(main_window._on_running_progressed)
        interactor.notified.connect(main_window._on_interactor_notified)
        interactor.progressed.connect(main_window._on_interactor_progressed)
        interactor.prompt_requested.connect(
            lambda text, choices, ic=interactor:
                main_window._on_prompt_requested(ic, text, choices)
        )
        worker.finished_ok.connect(on_finished)
        worker.failed.connect(on_failed)
        worker.interrupted.connect(on_interrupted)
        main_window._logs_progress_stage = ""
        main_window._logs_progress_milestone = 0
        return worker