"""
应用部署任务 API 路由
提供任务查询和状态跟踪接口
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any

from app.db.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.deployment_task_service import DeploymentTaskService

router = APIRouter()


@router.get("/tasks/{task_id}")
def get_deployment_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取部署任务状态"""
    task = DeploymentTaskService.get_task_status(db, task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return task


@router.get("/{app_id}/tasks")
def list_app_deployment_tasks(
    app_id: str,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取应用的部署任务列表"""
    tasks = DeploymentTaskService.list_app_tasks(db, app_id, limit)
    return tasks
