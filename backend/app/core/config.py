"""
系统核心配置模块，负责加载环境变量
遵循 Google Python Style Guide
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """
    环境配置类。自动读取当前运行目录下的 .env 文件。
    """
    PROJECT_NAME: str = "OpsPilot API"

    # [安全审计要求] 必须从外部 .env 加载的敏感信息
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # 数据库配置。统一使用 PostgreSQL，连接串必须由 .env 的 DATABASE_URL 提供。
    # 不设默认值：漏配时启动即报错，避免静默回退到 SQLite 生成空库（曾导致排障误判）。
    DATABASE_URL: str
    # 自动建表开关。生产环境建议设为 false 并改用 Alembic 管理 schema。
    DB_AUTO_CREATE_TABLES: bool = True
    # PostgreSQL 连接池参数（增加以支持并发和后台任务）
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # 外部依赖
    GEMINI_API_KEY: str = ""
    OPENAI_COMPAT_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    CREDENTIAL_ENCRYPTION_KEY: str = ""

    # 首次启动管理员初始化配置。
    # 仅当用户名与密码同时提供时才会执行创建，避免误创建默认弱口令账号。
    INIT_ADMIN_USERNAME: str = ""
    INIT_ADMIN_PASSWORD: str = ""
    INIT_ADMIN_NAME: str = "系统管理员"
    INIT_ADMIN_EMAIL: str = ""
    INIT_ADMIN_ROLE: str = "管理员"
    INIT_ADMIN_DEPARTMENT: str = "基础平台部"

    # WebSSH SFTP 文件上传限制，单位 MB。
    # 该值只影响 OpsPilot 经后端代理上传到目标主机的单文件大小。
    TERMINAL_SFTP_UPLOAD_MAX_MB: int = 100

    # 登录失败限流。当前实现为单进程内存限流，生产多副本部署建议迁移到 Redis。
    LOGIN_MAX_FAILED_ATTEMPTS: int = 5
    LOGIN_FAILURE_WINDOW_SECONDS: int = 300
    LOGIN_LOCK_SECONDS: int = 300

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
