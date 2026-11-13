"""告警聚合与恢复引擎。

设计口径（对应验收序列 L 与各条边界）：

- 只有「已启用」且「是否启用=是」的规则能参与聚合，草稿/待校验/已停用规则的事件直接拒收，
  不产生任何通知或聚合包（示例告警不能绕过停用条件）。
- 聚合判断同时读取七个字段：监控目标、告警条件、告警级别、通知渠道、静默时段、聚合窗口、
  是否启用；任一字段不同都会落到不同聚合包，不同监控目标天然不会合并。
- 静默期内只累计不发送：事件标记 silenced 并挂到当前聚合包，窗口结束（静默结束或聚合窗口
  到期）时只生成一份摘要、只发送一次；摘要按告警级别分组，每级列 1 项。
- 恢复永远以原告警事件为目标：按 event_id 幂等处理，并发/重复恢复只生效一次；恢复项挂在
  原事件所在的聚合包上，并只关闭该包对应的工作台待处理项，不跨规则、不跨目标连带关闭。
- 发送失败保留原聚合包，重试从原包继续，不拆包、不重建；历史通知按发送当时的规则条件
  快照留档。
- 告警结论（摘要发送、恢复生效）同步写入采集日志模块。

引擎只依赖标准库，时间全部可注入，便于按验收序列回放。
"""
from __future__ import annotations

import threading
from datetime import datetime, time, timedelta
from typing import Any

from app.store import store

ALERT_MODULE = "alert"
LOG_MODULE = "log"

# 聚合判断必须读取的七个字段（告警编号随规则本身携带，不放进键里）
KEY_FIELDS = ["监控目标", "告警条件", "告警级别", "通知渠道", "静默时段", "聚合窗口", "是否启用"]
LEVEL_ORDER = {"提示": 0, "警告": 1, "严重": 2}

# 工作台/通知/日志里用到的状态文案
STATE_OPEN = "open"          # 累计中，窗口未结束
STATE_READY = "ready"        # 摘要已生成，等待发送
STATE_SENT = "sent"          # 已成功发送
STATE_FAILED = "failed"      # 发送失败，保留原包等待续发


def parse_clock(value: str | datetime | None) -> datetime:
    """把 'YYYY-MM-DD HH:MM' 之类的入参转成 datetime；缺省取当前时间。"""
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                today = datetime.now()
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            return parsed
        except ValueError:
            continue
    raise ValueError(f"时间格式无法识别：{value}")


def parse_silence_window(raw: Any) -> tuple[time, time] | None:
    """解析 '20:00-20:10' 形式的每日静默时段。"""
    text = str(raw or "").strip()
    if "-" not in text:
        return None
    start_text, end_text = (part.strip() for part in text.split("-", 1))
    try:
        start = datetime.strptime(start_text, "%H:%M").time()
        end = datetime.strptime(end_text, "%H:%M").time()
    except ValueError:
        return None
    return start, end


def parse_aggregate_minutes(raw: Any) -> int:
    """解析 '10m' / '5m' 形式的聚合窗口，缺省 10 分钟。"""
    text = str(raw or "").strip().lower()
    if text.endswith("m") and text[:-1].isdigit():
        return max(int(text[:-1]), 1)
    if text.isdigit():
        return max(int(text), 1)
    return 10


def in_silence(rule: dict[str, Any], moment: datetime) -> bool:
    window = parse_silence_window(rule.get("静默时段"))
    if window is None:
        return False
    start, end = window
    return start <= moment.time() < end


def silence_end_at(rule: dict[str, Any], moment: datetime) -> datetime:
    window = parse_silence_window(rule.get("静默时段"))
    if window is None:
        return moment
    end = window[1]
    return moment.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)


def aggregation_key(rule: dict[str, Any]) -> tuple[str, ...]:
    """七个字段完全一致才视为同一聚合口径。"""
    return tuple(str(rule.get(field) or "") for field in KEY_FIELDS)


