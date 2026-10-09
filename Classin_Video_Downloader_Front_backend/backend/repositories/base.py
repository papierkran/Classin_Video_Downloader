"""仓储层：泛型 CRUD 基类 + 各领域薄子类。

Why Repository：service 里直接堆 ORM 查询会让"业务规则"和"SQL 细节"
纠缠（违反 SRP）。统一收口后，换存储（如迁 PostgreSQL）只动这一层。
泛型基类 T承载实体类型，子类几乎零代码，避免重复的 get/list/delete。
"""
from __future__ import annotations

from typing import Generic, Type, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..db.models import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """通用仓储：只做"怎么取"，不做"该不该"（业务判断归 service）。

    方法刻意保持"哑"：没有 try/except，没有状态检查——异常由
    service 捕获或转换成 BizError。Why：仓储层吞异常会让业务层
    拿不到"为什么失败"，最终只能靠猜。
    """

    model: Type[T]

    def __init__(self, db: Session):
        # Why 存 Session 而非自己建：事务生命周期归 service 统一控制，
        # 仓储只借用当前事务——否则一个 service 动作会触发多次隐式提交
        self.db = db

    def get(self, entity_id: int) -> T | None:
        """按主键取单条。None 由 service 翻译成 NotFoundError
        （仓储不认识 HTTP 语义，404 是 API 层的事）。"""
        return self.db.get(self.model, entity_id)

    def list_stmt(self) -> Select:
        """返回基础查询语句，子类可叠加过滤条件（模板方法模式）。"""
        return select(self.model)

    def count(self, stmt: Select | None = None) -> int:
        stmt = stmt if stmt is not None else self.list_stmt()
        # 子查询包一层：COUNT 不能直接数带 JOIN/多列的 select
        # Why or 0：scalar 在空结果时返回 None，None 回到前端会让 total 变 null
        return self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    def page(self, stmt: Select, page: int, page_size: int) -> list[T]:
        """物理分页。Why 在 SQL 层做：几千行数据全查回内存是浪费。
        Why 转 list()：scalars 返回惰性结果，session 关闭后再迭代会炸。"""
        return list(self.db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all())

    def add(self, entity: T) -> T:
        self.db.add(entity)
        # flush 只发 INSERT 不提交：service 还可能在同一事务里做别的事
        # （如算 id 再插 TaskItem），flush 拿到自增 id 而不关事务
        self.db.flush()
        return entity

    def delete(self, entity: T) -> None:
        self.db.delete(entity)  # 级联（items/logs）由模型 relationship 配置，仓储不关心

    def commit(self) -> None:
        """提交事务。Why 由 service 显式调用而非 add/delete 自动提交：
        一个业务动作可能改多行，中途提交会留下半成品数据。"""
        self.db.commit()

    def rollback(self) -> None:
        """回滚。service 在捕获异常后调用，防止脏数据随 finally 提交。"""
        self.db.rollback()
