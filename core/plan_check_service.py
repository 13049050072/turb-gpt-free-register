# -*- coding: utf-8 -*-
"""套餐/Plus 资格查询后台队列。"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from config import proxy as proxy_cfg
from core import db
from core.chatgpt_plan import check_account_plan

logger = logging.getLogger(__name__)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("PLAN_CHECK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("PLAN_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="plan-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_LOCK = threading.Lock()
_RUNNING: set[int] = set()
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0)
    jitter = _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _registration_recheck_delay() -> float:
    return _float_setting("PLAN_CHECK_REGISTRATION_RECHECK_DELAY", 2.0, 0.0, 30.0)


def _run_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
    session=None,
    run_id: str | None = None,
) -> dict:
    expected_account = {"email": email, "access_token": access_token}
    run_id = run_id or uuid.uuid4().hex
    try:
        _wait_for_rate_slot()
        if not db.mark_account_plan_check_running(account_id, run_id=run_id, expected_account=expected_account):
            return {"ok": False, "error": "账号已删除、邮箱/AT 已变更或套餐任务已被重置"}
        session_options = {"session": session} if session is not None else {}
        result = check_account_plan(
            access_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
            **session_options,
        )

        recheck_delay = _registration_recheck_delay()
        should_recheck = (
            trigger == "registration_auto"
            and recheck_delay > 0
            and bool(result.get("ok"))
            and str(result.get("current_plan_type") or "").lower() == "free"
            and not bool(result.get("plus_trial_eligible"))
        )
        if should_recheck:
            logger.info("[Plan] 新账号暂未发现 Plus 试用资格，%.1fs 后复查一次: %s", recheck_delay, email)
            time.sleep(recheck_delay)
            _wait_for_rate_slot()
            if not db.mark_account_plan_check_running(account_id, run_id=run_id, expected_account=expected_account):
                return {"ok": False, "error": "账号已变更，取消套餐复查"}
            recheck_result = check_account_plan(
                access_token,
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
                max_attempts=1,
                **session_options,
            )
            if recheck_result.get("ok"):
                result = recheck_result
            else:
                logger.warning(
                    "[Plan] 新账号资格复查失败，保留首次成功结果: %s, %s",
                    email,
                    recheck_result.get("error") or "未知错误",
                )

        if not db.update_account_plan_check(acc_id=account_id, result=result, expected_account=expected_account, expected_run_id=run_id):
            return {"ok": False, "error": "账号已删除或邮箱/AT 已变更，丢弃旧套餐结果"}
        if result.get("ok"):
            logger.info(
                "[Plan] 后台查询成功: %s, plan=%s, plus_trial=%s, trigger=%s",
                email,
                result.get("current_plan_type") or "unknown",
                bool(result.get("plus_trial_eligible")),
                trigger,
            )
        else:
            logger.warning(
                "[Plan] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_plan_check(acc_id=account_id, result=result, expected_account=expected_account, expected_run_id=run_id)
        except Exception:
            logger.exception("[Plan] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Plan] 后台查询异常: %s", email)
        return result
    finally:
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def _release_cancelled_check(future, account_id: int, run_id: str | None = None) -> None:
    # 被取消且尚未执行的 Future 不会进入 worker 的 finally，必须单独归还占用。
    if not future.cancelled():
        return
    result = {"ok": False, "status": "failed", "error": "任务在开始执行前被取消",
              "checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        db.update_account_plan_check(acc_id=account_id, result=result, expected_run_id=run_id)
    except Exception:
        logger.exception("写入取消状态失败: account_id=%s", account_id)
    finally:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()


def _claim_plan_slot(account_id: int, trigger: str, *, run_id: str) -> dict | None:
    """同一占用逻辑用于手动队列与查活会话内同步查询。"""
    with _LOCK:
        if account_id in _RUNNING:
            return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}
        _RUNNING.add(account_id)
    if not _QUEUE_SLOTS.acquire(blocking=False):
        with _LOCK:
            _RUNNING.discard(account_id)
        return {"accepted": False, "busy": False, "queue_full": True, "error": "套餐查询队列已满，请稍后重试"}

    try:
        claimed = db.claim_account_plan_check(acc_id=account_id, trigger=trigger, run_id=run_id)
    except Exception:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        raise
    if not claimed:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询套餐"}

    return None


def check_account_plan_in_session(*, account_id: int, email: str, access_token: str, session) -> dict:
    """直接在查活线程内执行，不能提交到同一个阶段池后等待（避免线程池死锁）。"""
    account_id = int(account_id)
    if session is None:
        return {"ok": False, "error": "查活会话不存在，未查询套餐"}
    if not str(access_token or "").strip():
        return {"ok": False, "error": "账号缺少 access_token"}
    run_id = uuid.uuid4().hex
    rejected = _claim_plan_slot(account_id, "live_auto", run_id=run_id)
    if rejected is not None:
        return {"ok": False, **rejected}
    return _run_plan_check(account_id=account_id, email=email, access_token=access_token,
                           trigger="live_auto", proxy=None, timezone_offset_min="-", session=session, run_id=run_id)


def enqueue_account_plan_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
    return_future: bool = False,
    executor: ThreadPoolExecutor | None = None,
) -> dict:
    """把查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    run_id = uuid.uuid4().hex
    rejected = _claim_plan_slot(account_id, trigger, run_id=run_id)
    if rejected is not None:
        return rejected

    try:
        future = (executor or _EXECUTOR).submit(
            _run_plan_check,
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
            run_id=run_id,
        )
        future.add_done_callback(lambda done: _release_cancelled_check(done, account_id, run_id))
    except Exception as exc:
        with _LOCK:
            _RUNNING.discard(account_id)
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"套餐查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_plan_check(acc_id=account_id, result=result, expected_run_id=run_id)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        **({"future": future} if return_future else {}),
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("PLAN_CHECK_MIN_INTERVAL", 0.4, 0.0, 30.0),
        "jitter": _float_setting("PLAN_CHECK_JITTER", 0.3, 0.0, 30.0),
    }


