"""验收序列 L：告警聚合与恢复。

运行：cd backend && python -m tests.test_acceptance_l

覆盖：
1. ALER-0002 维护窗口 20:00-20:10 内触发三次，20:11 只生成一份摘要、只发送一次，
   摘要含 3 个事件与 1 个恢复项，恢复项只挂 ALER-0002，20:05 事件归入维护摘要；
2. ALER-0003 的 20:15 事件按自身静默/聚合条件处理，静默结束后仍按自身级别（提示）发送；
3. ALER-0001 草稿规则不生成任何通知或事件；
4. 不同监控目标、不同级别不串包；摘要按严重度各列 1 项；
5. 恢复只关闭自己的工作台待处理项，重复/并发恢复只生效一次；
6. 发送失败保留原聚合包，续发从原包继续，通知按当时条件留档；
7. 告警结论同步采集日志。
"""
from __future__ import annotations

from datetime import datetime

from app.services.alert_engine import (
    STATE_FAILED,
    STATE_OPEN,
    STATE_SENT,
    alert_engine,
    aggregation_key,
)
from app.store import store

DATE = "2026-11-13"


def at(hm: str) -> datetime:
    return datetime.strptime(f"{DATE} {hm}", "%Y-%m-%d %H:%M")


class CheckFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)
    print(f"  ✓ {message}")


def rule(code: str) -> dict:
    return next(row for row in store.rows("alert") if row["告警编号"] == code)


