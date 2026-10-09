"""课堂服务：CRUD + CSV/XLSX 批量导入（按 lesson_id upsert 去重）。

导入是核心入口：用户从 ClassIn 后台导出统计表（CSV 或 XLSX），
列名是中文（班级ID/课堂ID/...），必须兼容 utf-8-sig 与 gbk 两种编码。

解析与入库分层的理由（单一职责）：
- parse_* 只管"字节 → 行字典"，可独立单测、可被 CLI 复用；
- import_rows 只管"行字典 → 数据库 upsert"，不关心文件格式。
"""
from __future__ import annotations

import csv
import io
import logging

from openpyxl import load_workbook
from sqlalchemy.orm import Session

from ..core.exceptions import ConflictError, NotFoundError, ValidationFailure
from ..db.models import Lesson
from ..repositories import LessonRepo
from ..schemas import ImportResultOut, LessonIn, LessonOut, LessonUpdateIn

logger = logging.getLogger("app.lessons")

# ClassIn 导出表的标准列 → 模型字段映射（列顺序不固定，按表头名取）
CSV_COLUMNS = {
    "班级ID": "class_id",
    "课堂ID": "lesson_id",
    "课堂名称": "lesson_name",
    "班级名称": "class_name",
    "开课时间": "start_time",
    "创建时间": "creation_time",
}


def _normalize_rows(rows: list[dict]) -> list[dict]:
    """统一表头（去空白/BOM）并过滤无 lesson_id 的行。"""
    cleaned = []
    for row in rows:
        item = {(str(k) or "").strip().lstrip("\ufeff"): (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v)) for k, v in row.items()}
        lesson_id = item.get("课堂ID", "")
        if not lesson_id:
            continue  # Why 跳过：平台导出的表常见空行/汇总行，无课堂ID 无法下载
        cleaned.append({field: item.get(col, "") for col, field in CSV_COLUMNS.items()})
    return cleaned


def parse_csv(content: bytes) -> list[dict]:
    """解析 CSV，兼容 utf-8-sig（Excel 导出）与 gbk（中文 Windows 默认）。

    Why 按序尝试而非探测 BOM：很多导出文件既无 BOM 也非纯 ASCII，
    "试解码"比"看前缀"覆盖面更广；utf-8 排最后是因为它几乎能解一切，
    放前面会把 gbk 文件解成乱码而不报错（mojibake 比 UnicodeDecodeError
    危险得多——乱码数据会静默入库）。
    """
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = content.decode(encoding)
            raw_rows = list(csv.reader(io.StringIO(text)))
            # ClassIn 机构统计导出会在表头前写入标题、总数和导出时间。
            # 不假设表头固定在第 1 行，兼容标准 CSV 和该统计导出格式。
            header_index = next(
                (i for i, row in enumerate(raw_rows) if "课堂ID" in [str(cell).strip().lstrip("\ufeff") for cell in row]),
                None,
            )
            if header_index is None:
                continue
            headers = [str(cell).strip().lstrip("\ufeff") for cell in raw_rows[header_index]]
            rows = [dict(zip(headers, row)) for row in raw_rows[header_index + 1 :]]
            return _normalize_rows(rows)
        except UnicodeDecodeError:
            continue
    raise ValidationFailure("无法识别 CSV 编码或表头（需包含“课堂ID”列，支持 UTF-8/GBK）")


def parse_xlsx(content: bytes) -> list[dict]:
    """解析 XLSX：openpyxl 只读模式，首行当表头。

    Why data_only=True：用户表里若有公式列（如 =B2-C2），data_only
    取的是缓存计算值；取公式原文会把 "=..." 当 ID 入库成脏数据。
    """
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise ValidationFailure(f"XLSX 文件无法打开: {e}") from e
    try:
        ws = wb.active
        if ws is None:
            raise ValidationFailure("XLSX 中没有任何工作表")
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()  # Why finally：read_only 模式持有 zip 句柄，不关会占文件锁
    if not rows:
        return []
    header_index = next(
        (i for i, row in enumerate(rows) if any(str(cell or "").strip().lstrip("\ufeff") == "课堂ID" for cell in row)),
        None,
    )
    if header_index is None:
        raise ValidationFailure("XLSX 中未找到包含“课堂ID”的表头")
    headers = [str(h).strip().lstrip("\ufeff") if h is not None else "" for h in rows[header_index]]
    return _normalize_rows([dict(zip(headers, r)) for r in rows[header_index + 1 :]])