class AlertEngine:
    """聚合、恢复、发送与留档的全部有状态行为都收敛在这个类里。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self.events: list[dict[str, Any]] = []
        self.packages: list[dict[str, Any]] = []
        self.recoveries: list[dict[str, Any]] = []
        self.workbench: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        # ok：正常发送；fail：一直失败；fail_once：下一次失败后自动恢复
        self.notifier_mode = "ok"

    # ------------------------------------------------------------------ 基础工具
    def reset(self) -> None:
        """清空引擎运行态（规则本身仍在 store 里），供演示与验收回放使用。"""
        with self._lock:
            self._seq = 0
            self.events.clear()
            self.packages.clear()
            self.recoveries.clear()
            self.workbench.clear()
            self.notifications.clear()
            self.notifier_mode = "ok"

    def set_notifier(self, mode: str) -> str:
        if mode not in ("ok", "fail", "fail_once"):
            raise ValueError("通知渠道模拟模式只支持 ok / fail / fail_once")
        with self._lock:
            self.notifier_mode = mode
        return self.notifier_mode

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _find_rule(self, rule_ref: str | int) -> dict[str, Any] | None:
        rows = store.rows(ALERT_MODULE)
        if isinstance(rule_ref, int) or str(rule_ref).isdigit():
            return store.find(ALERT_MODULE, int(rule_ref))
        for row in rows:
            if str(row.get("告警编号")) == str(rule_ref):
                return row
        return None

    @staticmethod
    def is_enabled(rule: dict[str, Any]) -> bool:
        """已启用状态与启用标记同时满足才允许参与聚合，停用条件不可绕过。"""
        return rule.get("status") == "已启用" and str(rule.get("是否启用") or "") == "是"

    def _open_package(self, rule: dict[str, Any]) -> dict[str, Any] | None:
        key = aggregation_key(rule)
        for package in self.packages:
            if (
                package["rule_id"] == rule.get("id")
                and package["key"] == key
                and package["state"] == STATE_OPEN
            ):
                return package
        return None

    # ------------------------------------------------------------------ 事件受理
    def ingest(
        self,
        rule_ref: str | int,
        content: str = "",
        occurred_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """受理一条采集失败告警事件。

        静默期内只累计；非静默期按聚合窗口累计；窗口到期由 tick/下一次受理触发摘要发送。
        """
        with self._lock:
            moment = parse_clock(occurred_at)
            rule = self._find_rule(rule_ref)
            if rule is None:
                return {"accepted": False, "reason": "告警规则不存在", "event": None}
            if not self.is_enabled(rule):
                # 草稿/待校验/已停用：不生成事件、不进聚合包、不发通知
                return {
                    "accepted": False,
                    "reason": f"规则 {rule.get('告警编号')} 当前为「{rule.get('status')}」且"
                    f"是否启用={rule.get('是否启用')}，停用条件下不参与聚合",
                    "event": None,
                }

            # 先把已到期的包结算掉，避免跨窗口事件被错误合并
            self._close_due_packages(moment, rule_id=int(rule["id"]))

            silenced = in_silence(rule, moment)
            package = self._open_package(rule)
            if package is None:
                if silenced:
                    kind, window_end = "silence", silence_end_at(rule, moment)
                else:
                    kind = "window"
                    window_end = moment + timedelta(minutes=parse_aggregate_minutes(rule.get("聚合窗口")))
                package = self._create_package(rule, kind, moment, window_end)

            event = {
                "id": self._next_id("EVT"),
                "rule_id": rule["id"],
                "rule_code": rule.get("告警编号"),
                "package_id": package["id"],
                "监控目标": rule.get("监控目标"),
                "告警条件": rule.get("告警条件"),
                "告警级别": rule.get("告警级别"),
                "通知渠道": rule.get("通知渠道"),
                "content": content or f"{rule.get('监控目标')} 触发告警条件「{rule.get('告警条件')}」",
                "occurred_at": moment,
                "silenced": silenced,
                "recovered": False,
            }
            package["events"].append(event)
            self.events.append(event)

            # 极端情况下事件时间已越过窗口结束时间，立即结算
            self._close_due_packages(moment, rule_id=int(rule["id"]))
            return {
                "accepted": True,
                "reason": "静默期内累计，窗口结束统一发送" if silenced else "已计入聚合窗口",
                "event": self._public_event(event),
                "package_id": package["id"],
            }

    def _create_package(
        self,
        rule: dict[str, Any],
        kind: str,
        opened_at: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        package = {
            "id": self._next_id("PKG"),
            "rule_id": rule["id"],
            "rule_code": rule.get("告警编号"),
            "key": aggregation_key(rule),
            "kind": kind,
            "state": STATE_OPEN,
            "监控目标": rule.get("监控目标"),
            "告警条件": rule.get("告警条件"),
            "告警级别": rule.get("告警级别"),
            "通知渠道": rule.get("通知渠道"),
            "静默时段": rule.get("静默时段"),
            "聚合窗口": rule.get("聚合窗口"),
            "opened_at": opened_at,
            "window_end": window_end,
            "closed_at": None,
            "sent_at": None,
            "events": [],
            "recoveries": [],
            "send_attempts": 0,
            "last_error": None,
            "summary": None,
            "notification_id": None,
        }
        self.packages.append(package)
        workbench_item = {
            "id": self._next_id("WB"),
            "package_id": package["id"],
            "rule_code": package["rule_code"],
            "target": package["监控目标"],
            "level": package["告警级别"],
            "opened_at": opened_at,
            "pending": True,
            "closed_at": None,
            "close_reason": None,
        }
        self.workbench.append(workbench_item)
        package["workbench_id"] = workbench_item["id"]
        return package

    # ------------------------------------------------------------------ 窗口结算
    def tick(self, now: str | datetime | None = None) -> list[dict[str, Any]]:
        """推进时间：结算所有到期聚合包并尝试发送摘要。"""
        with self._lock:
            moment = parse_clock(now)
            due = [pkg for pkg in self.packages if pkg["state"] == STATE_OPEN and moment >= pkg["window_end"]]
            for package in due:
                self._close_and_dispatch(package, moment)
            return [self._public_package(pkg) for pkg in due]

    def _close_due_packages(self, moment: datetime, rule_id: int | None = None) -> None:
        for package in self.packages:
            if package["state"] != STATE_OPEN or moment < package["window_end"]:
                continue
            if rule_id is not None and package["rule_id"] != rule_id:
                continue
            self._close_and_dispatch(package, moment)

    def _close_and_dispatch(self, package: dict[str, Any], moment: datetime) -> None:
        package["state"] = STATE_READY
        package["closed_at"] = moment
        package["summary"] = self._build_summary(package)
        self._dispatch(package, moment)

    @staticmethod
    def _build_summary(package: dict[str, Any]) -> dict[str, Any]:
        """按严重度分组：每个级别只列 1 项，挂载该级别下全部事件。"""
        groups: dict[str, dict[str, Any]] = {}
        for event in package["events"]:
            group = groups.setdefault(event["告警级别"], {"level": event["告警级别"], "count": 0, "event_ids": []})
            group["count"] += 1
            group["event_ids"].append(event["id"])
        ordered = sorted(groups.values(), key=lambda item: LEVEL_ORDER.get(item["level"], -1), reverse=True)
        lines = [
            f"[{item['level']}] {item['count']} 个事件：{', '.join(item['event_ids'])}"
            for item in ordered
        ]
        for recovery in package["recoveries"]:
            lines.append(f"[恢复] {recovery['id']} 对应原事件 {recovery['event_id']} 已恢复")
        return {
            "title": f"{package['rule_code']} 维护摘要" if package["kind"] == "silence"
            else f"{package['rule_code']} 聚合摘要",
            "level_groups": ordered,
            "event_count": len(package["events"]),
            "recovery_count": len(package["recoveries"]),
            "text": "；".join(lines),
        }

    # ------------------------------------------------------------------ 发送与续发
    def _dispatch(self, package: dict[str, Any], moment: datetime) -> bool:
        package["send_attempts"] += 1
        ok, error = self._channel_send(package)
        notification = None
        if package["notification_id"]:
            notification = next(
                (item for item in self.notifications if item["id"] == package["notification_id"]), None
            )
        if notification is None:
            notification = {
                "id": self._next_id("NTF"),
                "package_id": package["id"],
                "rule_code": package["rule_code"],
                "channel": package["通知渠道"],
                "level": package["告警级别"],
                "target": package["监控目标"],
                "kind": "summary",
                "status": STATE_FAILED,
                "attempts": 0,
                "first_attempt_at": moment,
                "sent_at": None,
                "snapshot": None,
                "summary_text": None,
                "event_count": 0,
                "recovery_count": 0,
            }
            self.notifications.append(notification)
            package["notification_id"] = notification["id"]

        notification["attempts"] = package["send_attempts"]
        notification["event_count"] = len(package["events"])
        notification["recovery_count"] = len(package["recoveries"])
        notification["summary_text"] = package["summary"]["text"] if package["summary"] else None

        if ok:
            package["state"] = STATE_SENT
            package["sent_at"] = moment
            package["last_error"] = None
            notification["status"] = STATE_SENT
            notification["sent_at"] = moment
            # 历史通知按发送当时的规则条件留档
            notification["snapshot"] = {field: package.get(field) for field in KEY_FIELDS}
            self._sync_log(
                package=package,
                status="完全失败",
                abnormal=True,
                detail=f"告警结论：{notification['summary_text']}",
                moment=moment,
            )
            return True

        package["state"] = STATE_FAILED
        package["last_error"] = error
        notification["status"] = STATE_FAILED
        return False

    def _channel_send(self, package: dict[str, Any]) -> tuple[bool, str | None]:
        """模拟通知渠道；可通过 set_notifier 注入故障验证续发。"""
        if self.notifier_mode == "fail":
            return False, "模拟渠道持续故障"
        if self.notifier_mode == "fail_once":
            self.notifier_mode = "ok"
            return False, "模拟渠道瞬时故障"
        return True, None

    def retry_package(self, package_id: str, now: str | datetime | None = None) -> dict[str, Any]:
        """发送失败后从原聚合包继续：不拆包、不丢事件、不重复产生成功通知。"""
        with self._lock:
            moment = parse_clock(now)
            package = next((item for item in self.packages if item["id"] == package_id), None)
            if package is None:
                return {"ok": False, "message": f"聚合包 {package_id} 不存在"}
            if package["state"] == STATE_SENT:
                return {"ok": True, "message": "聚合包已成功发送，无需重发", "package": self._public_package(package)}
            if package["state"] == STATE_OPEN:
                return {"ok": False, "message": "聚合窗口尚未结束，暂不能发送"}
            # 恢复项可能在失败后才挂上，重发前刷新摘要
            package["summary"] = self._build_summary(package)
            ok = self._dispatch(package, moment)
            return {
                "ok": ok,
                "message": "已从原聚合包续发成功" if ok else f"续发仍失败：{package['last_error']}",
                "package": self._public_package(package),
            }

    # ------------------------------------------------------------------ 恢复
    def recover(
        self,
        event_id: str | None = None,
        package_id: str | None = None,
        rule_ref: str | int | None = None,
        now: str | datetime | None = None,
    ) -> dict[str, Any]:
        """以原告警事件为恢复目标；重复/并发恢复只生效一次。"""
        with self._lock:
            moment = parse_clock(now)
            event = self._resolve_recovery_target(event_id, package_id, rule_ref)
            if isinstance(event, str):
                return {"effective": False, "reason": event, "recovery": None}

            if event["recovered"]:
                # 幂等：同一次异常的重复恢复不再生效
                return {
                    "effective": False,
                    "reason": f"原事件 {event['id']} 已恢复，重复恢复已忽略",
                    "recovery": None,
                }

            event["recovered"] = True
            recovery = {
                "id": self._next_id("REC"),
                "event_id": event["id"],
                "package_id": event["package_id"],
                "rule_code": event["rule_code"],
                "target": event["监控目标"],
                "level": event["告警级别"],
                "requested_at": moment,
                "effective": True,
            }
            self.recoveries.append(recovery)

            package = next(item for item in self.packages if item["id"] == event["package_id"])
            package["recoveries"].append(recovery)
            if package["summary"] is not None:
                package["summary"] = self._build_summary(package)

            # 工作台只关闭原告警事件所在聚合包对应的那一项
            workbench_item = next(
                (item for item in self.workbench if item["package_id"] == package["id"] and item["pending"]),
                None,
            )
            if workbench_item is not None:
                workbench_item["pending"] = False
                workbench_item["closed_at"] = moment
                workbench_item["close_reason"] = f"原事件 {event['id']} 恢复，联动关闭"

            self._sync_log(
                package=package,
                status="成功",
                abnormal=False,
                detail=f"告警恢复：{event['rule_code']} {event['监控目标']} 原事件 {event['id']} 已恢复",
                moment=moment,
                recovery=recovery,
            )
            return {"effective": True, "reason": "恢复已生效", "recovery": recovery}

    def _resolve_recovery_target(
        self,
        event_id: str | None,
        package_id: str | None,
        rule_ref: str | int | None,
    ) -> dict[str, Any] | str:
        if event_id:
            event = next((item for item in self.events if item["id"] == event_id), None)
            if event is None:
                return f"原告警事件 {event_id} 不存在"
            if rule_ref is not None:
                rule = self._find_rule(rule_ref)
                if rule is not None and event["rule_id"] != rule.get("id"):
                    return "恢复目标与指定规则不一致，以原告警事件为准已拒绝"
            return event
        if package_id:
            package = next((item for item in self.packages if item["id"] == package_id), None)
            if package is None:
                return f"聚合包 {package_id} 不存在"
            if not package["events"]:
                return f"聚合包 {package_id} 内没有原告警事件"
            # 冲突时以最早的原告警事件为恢复目标
            return package["events"][0]
        return "恢复必须指定原告警事件或原聚合包"

    # ------------------------------------------------------------------ 日志同步
    def _sync_log(
        self,
        *,
        package: dict[str, Any],
        status: str,
        abnormal: bool,
        detail: str,
        moment: datetime,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        log_rows = store.rows(LOG_MODULE)
        log_id = recovery["id"] if recovery else package["id"]
        log_rows.append({
            "id": max((int(row.get("id", 0)) for row in log_rows), default=0) + 1,
            "status": status,
            "pending": False,
            "abnormal": abnormal,
            "日志编号": f"LOG-ALERT-{log_id}",
            "关联任务": package["监控目标"],
            "开始时间": moment.strftime("%Y-%m-%d %H:%M"),
            "结束时间": moment.strftime("%Y-%m-%d %H:%M"),
            "采集行数": "0",
            "成功条数": str(len(package["events"])) if status == "成功" else "0",
            "失败条数": "0" if status == "成功" else str(len(package["events"])),
            "错误信息": detail,
        })

    # ------------------------------------------------------------------ 只读视图
    @staticmethod
    def _public_event(event: dict[str, Any]) -> dict[str, Any]:
        return dict(event)

    def _public_package(self, package: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": package["id"],
            "rule_code": package["rule_code"],
            "kind": package["kind"],
            "state": package["state"],
            "监控目标": package["监控目标"],
            "告警条件": package["告警条件"],
            "告警级别": package["告警级别"],
            "通知渠道": package["通知渠道"],
            "静默时段": package["静默时段"],
            "聚合窗口": package["聚合窗口"],
            "opened_at": package["opened_at"],
            "window_end": package["window_end"],
            "closed_at": package["closed_at"],
            "sent_at": package["sent_at"],
            "event_count": len(package["events"]),
            "recovery_count": len(package["recoveries"]),
            "send_attempts": package["send_attempts"],
            "last_error": package["last_error"],
            "summary": package["summary"],
            "workbench_id": package["workbench_id"],
            "event_ids": [event["id"] for event in package["events"]],
            "recovery_ids": [item["id"] for item in package["recoveries"]],
        }

    def list_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public_event(item) for item in self.events]

    def list_packages(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public_package(item) for item in self.packages]

    def list_workbench(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.workbench]

    def list_notifications(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.notifications]

    def list_recoveries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.recoveries]


# 单例：路由与测试共用同一份运行态
alert_engine = AlertEngine()
