from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

class EnvVar(BaseModel):
    name: str
    value: str

class ContainerPort(BaseModel):
    name: Optional[str] = None
    container_port: int
    protocol: str = "TCP"

class ResourceRequirements(BaseModel):
    requests: Optional[Dict[str, str]] = None
    limits: Optional[Dict[str, str]] = None

class ContainerSpec(BaseModel):
    name: str
    image: str
    image_pull_policy: str = "IfNotPresent"
    env: List[EnvVar] = Field(default_factory=list)
    ports: List[ContainerPort] = Field(default_factory=list)
    resources: Optional[ResourceRequirements] = None
    command: List[str] = Field(default_factory=list)
    args: List[str] = Field(default_factory=list)

class WorkloadCreateRequest(BaseModel):
    name: str = Field(..., max_length=253)
    namespace: str = Field(..., max_length=253)
    labels: Dict[str, str] = Field(default_factory=dict)
    replicas: int = 1
    containers: List[ContainerSpec] = Field(..., min_items=1)
    node_selector: Dict[str, str] = Field(default_factory=dict)
    tolerations: List[Dict[str, Any]] = Field(default_factory=list)
    image_pull_secrets: List[str] = Field(default_factory=list)
    restart_policy: str = "Always"
    cron_schedule: Optional[str] = None # For CronJob only
