"""采集告警接口：维护告警规则，并提供告警聚合、恢复与历史通知能力。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert import AlertService
from app.services.alert_engine import alert_engine

router = APIRouter(prefix="/api/alert", tags=["采集告警"])

service = AlertService()

LIST_FIELDS = ["告警编号", "监控目标", "告警条件", "告警级别", "通知渠道", "静默时段", "聚合窗口", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按告警编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按告警编号与状态过滤采集告警列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条告警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"告警规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条告警规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="告警规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条告警规则执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集告警清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alert", "total": total, "items": items}


# ------------------------------------------------------------------ 告警聚合与恢复
@router.post("/engine/events")
def ingest_event(payload: EntryPayload) -> dict[str, Any]:
    """受理一条采集失败告警事件。

    停用/草稿规则直接拒收；静默期内只累计，窗口结束由 engine/tick 结算成一份摘要。
    values 支持：rule（告警编号或 id）、content、occurred_at。
    """
    values = payload.values
    rule_ref = values.get("rule") or values.get("rule_id") or values.get("告警编号")
    if rule_ref is None:
        raise HTTPException(status_code=400, detail="必须指定告警规则 rule（告警编号或 id）")
    result = alert_engine.ingest(
        rule_ref,
        content=str(values.get("content") or ""),
        occurred_at=values.get("occurred_at"),
    )
    return result


@router.post("/engine/tick")
def engine_tick(payload: EntryPayload | None = None) -> dict[str, Any]:
    """推进聚合时钟：结算所有到期窗口并尝试发送摘要。"""
    now = payload.values.get("now") if payload and payload.values else None
    closed = alert_engine.tick(now)
    return {"closed": len(closed), "packages": closed}


@router.post("/engine/recoveries")
def recover_event(payload: EntryPayload) -> dict[str, Any]:
    """以原告警事件为目标执行恢复；重复/并发恢复只生效一次。"""
    values = payload.values
    result = alert_engine.recover(
        event_id=values.get("event_id"),
        package_id=values.get("package_id"),
        rule_ref=values.get("rule"),
        now=values.get("now"),
    )
    return result


@router.post("/engine/packages/{package_id}/retry")
def retry_package(package_id: str, payload: EntryPayload | None = None) -> dict[str, Any]:
    """发送失败后从原聚合包继续发送。"""
    now = payload.values.get("now") if payload and payload.values else None
    return alert_engine.retry_package(package_id, now)


@router.get("/engine/state")
def engine_state() -> dict[str, Any]:
    """聚合引擎运行态：事件、聚合包、工作台待处理项、历史通知、恢复记录。"""
    return {
        "events": alert_engine.list_events(),
        "packages": alert_engine.list_packages(),
        "workbench": alert_engine.list_workbench(),
        "notifications": alert_engine.list_notifications(),
        "recoveries": alert_engine.list_recoveries(),
    }


@router.post("/engine/reset")
def engine_reset() -> dict[str, Any]:
    """清空引擎运行态，便于按验收序列重新回放。"""
    alert_engine.reset()
    return {"ok": True, "message": "聚合引擎运行态已清空"}


@router.post("/engine/notifier")
def set_notifier(payload: EntryPayload) -> dict[str, Any]:
    """注入通知渠道故障模式：ok / fail / fail_once，用于验证发送失败续发。"""
    mode = str(payload.values.get("mode") or "").strip()
    try:
        current = alert_engine.set_notifier(mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "notifier_mode": current}
