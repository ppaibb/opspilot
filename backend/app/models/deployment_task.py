"""
应用部署任务模型
用于异步部署任务的状态跟踪和进度管理
"""
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from app.db.database import Base


class ApplicationDeploymentTask(Base):
    __tablename__ = "application_deployment_tasks"
    __table_args__ = (
        Index('idx_deployment_tasks_status', 'status'),
        Index('idx_deployment_tasks_app_id', 'app_id'),
        Index('idx_deployment_tasks_created_at', 'created_at'),
        {'comment': '应用部署任务表（异步任务）'}
    )

    task_id = Column(String(36), primary_key=True, comment='任务 ID（UUID）')
    app_id = Column(String(36), ForeignKey('applications.app_id', ondelete='CASCADE'), nullable=False, comment='关联应用 ID')
    task_type = Column(String(20), nullable=False, comment='任务类型：deploy/restart/stop/start')
    status = Column(String(20), nullable=False, default='pending', comment='任务状态：pending/running/success/failed/cancelled')
    progress = Column(Integer, default=0, comment='进度百分比 0-100')
    current_step = Column(String(100), comment='当前步骤描述')
    total_steps = Column(Integer, comment='总步骤数')
    result_message = Column(Text, comment='结果消息或错误信息')
    output_log = Column(Text, comment='执行日志输出')
    deployment_config = Column(JSONB, comment='部署配置（JSON）')
    created_by = Column(String(50), comment='创建人')
    created_at = Column(DateTime, server_default=func.now(), nullable=False, comment='创建时间')
    started_at = Column(DateTime, comment='开始执行时间')
    finished_at = Column(DateTime, comment='完成时间')

    def to_dict(self):
        return {
            'task_id': self.task_id,
            'app_id': self.app_id,
            'task_type': self.task_type,
            'status': self.status,
            'progress': self.progress,
            'current_step': self.current_step,
            'total_steps': self.total_steps,
            'result_message': self.result_message,
            'output_log': self.output_log,
            'deployment_config': self.deployment_config,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }
