"""课堂数据路由：CRUD + 文件导入。

课堂是本系统的"数据基座"——任务创建、进度回写都以 lesson 为锚点，
所以导入/增删改的可用性直接决定整体体验：
- 导入支持 CSV 与 XLSX（ClassIn 后台两种导出格式都常见）；
- 删除课堂不会连带删历史任务（任务里存了快照，见 TaskItem 设计）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from ..core.exceptions import ValidationFailure
from ..db.models import User
from ..schemas import ImportResultOut, LessonBatchDeleteIn, LessonIn, LessonOut, LessonUpdateIn, MsgOut, PageOut
from ..services.lesson_service import LessonService, parse_csv, parse_xlsx
from .deps import current_user, db_session, pagination

router = APIRouter(prefix="/api/lessons", tags=["课堂"])

# 上传大小上限 20MB：ClassIn 导出表通常 <1MB，上限防内存型 DoS（公网部署必须）
MAX_IMPORT_BYTES = 20 * 1024 * 1024


@router.get("", response_model=PageOut)
def list_lessons(
    status: str = "",
    keyword: str = "",
    paging: tuple[int, int] = Depends(pagination),
    _: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    """课堂列表：状态精确筛选 + 名称/ID 模糊搜索 + 分页。

    keyword 同时匹配 lesson_name/class_name/lesson_id——用户手里可能只有
    平台导出的 ID，也可能只记得中文名，一个框全兼容比两个框更省心。
    """
    page, page_size = paging
    total, rows = LessonService(db).list(page, page_size, status=status, keyword=keyword)
    return PageOut(total=total, page=page, page_size=page_size, items=[LessonOut.model_validate(r) for r in rows])


@router.post("", response_model=LessonOut, status_code=201)
def create_lesson(payload: LessonIn, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """手工新增单个课堂（lesson_id 重复返回 409，由 service 抛出）。"""
    return LessonOut.model_validate(LessonService(db).create(payload))


@router.put("/{lesson_id}", response_model=LessonOut)
def update_lesson(lesson_id: int, payload: LessonUpdateIn, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """编辑课堂名称/时间（lesson_id 不可改：它是平台侧主键，改了等于换记录）。"""
    return LessonOut.model_validate(LessonService(db).update(lesson_id, payload))


@router.delete("/{lesson_id}", response_model=MsgOut)
def delete_lesson(lesson_id: int, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """删除单个课堂。历史任务不受影响（TaskItem 快照设计）。"""
    LessonService(db).delete(lesson_id)
    return MsgOut(message="已删除")


@router.post("/batch-delete", response_model=MsgOut)
def batch_delete(payload: LessonBatchDeleteIn, _: User = Depends(current_user), db: Session = Depends(db_session)):
    """批量删除（表格多选后的"删除所选"）。单事务提交，避免逐条 commit。"""
    count = LessonService(db).batch_delete(payload.ids)
    return MsgOut(message=f"已删除 {count} 条")


@router.post("/import", response_model=ImportResultOut)
async def import_file(file: UploadFile = File(...), _: User = Depends(current_user), db: Session = Depends(db_session)):
    """导入 CSV/XLSX（multipart）。Why async + 手动 read：需要先量大小再读，
    直接 UploadFile.read() 无上限，大文件会打爆内存——公网部署的低成本防线。

    按文件扩展名分流解析；重复导入按 lesson_id upsert（幂等），
    用户可以放心多次导入同一批导出文件。
    """
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise ValidationFailure("文件超过 20MB 限制")
    if not content:
        raise ValidationFailure("文件为空")

    name = (file.filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        rows = parse_xlsx(content)
    elif name.endswith(".csv") or (file.content_type or "").startswith("text"):
        rows = parse_csv(content)
    else:
        raise ValidationFailure("仅支持 .csv / .xlsx 文件")
    return LessonService(db).import_rows(rows)
