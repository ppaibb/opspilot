"""华为云主机预览和本地导入；云端查询与数据库写入分离。"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.services.credential_usage import sync_credential_usage
from app.services.huawei_cloud import HuaweiCloud, HuaweiCloudError


def _same_identity(asset: Asset, host: dict) -> bool:
    """匹配云账号、项目和区域，凭证 ID 不参与身份比较，支持密钥轮换。"""
    return all(getattr(asset, field) == host[field] for field in (
        "provider", "region", "cloud_account_id", "cloud_project_id",
    ))


def sync_huawei_assets(db: Session, req, access_key: str, secret_key: str) -> dict:
    """预览绝不写库；提交时重新读取云端，校验整批实例后才原子提交。"""
    if req.instance_ids is not None and (not req.instance_ids or len(req.instance_ids) > 500):
        return {"success": False, "message": "每次请选择 1 至 500 台华为云主机"}
    try:
        scan = HuaweiCloud(access_key, secret_key).scan(req.region)
    except HuaweiCloudError as exc:
        return {"success": False, "message": str(exc)}
    rows = {host["instance_id"]: host for host in scan.instances}
    existing = {
        asset.instance_id: asset for asset in db.query(Asset).filter(Asset.instance_id.in_(rows)).all()
    } if rows else {}
    if req.instance_ids is None:
        return {
            "success": True, "preview": True, "warnings": scan.warnings,
            "instances": [dict(host, is_imported=host["instance_id"] in existing) for host in rows.values()],
        }

    selected = list(dict.fromkeys(req.instance_ids))
    missing = [server_id for server_id in selected if server_id not in rows]
    if missing:
        return {"success": False, "message": "部分所选实例已不存在或无权访问，请重新扫描后导入", "warnings": scan.warnings}
    for server_id in selected:
        host = rows[server_id]
        if server_id in existing and not _same_identity(existing[server_id], host):
            return {"success": False, "message": "实例与已有资产的厂商、账号或项目身份冲突，已停止整批导入"}
        if not host["ip"]:
            return {"success": False, "message": "所选主机缺少可用 IP 地址，请补齐云端网络配置后重试"}

    imported_count = 0
    try:
        for server_id in selected:
            host = rows[server_id]
            asset = existing.get(server_id)
            if asset is None:
                asset = Asset(
                    instance_id=server_id, type="pending", ssh_port=22, ssh_user="root",
                    ssh_password="", credential_ref="",
                )
                db.add(asset)
                imported_count += 1
            # 仅更新云元数据；SSH 凭证、登录用户、分组和人工备注均保留。
            for field in (
                "name", "ip", "private_ip", "provider", "region", "status",
                "cloud_resource_type", "cloud_project_id", "cloud_account_id",
                "os", "cpu_cores", "memory_gb",
            ):
                setattr(asset, field, host[field])
            if host["system_disk_gb"] > 0:
                asset.system_disk_gb = host["system_disk_gb"]
            asset.cloud_credential_ref = req.credential_id or ""
        db.flush()
        sync_credential_usage(db)
        db.commit()
    except IntegrityError:
        # 并发导入时唯一索引兜底，不能部分提交或回传数据库内部错误。
        db.rollback()
        return {"success": False, "message": "资产已被其他请求导入，请刷新后重试"}
    return {
        "success": True, "imported_count": imported_count, "updated_count": len(selected) - imported_count,
        "warnings": scan.warnings,
        "message": f"华为云主机导入完成：新增 {imported_count} 台，更新 {len(selected) - imported_count} 台",
    }
