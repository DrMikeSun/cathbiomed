"""筛选中心草稿管理器（QObject，非 UI）。"""

from pathlib import Path
import pandas as pd

from PySide6.QtCore import QObject, QTimer, Signal

class ScreeningDraftManager(QObject):
    FLUSH_INTERVAL_MS = 5000

    DRAFT_COLUMNS = ["canonical_id", "draft_decision", "draft_updated_at"]

    draft_changed = Signal(str)

    def __init__(self, library_root: Path, parent=None):
        super().__init__(parent)
        self._library_root = Path(library_root)
        self._drafts: dict[str, dict[str, str]] = {}
        self._dirty: set[str] = set()
        self._loaded_strategies: set[str] = set()

        self._timer = QTimer(self)
        self._timer.setInterval(self.FLUSH_INTERVAL_MS)
        self._timer.timeout.connect(self._on_timer_flush)
        self._timer.start()

    def set_decision(self, strategy, cid, decision):
        if not strategy or not cid:
            return
        d = str(decision or "unscreened")
        bucket = self._drafts.setdefault(str(strategy), {})
        if bucket.get(str(cid)) == d:
            return
        bucket[str(cid)] = d
        self._dirty.add(str(strategy))
        self.draft_changed.emit(str(strategy))

    def set_decisions_batch(self, strategy, cids, decision):
        if not strategy or not cids:
            return
        s = str(strategy)
        d = str(decision or "unscreened")
        bucket = self._drafts.setdefault(s, {})
        changed = False
        for cid in cids:
            cid_s = str(cid or "")
            if not cid_s:
                continue
            if bucket.get(cid_s) == d:
                continue
            bucket[cid_s] = d
            changed = True
        if changed:
            self._dirty.add(s)
            self.draft_changed.emit(s)

    def has_draft(self, strategy):
        s = str(strategy)
        if s in self._dirty:
            return True
        if self._drafts.get(s):
            return True
        try:
            return self._draft_path(s).exists()
        except OSError:
            return False

    def flush(self, strategy=None):
        if strategy is not None:
            targets = [str(strategy)] if str(strategy) in self._dirty else []
        else:
            targets = list(self._dirty)
        for s in targets:
            if self._write_draft(s):
                self._dirty.discard(s)

    def load_from_disk(self, strategy):
        s = str(strategy)
        if s in self._loaded_strategies:
            return

        path = self._draft_path(s)
        if not path.exists():
            self._loaded_strategies.add(s)
            return
        try:
            df = pd.read_parquet(path)
        except Exception:
            return
        if df is None or len(df) == 0:
            self._loaded_strategies.add(s)
            return
        if "canonical_id" not in df.columns or "draft_decision" not in df.columns:
            return
        bucket: dict[str, str] = {}
        for _, row in df.iterrows():
            cid = str(row.get("canonical_id", "") or "")
            if not cid:
                continue
            bucket[cid] = str(row.get("draft_decision", "") or "unscreened")
        existing = self._drafts.get(s, {})
        existing.update(bucket)
        self._drafts[s] = existing
        self._loaded_strategies.add(s)
        self.draft_changed.emit(s)

    def clear(self, strategy=None):
        if strategy is not None:
            self._drafts.pop(str(strategy), None)
            self._dirty.discard(str(strategy))
            self._loaded_strategies.discard(str(strategy))
            try:
                self._draft_path(str(strategy)).unlink(missing_ok=True)
            except OSError:
                pass
            self.draft_changed.emit(str(strategy))
        else:
            for s in list(self._drafts.keys()):
                self._drafts.pop(s, None)
                try:
                    self._draft_path(s).unlink(missing_ok=True)
                except OSError:
                    pass
            self._dirty.clear()

    def has_dirty_draft(self, strategy) -> bool:
        return str(strategy) in self._dirty

    def stop(self):
        self._timer.stop()

    def _on_timer_flush(self):
        for s in list(self._dirty):
            if self._write_draft(s):
                self._dirty.discard(s)

    def _write_draft(self, strategy) -> bool:
        bucket = self._drafts.get(str(strategy)) or {}
        if not bucket:
            return True
        path = self._draft_path(str(strategy))
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            _now = _dt.now().astimezone().isoformat()
            rows = [
                {"canonical_id": cid, "draft_decision": decision,
                "draft_updated_at": _now}
                for cid, decision in bucket.items()
            ]
            df = pd.DataFrame(rows, columns=self.DRAFT_COLUMNS)
            df.to_parquet(tmp, index=False)
            tmp.replace(path)
            return True
        except Exception:
            return False
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _draft_path(self, strategy):
        return self._library_root / "screening_drafts" / f"{strategy}.parquet"