class LessonService:
    """课堂领域服务：所有写操作统一 commit（事务边界在 service 而非 repo）。

    Why 事务归 service：一次业务动作（如 batch_delete）可能触碰多张表/
    多行，只有 service 层知道"动作何时完成"；repo 只负责拼 SQL。
    这样做还有个实际好处：动作失败时 service 可以先做"回滚+业务化
    错误"（如"课堂被任务引用"）再抛给用户，仓储层做不到这种翻译。
    """

    def __init__(self, db: Session):
        self.db = db
        self.repo = LessonRepo(db)

    def list(self, page: int, page_size: int, status: str = "", keyword: str = "") -> tuple[int, list[Lesson]]:
        """查询：先 count 后取页。Why 两条 SQL 而非窗口函数：
        SQLite 的窗口函数可用但可读性差，且分页数据量小（≤200/页），代价可忽略。
        count/page 复用同一 stmt——条件一致才能保证"总数与本页数据同口径"。"""
        stmt = self.repo.filtered(status=status, keyword=keyword)
        return self.repo.count(stmt), self.repo.page(stmt, page, page_size)

    def create(self, payload: LessonIn) -> Lesson:
        """新增课堂（lesson_id 唯一约束 + 前置检查双保险：
        前置检查给出可读错误，约束兜底并发下的竞态——
        两个导入请求同时提交时只有前置检查会双双通过，
        最终靠 DB 约束拦下后者并抛 IntegrityError）。"""
        if self.repo.get_by_lesson_id(payload.lesson_id):
            raise ConflictError(f"课堂 {payload.lesson_id} 已存在")
        lesson = self.repo.add(Lesson(**payload.model_dump()))
        self.repo.commit()
        return lesson

    def update(self, lesson_id: int, payload: LessonUpdateIn) -> Lesson:
        """局部更新：exclude_unset 保证"没传的字段"不会被 None 覆盖——
        前端编辑框只改了名称就只更新名称，时间字段保持原值。"""
        lesson = self.repo.get(lesson_id)
        if not lesson:
            raise NotFoundError("课堂不存在")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(lesson, key, value)
        self.repo.commit()
        return lesson

    def delete(self, lesson_id: int) -> None:
        """删除单个课堂。历史任务不受影响：TaskItem 存了课堂快照，
        这就是为什么删除不报"任务引用中"——回看历史不该被数据清理绑架。"""
        lesson = self.repo.get(lesson_id)
        if not lesson:
            raise NotFoundError("课堂不存在")
        self.repo.delete(lesson)
        self.repo.commit()

    def batch_delete(self, ids: list[int]) -> int:
        """批量删除，返回实际删除条数。

        Why 不校验"是否存在"：不存在的 id 静默跳过是批量操作的
        合理语义（用户勾选后别人先删了一条，整个操作不该失败）；
        返回值让用户看到"删了 9 条"而非笼统成功。
        """
        lessons = self.repo.list_by_ids(ids)
        for lesson in lessons:
            self.repo.delete(lesson)
        self.repo.commit()  # 单事务：要么全删要么全不删，不留半删状态
        return len(lessons)

    def import_rows(self, rows: list[dict]) -> ImportResultOut:
        """upsert 导入：lesson_id 已存在则更新名称等字段（Why：用户会多次
        导出同一批课，重复导入不应报错，而应静默刷新）。

        `value and` 跳过空值的设计：源文件里单元格留空≠"清空该字段"，
        更可能是"这次导出没填"——用空覆盖会把已有班级名抹掉，
        这是用户最常见也最难理解的数据丢失来源。
        updated/skipped 分开计数让用户确认"刷新是否真的发生"。
        """
        if not rows:
            return ImportResultOut(total=0, created=0, updated=0, skipped=0, message="文件中没有有效课堂行")
        created = updated = skipped = 0
        for row in rows:
            existing = self.repo.get_by_lesson_id(row["lesson_id"])
            if existing:
                changed = False
                for field, value in row.items():
                    if field != "lesson_id" and value and getattr(existing, field) != value:
                        setattr(existing, field, value)
                        changed = True
                updated += int(changed)
                skipped += int(not changed)
            else:
                self.repo.add(Lesson(**row))
                created += 1
        self.db.flush()  # 新增行拿到 id 后再整体 commit，失败时一起回滚
        self.repo.commit()
        logger.info("导入完成: total=%d created=%d updated=%d skipped=%d", len(rows), created, updated, skipped)
        return ImportResultOut(total=len(rows), created=created, updated=updated, skipped=skipped, message="导入完成")

    def get_out(self, lesson_id: int) -> LessonOut:
        """转出参（含 not-found 收敛）：路由层拿到的就是可序列化对象，
        不用每个端点都写一遍"查→判空→404"。"""
        lesson = self.repo.get(lesson_id)
        if not lesson:
            raise NotFoundError("课堂不存在")
        return LessonOut.model_validate(lesson)