def main() -> None:
    print("== 验收序列 L：告警聚合与恢复 ==")
    alert_engine.reset()
    log_count_0 = len(store.rows("log"))

    r1, r2, r3 = rule("ALER-0001"), rule("ALER-0002"), rule("ALER-0003")
    print("[前置] 规则档位与字段")
    check(r1["status"] == "草稿" and r1["是否启用"] == "否", "ALER-0001 为草稿且未启用，级别=警告、监控 TASK-0001")
    check(r2["status"] == "待校验" and r2["是否启用"] == "否", "ALER-0002 初始为待校验档，级别=严重、监控 TASK-0002")
    check(r3["status"] == "已启用" and r3["是否启用"] == "是", "ALER-0003 已启用，级别=提示、监控 TASK-0003")
    check(r1["告警级别"] == "警告" and r2["告警级别"] == "严重" and r3["告警级别"] == "提示",
          "多级别边界：警告 / 严重 / 提示")
    check([r1["监控目标"], r2["监控目标"], r3["监控目标"]] == ["TASK-0001", "TASK-0002", "TASK-0003"],
          "多目标边界：三条规则分别监控 TASK-0001/0002/0003")
    check(aggregation_key(r2) != aggregation_key(r3), "七字段口径不同的规则不会落到同一聚合包")

    print("[边界] 草稿规则不能绕过停用条件")
    rejected = alert_engine.ingest("ALER-0001", "草稿规则事件", at("20:03"))
    check(rejected["accepted"] is False, "ALER-0001 草稿规则事件被拒收")
    check(len(alert_engine.list_events()) == 0 and len(alert_engine.list_packages()) == 0,
          "拒收后没有产生事件、聚合包与通知")

    print("[边界] 待校验规则同样不能参与聚合；启用后才放行")
    rejected_pending = alert_engine.ingest("ALER-0002", "启用前事件", at("20:01"))
    check(rejected_pending["accepted"] is False, "ALER-0002 待校验档事件被拒收")
    r2["status"] = "已启用"
    r2["是否启用"] = "是"
    check(alert_engine.is_enabled(r2), "ALER-0002 启用后具备聚合资格")

    print("[主序列] 维护窗口 20:00-20:10 内 ALER-0002 触发三次，静默期只累计")
    first = alert_engine.ingest("ALER-0002", "TASK-0002 连续失败 #1", at("20:03"))
    alert_engine.ingest("ALER-0002", "TASK-0002 连续失败 #2", at("20:05"))
    alert_engine.ingest("ALER-0002", "TASK-0002 连续失败 #3", at("20:08"))
    check(first["accepted"] is True and first["event"]["silenced"] is True, "20:03 事件标记为静默累计")
    packages_open = alert_engine.list_packages()
    check(len(packages_open) == 1, "三个事件合并进同一个维护聚合包")
    pkg2 = packages_open[0]
    check(pkg2["kind"] == "silence" and pkg2["event_count"] == 3, "维护摘要包含 3 个事件")
    check(pkg2["state"] == STATE_OPEN, "静默窗口未结束前不发送")
    check(len(alert_engine.list_notifications()) == 0, "静默期内没有任何通知发出")
    check(len(alert_engine.list_workbench()) == 1 and alert_engine.list_workbench()[0]["pending"] is True,
          "工作台只登记了 ALER-0002 的 1 个待处理项")

    print("[主序列] 窗口内恢复，恢复项挂到原聚合包")
    event_ids = pkg2["event_ids"]
    rec = alert_engine.recover(event_id=event_ids[0], rule_ref="ALER-0002", now=at("20:09"))
    check(rec["effective"] is True, "20:09 对最早原告警事件的恢复生效")
    pkg2_open = next(p for p in alert_engine.list_packages() if p["id"] == pkg2["id"])
    check(pkg2_open["recovery_count"] == 1, "恢复项只挂在 ALER-0002 聚合包上（1 个恢复项）")

    print("[幂等] 并发/重复恢复只生效一次")
    again = alert_engine.recover(event_id=event_ids[0], rule_ref="ALER-0002", now=at("20:09"))
    check(again["effective"] is False, "同一原告警事件的重复恢复被幂等忽略")
    check(len(alert_engine.list_recoveries()) == 1, "恢复记录仍只有 1 条")
    wb2 = next(item for item in alert_engine.list_workbench() if item["package_id"] == pkg2["id"])
    check(wb2["pending"] is False and wb2["close_reason"] is not None, "恢复已联动关闭 ALER-0002 工作台项")

    print("[主序列] 20:11 窗口结束：只生成一份摘要、只发送一次")
    tick_result = alert_engine.tick(at("20:11"))
    check(len(tick_result) == 1, "20:11 只结算出 1 个到期聚合包")
    pkg2_sent = next(p for p in alert_engine.list_packages() if p["id"] == pkg2["id"])
    check(pkg2_sent["state"] == STATE_SENT, "维护摘要已成功发送")
    check(pkg2_sent["summary"]["event_count"] == 3 and pkg2_sent["summary"]["recovery_count"] == 1,
          "维护摘要包含 3 个事件和 1 个恢复项")
    notifications = alert_engine.list_notifications()
    check(len(notifications) == 1 and notifications[0]["attempts"] == 1, "20:11 只发送 1 次通知")
    check(notifications[0]["snapshot"] is not None and notifications[0]["snapshot"]["监控目标"] == "TASK-0002",
          "历史通知按发送当时的规则条件留档（快照含七字段）")
    check(len(pkg2_sent["summary"]["level_groups"]) == 1
          and pkg2_sent["summary"]["level_groups"][0]["level"] == "严重"
          and pkg2_sent["summary"]["level_groups"][0]["count"] == 3,
          "摘要按严重度分组：严重级列 1 项，覆盖全部 3 个事件")

    print("[ALER-0003] 20:15 事件按自身静默时段（20:12-20:14）与提示级别处理")
    e3 = alert_engine.ingest("ALER-0003", "TASK-0003 连续失败", at("20:15"))
    check(e3["accepted"] is True and e3["event"]["silenced"] is False, "20:15 已不在 ALER-0003 静默时段")
    alert_engine.tick(at("20:21"))  # 聚合窗口 5m：20:15 + 5m = 20:20 到期
    pkgs3 = [p for p in alert_engine.list_packages() if p["rule_code"] == "ALER-0003"]
    check(len(pkgs3) == 1 and pkgs3[0]["state"] == STATE_SENT, "ALER-0003 独立成包并已发送")
    check(pkgs3[0]["告警级别"] == "提示" and pkgs3[0]["通知渠道"] == "站内信",
          "静默窗口结束后仍按 ALER-0003 自身级别（提示）与渠道发送")
    check(all(p["rule_code"] != "ALER-0002" for p in pkgs3), "ALER-0003 没有并入 ALER-0002 的包")

    print("[边界] ALER-0003 恢复只关闭它自己的待处理项，不影响其它规则")
    wb3 = next(item for item in alert_engine.list_workbench() if item["package_id"] == pkgs3[0]["id"])
    check(wb3["pending"] is True, "ALER-0003 工作台项在恢复前保持待处理")
    rec3 = alert_engine.recover(event_id=pkgs3[0]["event_ids"][0], rule_ref="ALER-0003", now=at("20:22"))
    check(rec3["effective"] is True, "ALER-0003 恢复生效")
    wb3_after = next(item for item in alert_engine.list_workbench() if item["id"] == wb3["id"])
    wb2_after = next(item for item in alert_engine.list_workbench() if item["id"] == wb2["id"])
    check(wb3_after["pending"] is False and wb2_after["pending"] is False,
          "两条工作台项各自关闭，恢复不跨规则连带")
    check(len(alert_engine.list_packages()) == 2, "多目标边界：最终只有 2 个聚合包，TASK-0002/0003 未合并")

    print("[同步] 告警结论写入采集日志")
    logs = store.rows("log")
    alert_logs = [row for row in logs if str(row.get("日志编号", "")).startswith("LOG-ALERT-")]
    check(len(logs) - log_count_0 == len(alert_logs) and len(alert_logs) >= 3,
          "摘要发送与两次恢复都同步了采集日志")
    check(any("维护摘要" in row["错误信息"] or "告警结论" in row["错误信息"] for row in alert_logs),
          "日志中包含 ALER-0002 维护摘要结论")
    check(any("已恢复" in row["错误信息"] and row["关联任务"] == "TASK-0003" for row in alert_logs),
          "ALER-0003 恢复结论同步到 TASK-0003 的采集日志")

    print("[故障续发] 发送失败保留原聚合包，恢复后从原包继续")
    alert_engine.reset()
    alert_engine.set_notifier("fail_once")
    alert_engine.ingest("ALER-0003", "故障演练事件", at("20:15"))
    alert_engine.tick(at("20:21"))
    failed_pkgs = [p for p in alert_engine.list_packages() if p["state"] == STATE_FAILED]
    check(len(failed_pkgs) == 1, "首次发送失败，聚合包保留为 failed 而不是丢弃")
    failed_id = failed_pkgs[0]["id"]
    ntf = alert_engine.list_notifications()[0]
    check(ntf["status"] == STATE_FAILED and ntf["attempts"] == 1, "失败通知留档，尝试次数为 1")
    retry = alert_engine.retry_package(failed_id, at("20:22"))
    check(retry["ok"] is True and retry["package"]["id"] == failed_id, "续发从原聚合包继续，未拆新包")
    check(retry["package"]["state"] == STATE_SENT and retry["package"]["send_attempts"] == 2,
          "同一聚合包第 2 次尝试成功，事件不重复通知")
    check(len(alert_engine.list_notifications()) == 1, "历史通知仍是同一条记录，快照保留当时条件")

    print("[持续故障] 一直失败时状态保持 failed，工作台项不被错误关闭")
    alert_engine.reset()
    alert_engine.set_notifier("fail")
    alert_engine.ingest("ALER-0003", "持续故障事件", at("20:15"))
    alert_engine.tick(at("20:21"))
    pkg_fail = alert_engine.list_packages()[0]
    check(pkg_fail["state"] == STATE_FAILED and pkg_fail["last_error"], "渠道持续故障时包保持 failed 并记录原因")
    check(alert_engine.list_workbench()[0]["pending"] is True, "发送失败不关闭工作台待处理项")
    retry_again = alert_engine.retry_package(pkg_fail["id"], at("20:23"))
    check(retry_again["ok"] is False, "持续故障下续发仍然失败，原包继续保留")

    print("\n全部验收项通过 ✅")


if __name__ == "__main__":
    main()
