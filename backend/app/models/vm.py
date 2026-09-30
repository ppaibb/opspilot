"""
虚拟机资产模型
"""
from sqlalchemy import Column, Integer, String, Float
from app.db.database import Base


class VirtualMachine(Base):
    __tablename__ = "virtual_machines"

    id = Column(Integer, primary_key=True, index=True)
    vm_id = Column(String(64), unique=True, index=True, comment="平台侧唯一ID，如 pve-100")
    name = Column(String(120), comment="虚拟机名称")
    provider = Column(String(40), default="proxmox", comment="虚拟化平台标识")
    node = Column(String(80), default="", comment="所属宿主节点")
    status = Column(String(40), default="stopped", comment="running/stopped/error")
    ip = Column(String(80), default="", comment="业务IP")
    os = Column(String(80), default="", comment="系统类型")
    cpu_cores = Column(Float, default=0.0, comment="CPU核数")
    memory_gb = Column(Float, default=0.0, comment="内存GB")
    system_disk_gb = Column(Float, default=0.0, comment="系统盘GB")
    disks_json = Column(String(2000), default="[]", comment="磁盘明细JSON，仅保存磁盘名和容量")
    description = Column(String(300), default="", comment="描述信息")
