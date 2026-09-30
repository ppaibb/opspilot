"""pytest 全局配置：让 PostgreSQL 专属类型能在 SQLite 测试库上编译。

多个测试模块共用同一个 Base.metadata；只要有一个模块导入带 JSONB 列的模型，
后续模块在 SQLite 上 create_all 就会因缺 visit_JSONB 而失败。这里给 sqlite 方言
补上 JSONB -> JSON 的编译规则，使整套测试可以在同一个进程里跑完。
生产使用 PostgreSQL，不受影响。
"""
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_, compiler, **kw):  # noqa: ANN001
    return compiler.visit_JSON(type_, **kw)
