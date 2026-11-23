"""Human-approved plugin promotion and rollback (M1's only live path).

Promotion is deliberately boring: copy the old file aside, atomically write
the verified candidate, ask the kernel to reload, and undo everything if the
kernel says the plugin did not load. Core-code candidates are review-only in
M1, so a core promote is refused with an explicit reason instead of being
silently ignored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import lineage
from .models import (
    STATUS_PROMOTED,
    STATUS_ROLLED_BACK,
    VERDICT_VERIFIED,
    PromotionRefused,
    WorkbenchError,
    atomic_write_bytes,
    atomic_write_json,
    new_promotion_id,
    now_iso,
    sha256_text,
)
from .store import EvolveStore
from .workbench import (
    normalize_plugin_name,
    plugin_candidate_path,
    read_source_text,
    write_source_text,
)
from .. import plugin_policy

ReloadCallable = Callable[[], dict]


class PluginPromoter:
    def __init__(
        self,
        store: EvolveStore,
        plugin_dir: Path | None,
        reload_plugins: ReloadCallable | None = None,
    ) -> None:
        self.store = store
        self.plugin_dir = Path(plugin_dir).resolve() if plugin_dir else None
        self.reload_plugins = reload_plugins

    # ── promotion ────────────────────────────────────────────────────
    def promote(self, candidate_id: str, *, approved_by: str) -> dict[str, Any]:
        record = self.store.load_candidate(candidate_id)
        kind = str(record.get("kind", ""))
        if kind == "core":
            raise PromotionRefused(
                "core-code 候选在 M1 只能审查，不会写入正在运行的系统（M1 review-only）"
            )
        if kind != "plugin":
            raise PromotionRefused(f"未知候选类型: {kind!r}")
        if not approved_by or not str(approved_by).strip():
            raise PromotionRefused("晋升插件需要明确的批准人（approved_by）")
        if self.plugin_dir is None:
            raise PromotionRefused("未配置插件目录，无法晋升插件")
        if record.get("status") == "discarded":
            raise PromotionRefused("候选已被丢弃，不能晋升")
        if record.get("status") == STATUS_PROMOTED:
            raise PromotionRefused("候选已经晋升过；如需替换请先回滚")
        if (
            not record.get("evaluated")
            or record.get("eval_status") != VERDICT_VERIFIED
            or not record.get("eval")
        ):
            raise PromotionRefused(
                f"候选当前评估状态为 {record.get('eval_status') or '未评估'}，只有 verified 才能晋升"
            )

        name = normalize_plugin_name(record.get("plugin_name") or "")
        candidate_file = plugin_candidate_path(
            self.store.candidate_dir(candidate_id), name
        )
        if not candidate_file.is_file():
            raise PromotionRefused("候选插件文件不存在，可能已被清理")
        candidate_text, _candidate_newline = read_source_text(candidate_file)
        # Candidate storage is LF; restore the line ending the live file had
        # when the candidate was proposed.
        newline = record.get("plugin_newline") or "\n"
        if record.get("plugin_sha256") and sha256_text(candidate_text) != record.get("plugin_sha256"):
            raise PromotionRefused("候选源码在评估后发生了变化，请重新评估")
        violations = plugin_policy.check_source(candidate_text)
        if violations:
            raise PromotionRefused(
                "候选源码不再满足插件策略: " + plugin_policy.format_violations(violations)
            )

        promotion_id = new_promotion_id()
        backup_dir = self.store.backup_dir(promotion_id)
        backup_dir.mkdir(parents=True, exist_ok=True)
        live_path = self.plugin_dir / name
        live_existed = live_path.is_file()
        backup_path = backup_dir / name
        if live_existed:
            # Exact bytes: rollback must return the user's file untouched,
            # including its line endings.
            atomic_write_bytes(backup_path, live_path.read_bytes())
        meta = {
            "promotion_id": promotion_id,
            "candidate_id": candidate_id,
            "plugin_name": name,
            "approved_by": str(approved_by),
            "created_at": now_iso(),
            "live_existed": live_existed,
            "backup": str(backup_path) if live_existed else "",
        }
        atomic_write_json(backup_dir / "promotion.json", meta)

        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        try:
            write_source_text(live_path, candidate_text, newline)
        except OSError as exc:
            self._restore(live_path, backup_path, live_existed)
            raise PromotionRefused(f"写入插件文件失败: {exc}") from exc

        ok, detail = self._reload(name)
        if not ok:
            self._restore(live_path, backup_path, live_existed)
            self._reload(name)  # best effort: put the old plugin back in memory
            raise PromotionRefused(f"插件重新加载失败，已恢复旧文件: {detail}")

        promotion = {
            **meta,
            "status": STATUS_PROMOTED,
            "rolled_back_at": "",
        }
        promotions = record.get("promotions")
        if not isinstance(promotions, list):
            promotions = []
        promotions.append(promotion)
        record["promotions"] = promotions
        record["promotion_id"] = promotion_id
        record["status"] = STATUS_PROMOTED
        record["updated_at"] = now_iso()
        self.store.save_candidate(record)
        lineage.append_event(
            self.store,
            "promoted",
            candidate=record,
            status=STATUS_PROMOTED,
            actor=str(approved_by),
            promotion_id=promotion_id,
            plugin_name=name,
        )
        return record

    # ── rollback ─────────────────────────────────────────────────────
    def rollback(self, promotion_id: str) -> dict[str, Any]:
        record = self.store.find_promotion(promotion_id)
        if self.plugin_dir is None:
            raise PromotionRefused("未配置插件目录，无法回滚插件")

        promotion = self._promotion_item(record, promotion_id)
        name = normalize_plugin_name(
            promotion.get("plugin_name") or record.get("plugin_name") or ""
        )
        live_path = self.plugin_dir / name
        backup_path = self.store.backup_dir(promotion_id) / name
        live_existed = bool(promotion.get("live_existed"))
        if live_existed and not backup_path.is_file():
            raise WorkbenchError(f"找不到晋升备份: {backup_path}")

        self._restore(live_path, backup_path, live_existed)
        # Rolling back a new plugin means it must disappear from the kernel;
        # rolling back a rewrite means the old file must be live again.
        ok, detail = self._reload(name, expect_loaded=live_existed)
        if not ok:
            # The file is already restored; report the half-state instead of
            # pretending the rollback worked in the running kernel.
            raise PromotionRefused(f"插件文件已恢复，但重新加载失败: {detail}")

        promotion["status"] = STATUS_ROLLED_BACK
        promotion["rolled_back_at"] = now_iso()
        record["status"] = STATUS_ROLLED_BACK
        record["updated_at"] = now_iso()
        self.store.save_candidate(record)
        lineage.append_event(
            self.store,
            "rolled_back",
            candidate=record,
            status=STATUS_ROLLED_BACK,
            actor="user",
            promotion_id=promotion_id,
            plugin_name=name,
        )
        return record

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _promotion_item(record: dict[str, Any], promotion_id: str) -> dict[str, Any]:
        for item in record.get("promotions") or []:
            if isinstance(item, dict) and item.get("promotion_id") == promotion_id:
                return item
        if record.get("promotion_id") == promotion_id:
            return {
                "promotion_id": promotion_id,
                "plugin_name": record.get("plugin_name", ""),
                "live_existed": True,
                "approved_by": "",
            }
        raise PromotionRefused("找不到该晋升记录的详细信息")

    @staticmethod
    def _restore(live_path: Path, backup_path: Path, live_existed: bool) -> None:
        if live_existed:
            if not backup_path.is_file():
                raise WorkbenchError(f"找不到晋升备份: {backup_path}")
            atomic_write_bytes(live_path, backup_path.read_bytes())
        else:
            try:
                live_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise WorkbenchError(f"无法删除新插件文件: {exc}") from exc

    def _reload(self, name: str, *, expect_loaded: bool = True) -> tuple[bool, str]:
        if self.reload_plugins is None:
            return True, ""
        try:
            result = self.reload_plugins()
        except Exception as exc:  # the callback is the host app
            return False, str(exc)
        return self._interpret_reload(result, name, expect_loaded=expect_loaded)

    @staticmethod
    def _interpret_reload(
        result: Any, name: str, *, expect_loaded: bool = True
    ) -> tuple[bool, str]:
        if result is None:
            return True, ""
        if isinstance(result, bool):
            return result, "" if result else "reload returned False"
        if not isinstance(result, dict):
            return True, ""
        if result.get("ok") is False:
            return False, str(result.get("error") or result)
        # The plugin kernel names plugins by stem ("old_plugin"); the live
        # file is "old_plugin.py". Accept either shape from the host.
        stem = Path(name).stem
        identities = {name, stem}

        def _contains(values: Any) -> bool:
            if not isinstance(values, (list, tuple, set)):
                return False
            return any(value in identities for value in values)

        explicit = result.get("result")
        if explicit in ("failed", "skipped"):
            return False, str(result.get("error") or f"plugin {explicit}")
        for key in ("failed", "skipped"):
            if _contains(result.get(key)):
                return False, f"plugin {key}: {name}"
        loaded = result.get("loaded")
        if isinstance(loaded, (list, tuple, set)):
            if _contains(loaded):
                return True, ""
            if not expect_loaded:
                # A rolled-back new plugin is supposed to be absent.
                return True, ""
            # A callback that reports a loaded list is authoritative.
            return False, f"plugin not loaded: {name}"
        if loaded is True:
            return True, ""
        if not expect_loaded and explicit in ("unloaded", "removed", "deleted"):
            return True, ""
        if result.get("name") in identities and explicit == "loaded":
            return True, ""
        return True, ""


__all__ = ["PluginPromoter"]
