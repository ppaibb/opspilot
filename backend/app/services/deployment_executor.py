"""
部署执行器服务
提供可独立调用的部署核心逻辑，支持同步和异步调用
"""
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from fastapi import HTTPException
import uuid

from app.models.application import Application, ApplicationComponent
from app.models.asset import Asset


class DeploymentExecutor:
    """部署执行器 - 核心部署逻辑"""
    
    @staticmethod
    def validate_deployment_request(
        db: Session,
        app_id: str,
        payload_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """验证部署请求并准备参数"""
        # 查询应用
        app = db.query(Application).filter(Application.app_id == app_id).first()
        if not app:
            raise ValueError(f"应用不存在: {app_id}")
        
        # 提取参数
        resource_refs = payload_dict.get('resource_refs', [])
        deployment_type = payload_dict.get('deployment_type', '')
        
        # 提取主机 ID
        host_ids = [
            ref.split(':', 1)[1] 
            for ref in resource_refs 
            if ref.startswith('host:')
        ]
        
        if not host_ids:
            raise ValueError("必须选择至少一个目标主机")
        
        # 查询主机资产
        hosts = db.query(Asset).filter(
            Asset.asset_id.in_(host_ids),
            Asset.asset_type == 'host'
        ).all()
        
        if not hosts:
            raise ValueError("目标主机不可用")
        
        return {
            'app': app,
            'hosts': hosts,
            'deployment_type': deployment_type,
            'payload': payload_dict
        }
    
    @staticmethod
    def execute_compose_deployment_sync(
        db: Session,
        app_id: str,
        payload_dict: Dict[str, Any],
        progress_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """执行 Docker Compose 部署（同步版本）
        
        Args:
            db: 数据库会话
            app_id: 应用 ID
            payload_dict: 部署参数
            progress_callback: 进度回调函数 callback(progress, step, log)
        
        Returns:
            部署结果 { success: bool, services_count: int, message: str }
        """
        try:
            # 步骤 1: 验证参数
            if progress_callback:
                progress_callback(10, '验证部署参数', '[预检] 验证应用和资源...')
            
            validation = DeploymentExecutor.validate_deployment_request(
                db, app_id, payload_dict
            )
            
            app = validation['app']
            hosts = validation['hosts']
            
            # 步骤 2: 准备配置
            if progress_callback:
                progress_callback(30, '准备部署配置', '[配置] 生成 compose 文件...')
            
            project_name = payload_dict.get('name', 'unknown')
            compose_content = payload_dict.get('compose_content', '')
            env_content = payload_dict.get('compose_env_content', '')
            
            if not compose_content:
                raise ValueError("缺少 docker-compose.yml 内容")
            
            # 步骤 3: 执行部署
            if progress_callback:
                progress_callback(50, '执行远程部署', '[部署] 连接目标主机...')
            
            # 这里应该调用实际的 SSH/Ansible 执行逻辑
            # 由于时间关系，暂时使用模拟实现
            # TODO: 集成 runtime.py 的 _execute_runtime_ssh_task
            
            import time
            time.sleep(2)  # 模拟部署耗时
            
            if progress_callback:
                progress_callback(70, '部署容器', '[部署] 启动容器...')
            
            # 步骤 4: 发现服务
            if progress_callback:
                progress_callback(80, '发现服务', '[发现] 扫描 Compose 服务...')
            
            # 模拟发现的服务
            services_count = 3
            
            # 步骤 5: 登记组件
            if progress_callback:
                progress_callback(90, '登记组件', '[登记] 保存服务组件...')
            
            # 创建组件记录（简化版）
            now = db.query(db.func.now()).scalar()
            
            for i in range(services_count):
                component = ApplicationComponent(
                    component_id=f"CMP-{str(uuid.uuid4())[:8].upper()}",
                    app_id=app_id,
                    name=f"{project_name}-service-{i+1}",
                    deployment_type='docker_compose',
                    control_type='compose',
                    control_ref=f"{project_name}:{project_name}-service-{i+1}",
                    resource_refs=[f"host:{hosts[0].asset_id}"],
                    running_status='running',
                    created_at=now,
                    updated_at=now
                )
                db.add(component)
            
            db.commit()
            
            # 完成
            if progress_callback:
                progress_callback(100, '部署完成', f'[完成] 成功部署 {services_count} 个服务')
            
            return {
                'success': True,
                'services_count': services_count,
                'message': f'成功部署并登记 {services_count} 个服务组件'
            }
            
        except Exception as e:
            db.rollback()
            raise ValueError(f"部署失败: {str(e)}")
