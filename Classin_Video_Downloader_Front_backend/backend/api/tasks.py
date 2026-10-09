"""下载任务路由：CRUD + 启停控制 + 增量日志轮询。

设计要点：
- 所有操作端点返回最新 TaskOut：前端点完按钮即可直接刷新视图，
  不用再发一次 GET（少一次往返，也避免前端拿到旧状态）。
- 启停用 POST 而非 PUT：start/pause/resume/stop 是"动作"不是"资源修改"，
  语义上更贴近 RPC，也让反向代理/网关能按 POST 统一做审计。
- 路由只做参数透传 + 权限依赖，业务规则（状态机校验）全在 TaskService，
  这样同一个状态机既能被 HTTP 调用，将来也能被定时任务/CLI 复用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db.models import User
from ..schemas import MsgOut, PageOut, TaskCreateIn, TaskDetailOut, TaskLogsPageOut, TaskOut
from ..services.task_service import TaskService
from .deps import current_user, db_session, pagination

router = APIRouter(prefix="/api/tasks", tags=["任务"])


@router.get("", response_model=PageOut)
def list_tasks(
    status: str = "",
    paging: tuple[int, int] = Depends(pagination),
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """任务列表（支持状态筛选 + 分页）。

    status 传空表示全部；这里不做枚举校验——非法值只会匹配不到行，
    返回空列表比 422 更宽松，方便前端下拉框传占位值。
    """
    page, page_size = paging
    total, rows = TaskService(db).list(page, page_size, status=status)
    return PageOut(total=total, page=page, page_size=page_size, items=[TaskOut.model_validate(r) for r in rows])


@router.post("", response_model=TaskOut, status_code=201)
def create_task(payload: TaskCreateIn, user: User = Depends(current_user), db: Session = Depends(db_session)):
    """创建任务：lesson_ids=null 表示"全部课堂"（快照进 items）。

    created_by 记录创建人：多人共用一套账号体系时，任务归属可追溯，
    也为将来"只看自己的任务"预留了过滤字段。
    """
    return TaskOut.model_validate(TaskService(db).create(payload, user_id=user.id))


@router.get("/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """任务详情（含课堂明细）。前端进度条每 2s 轮询此端点。"""
    return TaskService(db).get_detail(task_id)


@router.delete("/{task_id}", response_model=MsgOut)
def delete_task(task_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """删除任务（含 items/logs 级联）。运行中任务被拒（409），需先停止。"""
    TaskService(db).delete(task_id)
    return MsgOut(message="已删除")


@router.post("/{task_id}/start", response_model=TaskOut)
def start_task(task_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """启动任务：会做 Cookie/目录/并发闸门三重业务校验（失败返回 409/422）。"""
    return TaskOut.model_validate(TaskService(db).start(task_id))


@router.post("/{task_id}/pause", response_model=TaskOut)
def pause_task(task_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """暂停：仅 running 可暂停；已在传输的分片会完成再停（引擎语义）。"""
    return TaskOut.model_validate(TaskService(db).pause(task_id))


@router.post("/{task_id}/resume", response_model=TaskOut)
def resume_task(task_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """恢复：仅 paused 可恢复（对 running 调用返回 409，防止重复点击错乱）。"""
    return TaskOut.model_validate(TaskService(db).resume(task_id))


@router.post("/{task_id}/stop", response_model=TaskOut)
def stop_task(task_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """停止：置 stopping 后由 worker 收敛到 cancelled（异步终态，见 _finalize）。"""
    return TaskOut.model_validate(TaskService(db).stop(task_id))


@router.get("/{task_id}/logs", response_model=TaskLogsPageOut)
def task_logs(
    task_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """增量日志：前端只传上次收到的 last_id，避免全量拉取大日志表。

    Why after_id 而非页码：日志只追加不修改，游标式分页天然无重复/漏读，
    且 id 单调递增可用索引快速定位（OFFSET 分页越往后越慢）。
    """
    return TaskService(db).get_logs(task_id, after_id, limit)
