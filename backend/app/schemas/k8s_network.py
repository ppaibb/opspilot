from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

class ServicePort(BaseModel):
    name: Optional[str] = None
    port: int
    target_port: int
    node_port: Optional[int] = None
    protocol: str = "TCP"

class ServiceCreateRequest(BaseModel):
    name: str = Field(..., max_length=253)
    namespace: str = Field(..., max_length=253)
    type: str = "ClusterIP"
    selector: Dict[str, str] = Field(default_factory=dict)
    ports: List[ServicePort] = Field(..., min_items=1)
    labels: Dict[str, str] = Field(default_factory=dict)

class IngressBackendServicePort(BaseModel):
    number: int

class IngressBackendService(BaseModel):
    name: str
    port: IngressBackendServicePort

class IngressBackend(BaseModel):
    service: IngressBackendService

class IngressPath(BaseModel):
    path: str
    path_type: str = "Prefix"
    backend: IngressBackend

class IngressHttpRule(BaseModel):
    paths: List[IngressPath]

class IngressRule(BaseModel):
    host: Optional[str] = None
    http: IngressHttpRule

class IngressTLS(BaseModel):
    hosts: List[str]
    secret_name: str

class IngressCreateRequest(BaseModel):
    name: str = Field(..., max_length=253)
    namespace: str = Field(..., max_length=253)
    ingress_class_name: Optional[str] = None
    rules: List[IngressRule] = Field(..., min_items=1)
    tls: List[IngressTLS] = Field(default_factory=list)
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