# ---------------------------------------------------------------------------
# 全量套餐查询：把库里所有带 access_token 的账号依次塞进套餐队列。
# 队列有容量上限，这里用后台线程按剩余名额补位；每轮用独立线程池承载调用方
# 指定的并发数，不挤占手动单账号查询共用的线程池。
# ---------------------------------------------------------------------------

_BULK_LOCK = threading.Lock()
_BULK_STOP = threading.Event()
_BULK_THREAD: threading.Thread | None = None
_BULK_FEED_DONE = False
_BULK_MAX_WORKERS = 16
_BULK_STATE: dict = {
    "running": False,
    "total": 0,
    "fed": 0,
    "completed": 0,
    "queued": 0,
    "skipped": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "trigger": None,
    "workers": 0,
}
_BULK_QUEUE_WAIT_SECONDS = 0.5
_BULK_QUEUE_MAX_WAIT_ROUNDS = 600  # 单个账号最多等 5 分钟空位


def _bulk_workers(value: object) -> int:
    if value is None or value == "":
        return _WORKERS
    if isinstance(value, bool):
        raise ValueError(f"并发线程数必须为 1–{_BULK_MAX_WORKERS} 的整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"并发线程数必须为 1–{_BULK_MAX_WORKERS} 的整数")
    try:
        workers = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"并发线程数必须为 1–{_BULK_MAX_WORKERS} 的整数") from None
    if not 1 <= workers <= _BULK_MAX_WORKERS:
        raise ValueError(f"并发线程数必须为 1–{_BULK_MAX_WORKERS} 的整数")
    return workers


def _bulk_snapshot() -> dict:
    with _BULK_LOCK:
        snapshot = dict(_BULK_STATE)
    # 对外口径：已处理 = 已执行完 + 跳过 + 失败（未提交的账号也算处理过）
    snapshot["processed"] = (
        int(snapshot.get("completed") or 0)
        + int(snapshot.get("skipped") or 0)
        + int(snapshot.get("failed") or 0)
    )
    return snapshot


def _bulk_bump(field: str) -> None:
    with _BULK_LOCK:
        _BULK_STATE[field] = int(_BULK_STATE.get(field) or 0) + 1


def _bulk_mark_done_locked() -> None:
    """喂完且所有已提交任务都结束时，才把整轮标记为完成。"""
    if not _BULK_FEED_DONE:
        return
    if int(_BULK_STATE.get("completed") or 0) < int(_BULK_STATE.get("fed") or 0):
        return
    if _BULK_STATE["running"]:
        _BULK_STATE["running"] = False
    if not _BULK_STATE["finished_at"]:
        _BULK_STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _bulk_task_done(_future=None) -> None:
    with _BULK_LOCK:
        _BULK_STATE["completed"] = int(_BULK_STATE.get("completed") or 0) + 1
        _bulk_mark_done_locked()


def bulk_check_status() -> dict:
    """返回全量套餐查询的进度快照（processed 为已完成数）。"""
    return _bulk_snapshot()


def _bulk_worker(ids: list[int], trigger: str, pool: ThreadPoolExecutor) -> None:
    global _BULK_THREAD, _BULK_FEED_DONE
    try:
        for account_id in ids:
            if _BULK_STOP.is_set():
                break
            row = None
            try:
                row = db.get_account(int(account_id))
            except Exception:
                logger.exception("[Plan] 全量查询读取账号失败: account_id=%s", account_id)
            if not row or not str(row.get("access_token") or "").strip():
                _bulk_bump("failed")
                continue

            wait_rounds = 0
            try:
                while True:
                    if _BULK_STOP.is_set():
                        break
                    queued = enqueue_account_plan_check(
                        account_id=int(account_id),
                        email=str(row.get("email") or ""),
                        access_token=str(row.get("access_token") or ""),
                        trigger=trigger,
                        executor=pool,
                        return_future=True,
                    )
                    if queued.get("accepted"):
                        _bulk_bump("queued")
                        _bulk_bump("fed")
                        future = queued.get("future")
                        if future is None:
                            _bulk_task_done()
                        else:
                            future.add_done_callback(_bulk_task_done)
                        break
                    if queued.get("busy"):
                        _bulk_bump("skipped")
                        break
                    if queued.get("queue_full"):
                        wait_rounds += 1
                        if wait_rounds >= _BULK_QUEUE_MAX_WAIT_ROUNDS:
                            _bulk_bump("failed")
                            break
                        time.sleep(_BULK_QUEUE_WAIT_SECONDS)
                        continue
                    _bulk_bump("failed")
                    break
            except Exception:
                # 单个账号入队异常不能中断整轮全量查询。
                logger.exception("[Plan] 全量查询入队异常: account_id=%s", account_id)
                _bulk_bump("failed")
    finally:
        with _BULK_LOCK:
            _BULK_FEED_DONE = True
            _bulk_mark_done_locked()
            snapshot = dict(_BULK_STATE)
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass
        _BULK_THREAD = None
        logger.info(
            "[Plan] 全量查询已全部入队: total=%s fed=%s skipped=%s failed=%s workers=%s",
            snapshot.get("total"), snapshot.get("fed"),
            snapshot.get("skipped"), snapshot.get("failed"), snapshot.get("workers"),
        )


def start_bulk_all_check(
    *,
    archived: bool = False,
    trigger: str = "manual_all",
    workers: object = None,
) -> dict:
    """对库里全部账号启动一次套餐查询；已在运行时直接返回当前进度。"""
    global _BULK_THREAD, _BULK_FEED_DONE
    worker_count = _bulk_workers(workers)
    with _BULK_LOCK:
        if _BULK_STATE.get("running"):
            return {"started": False, "already_running": True, **_BULK_STATE}

    rows = db.list_accounts(limit=100000, archived=archived)
    ids = [
        int(row["id"]) for row in rows
        if row.get("id") is not None and str(row.get("access_token") or "").strip()
    ]
    pool = ThreadPoolExecutor(max_workers=max(1, worker_count), thread_name_prefix="plan-check-all")
    _BULK_STOP.clear()
    with _BULK_LOCK:
        _BULK_FEED_DONE = False
        _BULK_STATE.update({
            "running": True,
            "total": len(ids),
            "fed": 0,
            "completed": 0,
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "trigger": trigger,
            "workers": worker_count,
        })
        thread = threading.Thread(
            target=_bulk_worker, args=(ids, trigger, pool), name="plan-check-bulk", daemon=True,
        )
        _BULK_THREAD = thread
        thread.start()
        snapshot = dict(_BULK_STATE)
    return {"started": True, "already_running": False, **snapshot}
