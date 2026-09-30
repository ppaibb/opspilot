"""
应用部署任务后台服务
负责异步执行 Compose 部署任务
"""
import threading
import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.deployment_task import ApplicationDeploymentTask
from app.services.deployment_executor import DeploymentExecutor


def create_notification(db: Session, user_id: str, title: str, content: str, notification_type: str, link: str = None):
    """创建站内通知（临时实现，等待后端通知接口）"""
    # TODO: 接入真实的站内通知 API
    pass


class DeploymentTaskService:
    """部署任务后台服务"""
    
    @staticmethod
    def create_task(
        db: Session,
        app_id: str,
        task_type: str,
        deployment_config: Dict[str, Any],
        created_by: str
    ) -> str:
        """创建部署任务"""
        task_id = str(uuid.uuid4())
        
        task = ApplicationDeploymentTask(
            task_id=task_id,
            app_id=app_id,
            task_type=task_type,
            status='pending',
            progress=0,
            deployment_config=deployment_config,
            created_by=created_by,
            created_at=datetime.utcnow()
        )
        
        db.add(task)
        db.commit()
        
        return task_id
    
    @staticmethod
    def update_task_progress(
        task_id: str,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        current_step: Optional[str] = None,
        result_message: Optional[str] = None,
        output_log: Optional[str] = None
    ):
        """更新任务进度（独立会话）"""
        db = SessionLocal()
        try:
            task = db.query(ApplicationDeploymentTask).filter(
                ApplicationDeploymentTask.task_id == task_id
            ).first()
            
            if not task:
                return
            
            if status is not None:
                task.status = status
                if status == 'running' and not task.started_at:
                    task.started_at = datetime.utcnow()
                elif status in ('success', 'failed', 'cancelled'):
                    task.finished_at = datetime.utcnow()
            
            if progress is not None:
                task.progress = progress
            
            if current_step is not None:
                task.current_step = current_step
            
            if result_message is not None:
                task.result_message = result_message
            
            if output_log is not None:
                task.output_log = (task.output_log or '') + output_log + '\n'
            
            db.commit()
        finally:
            db.close()
    
    @staticmethod
    def execute_compose_deployment(
        task_id: str,
        app_id: str,
        deployment_config: Dict[str, Any],
        created_by: str
    ):
        """执行 Compose 部署（后台线程）"""
        db = SessionLocal()
        try:
            # 定义内部更新函数，复用当前 db 会话
            def update_progress(status=None, progress=None, current_step=None, result_message=None, output_log=None):
                task = db.query(ApplicationDeploymentTask).filter(
                    ApplicationDeploymentTask.task_id == task_id
                ).first()
                if not task:
                    return
                if status is not None:
                    task.status = status
                    if status == 'running' and not task.started_at:
                        task.started_at = datetime.utcnow()
                    elif status in ('success', 'failed', 'cancelled'):
                        task.finished_at = datetime.utcnow()
                if progress is not None:
                    task.progress = progress
                if current_step is not None:
                    task.current_step = current_step
                if result_message is not None:
                    task.result_message = result_message
                if output_log is not None:
                    task.output_log = (task.output_log or '') + output_log + '\n'
                db.commit()
            
            # 更新状态为运行中
            update_progress(
                status='running',
                progress=5,
                current_step='准备部署环境'
            )
            
            # 从配置中提取参数
            payload_dict = deployment_config.get('payload', {})
            app_name = payload_dict.get('name', 'unknown')
            
            # 定义进度回调函数
            def progress_callback(progress: int, step: str, log: str):
                update_progress(
                    progress=progress,
                    current_step=step,
                    output_log=log
                )
            
            # 调用部署执行器
            result = DeploymentExecutor.execute_compose_deployment_sync(
                db=db,
                app_id=app_id,
                payload_dict=payload_dict,
                progress_callback=progress_callback
            )
            
            # 部署成功
            services_count = result.get('services_count', 0)
            update_progress(
                status='success',
                progress=100,
                current_step='部署完成',
                result_message=result.get('message', '部署成功'),
                output_log=f'[完成] {result.get("message")}'
            )
            
            # 发送成功通知
            create_notification(
                db=db,
                user_id=created_by,
                title='部署成功',
                content=f'应用 {app_name} 部署完成，已自动发现并登记 {services_count} 个服务',
                notification_type='success',
                link=f'/apps/{app_id}'
            )
            
        except Exception as e:
            # 部署失败
            import traceback
            error_message = str(e)
            error_trace = traceback.format_exc()
            
            update_progress(
                status='failed',
                result_message=f'部署失败: {error_message}',
                output_log=f'[错误] {error_message}\n\n详细信息:\n{error_trace}'
            )
            
            # 发送失败通知
            create_notification(
                db=db,
                user_id=created_by,
                title='部署失败',
                content=f'应用部署失败: {error_message[:100]}',
                notification_type='error',
                link=f'/apps/{app_id}'
            )
        
        finally:
            db.close()
    
    @classmethod
    def start_deployment_task(
        cls,
        db: Session,
        app_id: str,
        task_type: str,
        deployment_config: Dict[str, Any],
        created_by: str
    ) -> str:
        """启动部署任务（创建任务并启动后台线程）"""
        # 创建任务记录
        task_id = cls.create_task(db, app_id, task_type, deployment_config, created_by)
        
        # 启动后台线程执行
        thread = threading.Thread(
            target=cls.execute_compose_deployment,
            args=(task_id, app_id, deployment_config, created_by),
            daemon=True
        )
        thread.start()
        
        return task_id
    
    @staticmethod
    def get_task_status(db: Session, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        task = db.query(ApplicationDeploymentTask).filter(
            ApplicationDeploymentTask.task_id == task_id
        ).first()
        
        if not task:
            return None
        
        return task.to_dict()
    
    @staticmethod
    def list_app_tasks(db: Session, app_id: str, limit: int = 10) -> list:
        """获取应用的部署任务列表"""
        tasks = db.query(ApplicationDeploymentTask).filter(
            ApplicationDeploymentTask.app_id == app_id
        ).order_by(
            ApplicationDeploymentTask.created_at.desc()
        ).limit(limit).all()
        
        return [task.to_dict() for task in tasks]
