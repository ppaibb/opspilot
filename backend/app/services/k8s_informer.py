"""
K8s SharedInformer — 集中同步 → 本地缓存 → 扇出推送

复刻 client-go SharedInformer 架构：
1. ResourceInformer: 每 (cluster, kind) 一条 Watch 长连接，维护内存镜像
2. PubSub: 按 kind/namespace 过滤，扇出事件到 SSE 订阅者
3. InformerManager: 全局单例，管理所有 informer 生命周期
"""
import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from kubernetes import client, watch
from kubernetes.client.rest import ApiException

from app.services.k8s_summarizers import summarize_pod, summarize_workload

logger = logging.getLogger(__name__)

WATCHED_KINDS: dict[str, dict[str, str]] = {
    "Pod": {"api": "CoreV1Api", "list_all": "list_pod_for_all_namespaces"},
    "Deployment": {"api": "AppsV1Api", "list_all": "list_deployment_for_all_namespaces"},
    "StatefulSet": {"api": "AppsV1Api", "list_all": "list_stateful_set_for_all_namespaces"},
    "DaemonSet": {"api": "AppsV1Api", "list_all": "list_daemon_set_for_all_namespaces"},
    "ReplicaSet": {"api": "AppsV1Api", "list_all": "list_replica_set_for_all_namespaces"},
    "Job": {"api": "BatchV1Api", "list_all": "list_job_for_all_namespaces"},
    "CronJob": {"api": "BatchV1Api", "list_all": "list_cron_job_for_all_namespaces"},
    "Event": {"api": "CoreV1Api", "list_all": "list_event_for_all_namespaces"},
    "ConfigMap": {"api": "CoreV1Api", "list_all": "list_config_map_for_all_namespaces"},
    "Secret": {"api": "CoreV1Api", "list_all": "list_secret_for_all_namespaces"},
    "Service": {"api": "CoreV1Api", "list_all": "list_service_for_all_namespaces"},
    "PersistentVolumeClaim": {"api": "CoreV1Api", "list_all": "list_persistent_volume_claim_for_all_namespaces"},
    "ServiceAccount": {"api": "CoreV1Api", "list_all": "list_service_account_for_all_namespaces"},
    "Ingress": {"api": "NetworkingV1Api", "list_all": "list_ingress_for_all_namespaces"},
}

MAX_CACHE_ITEMS = 5000
MAX_WATCHES_PER_CLUSTER = 15
GRACE_PERIOD_SECONDS = 60
RECONNECT_BACKOFF_MAX = 30.0


# ─── 数据结构 ────────────────────────────────────────────────────────────────────

@dataclass
class WatchEvent:
    event_type: str  # ADDED / MODIFIED / DELETED
    kind: str
    namespace: str
    name: str
    resource_version: str
    object_summary: dict[str, Any]


@dataclass
class Subscriber:
    queue: asyncio.Queue
    kinds: set[str]
    namespace: str  # "" = all
    created_at: float = field(default_factory=time.time)


# ─── ResourceInformer ─────────────────────────────────────────────────────────

def _summarize_object(kind: str, obj: Any) -> dict[str, Any]:
    """从 K8s API 获取前端列表展示的摘要数据。"""
    if kind == "Pod":
        return summarize_pod(obj)
    elif kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob"):
        return summarize_workload(kind, obj)
    elif kind == "Event":
        meta = obj.metadata
        return {
            "name": meta.name,
            "namespace": meta.namespace or "",
            "kind": "Event",
            "resourceVersion": meta.resource_version,
            "creationTimestamp": meta.creation_timestamp.isoformat() if meta.creation_timestamp else "",
            "type": obj.type or "",
            "reason": obj.reason or "",
            "message": (obj.message or "")[:200],
            "involvedObject": f"{obj.involved_object.kind}/{obj.involved_object.name}" if obj.involved_object else "",
        }
    # fallback
    # 针对可能带来巨大 payload 的资源剔除 data 以防止 OOM
    if kind in ("ConfigMap", "Secret"):
        if hasattr(obj, "data"):
            obj.data = None
        if hasattr(obj, "string_data"):
            obj.string_data = None

    meta = obj.metadata
    return {
        "name": meta.name,
        "namespace": meta.namespace or "",
        "kind": kind,
        "resourceVersion": meta.resource_version,
    }


class ResourceInformer:
    """每个 (cluster_id, kind) 一个实例，维护该资源的内存镜像。"""

    def __init__(self, cluster_id: str, kind: str):
        self.cluster_id = cluster_id
        self.kind = kind
        self._cache: dict[str, dict[str, Any]] = {}  # "ns/name" → summary
        self._resource_version = ""
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_callback: Callable[[WatchEvent], None] | None = None
        self._api_client_factory: Callable[[str], Any] | None = None
        self.running = False
        self.synced = False

    def start(self, loop: asyncio.AbstractEventLoop, api_client_factory: Callable, event_callback: Callable):
        self._loop = loop
        self._api_client_factory = api_client_factory
        self._event_callback = event_callback
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch_loop, daemon=True, name=f"informer-{self.cluster_id}-{self.kind}")
        self._thread.start()
        self.running = True

    def stop(self):
        self._stop_event.set()
        self.running = False

    def get_cache(self) -> dict[str, dict[str, Any]]:
        return dict(self._cache)

    def _watch_loop(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                api_client = self._api_client_factory(self.cluster_id)
                kind_cfg = WATCHED_KINDS[self.kind]
                api_instance = getattr(client, kind_cfg["api"])(api_client=api_client)
                list_fn = getattr(api_instance, kind_cfg["list_all"])

                # 初始 LIST
                if not self._resource_version:
                    self._initial_list(list_fn)

                # WATCH
                w = watch.Watch()
                kwargs = {"resource_version": self._resource_version, "timeout_seconds": 300}
                for event in w.stream(list_fn, **kwargs):
                    if self._stop_event.is_set():
                        w.stop()
                        break
                    self._process_event(event)
                    backoff = 1.0

            except ApiException as e:
                if e.status == 410:
                    self._resource_version = ""
                    self._cache.clear()
                    logger.info(f"[Informer] {self.cluster_id}/{self.kind} 410 Gone, re-list")
                else:
                    logger.warning(f"[Informer] {self.cluster_id}/{self.kind} ApiException: {e.status}")
                    backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
            except Exception as exc:
                logger.warning(f"[Informer] {self.cluster_id}/{self.kind} error: {exc}")
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

            if not self._stop_event.is_set():
                self._stop_event.wait(timeout=backoff)

        logger.info(f"[Informer] {self.cluster_id}/{self.kind} stopped")

    def _initial_list(self, list_fn):
        result = list_fn(_request_timeout=10)
        self._resource_version = result.metadata.resource_version
        self._cache.clear()
        for item in result.items[:MAX_CACHE_ITEMS]:
            key = f"{item.metadata.namespace or ''}/{item.metadata.name}"
            self._cache[key] = _summarize_object(self.kind, item)
        self.synced = True
        logger.info(f"[Informer] {self.cluster_id}/{self.kind} listed {len(self._cache)} items, rv={self._resource_version}")

    def _process_event(self, event: dict):
        event_type = event["type"]  # ADDED / MODIFIED / DELETED
        obj = event["object"]
        key = f"{obj.metadata.namespace or ''}/{obj.metadata.name}"
        self._resource_version = obj.metadata.resource_version

        if event_type == "DELETED":
            self._cache.pop(key, None)
        else:
            if len(self._cache) >= MAX_CACHE_ITEMS and key not in self._cache:
                return
            self._cache[key] = _summarize_object(self.kind, obj)

        watch_event = WatchEvent(
            event_type=event_type,
            kind=self.kind,
            namespace=obj.metadata.namespace or "",
            name=obj.metadata.name,
            resource_version=obj.metadata.resource_version,
            object_summary=self._cache.get(key, {}),
        )
        if self._loop and self._event_callback:
            self._loop.call_soon_threadsafe(self._event_callback, watch_event)


# ─── PubSub ──────────────────────────────────────────────────────────────────────

class PubSub:
    def __init__(self):
        self._subscribers: dict[str, Subscriber] = {}

    async def subscribe(self, subscriber_id: str, kinds: set[str], namespace: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers[subscriber_id] = Subscriber(queue=queue, kinds=kinds, namespace=namespace)
        return queue

    async def unsubscribe(self, subscriber_id: str):
        self._subscribers.pop(subscriber_id, None)

    def publish(self, event: WatchEvent):
        dead = []
        for sub_id, sub in self._subscribers.items():
            if event.kind not in sub.kinds:
                continue
            if sub.namespace and sub.namespace != event.namespace:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(event)
                except Exception:
                    dead.append(sub_id)
        for sub_id in dead:
            self._subscribers.pop(sub_id, None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribers_for_kind(self, kind: str) -> int:
        return sum(1 for s in self._subscribers.values() if kind in s.kinds)


# ─── InformerManager ─────────────────────────────────────────────────────────────

class InformerManager:
    _instance: "InformerManager | None" = None

    def __init__(self):
        self._informers: dict[tuple[str, str], ResourceInformer] = {}  # (cluster_id, kind)
        self._pubsubs: dict[str, PubSub] = {}  # cluster_id → PubSub
        self._grace_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._api_client_factory: Callable | None = None

    @classmethod
    def get_instance(cls) -> "InformerManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def initialize(self, loop: asyncio.AbstractEventLoop, api_client_factory: Callable):
        self._loop = loop
        self._api_client_factory = api_client_factory

    def _get_pubsub(self, cluster_id: str) -> PubSub:
        if cluster_id not in self._pubsubs:
            self._pubsubs[cluster_id] = PubSub()
        return self._pubsubs[cluster_id]

    def _on_event(self, event: WatchEvent, cluster_id: str):
        """从 informer thread 通过 call_soon_threadsafe 调用，已在 event loop 中。"""
        pubsub = self._pubsubs.get(cluster_id)
        if pubsub:
            pubsub.publish(event)

    async def subscribe(self, cluster_id: str, kinds: set[str], namespace: str) -> tuple[str, asyncio.Queue, dict]:
        pubsub = self._get_pubsub(cluster_id)
        subscriber_id = str(uuid.uuid4())
        queue = await pubsub.subscribe(subscriber_id, kinds, namespace)

        # 按需启动 informer
        snapshot: dict[str, dict] = {}
        for kind in kinds:
            key = (cluster_id, kind)
            # 取消 grace period 关闭任务
            grace_task = self._grace_tasks.pop(key, None)
            if grace_task and not grace_task.done():
                grace_task.cancel()

            if key not in self._informers or not self._informers[key].running:
                active_count = sum(1 for k, inf in self._informers.items() if k[0] == cluster_id and inf.running)
                if active_count >= MAX_WATCHES_PER_CLUSTER:
                    continue
                informer = ResourceInformer(cluster_id, kind)
                informer.start(self._loop, self._api_client_factory, lambda ev, cid=cluster_id: self._on_event(ev, cid))
                self._informers[key] = informer

            inf = self._informers.get(key)
            if inf:
                # 等待 initial list 完成（最多 5 秒）
                for _ in range(50):
                    if inf.synced:
                        break
                    await asyncio.sleep(0.1)
                cache = inf.get_cache()
                if namespace:
                    cache = {k: v for k, v in cache.items() if k.startswith(f"{namespace}/")}
                snapshot[kind] = cache

        return subscriber_id, queue, snapshot

    async def unsubscribe(self, cluster_id: str, subscriber_id: str):
        pubsub = self._pubsubs.get(cluster_id)
        if not pubsub:
            return
        await pubsub.unsubscribe(subscriber_id)

        # 检查是否需要 grace period 关闭
        for key, informer in list(self._informers.items()):
            if key[0] != cluster_id or not informer.running:
                continue
            if pubsub.subscribers_for_kind(key[1]) == 0:
                if key not in self._grace_tasks or self._grace_tasks[key].done():
                    self._grace_tasks[key] = asyncio.ensure_future(self._grace_stop(key))

    async def _grace_stop(self, key: tuple[str, str]):
        await asyncio.sleep(GRACE_PERIOD_SECONDS)
        pubsub = self._pubsubs.get(key[0])
        if pubsub and pubsub.subscribers_for_kind(key[1]) > 0:
            return
        informer = self._informers.pop(key, None)
        if informer:
            informer.stop()
            logger.info(f"[InformerManager] grace stop {key[0]}/{key[1]}")

    async def shutdown(self):
        for task in self._grace_tasks.values():
            task.cancel()
        self._grace_tasks.clear()
        for informer in self._informers.values():
            informer.stop()
        self._informers.clear()
        self._pubsubs.clear()

    async def remove_cluster(self, cluster_id: str):
        keys_to_remove = [k for k in self._informers if k[0] == cluster_id]
        for key in keys_to_remove:
            inf = self._informers.pop(key, None)
            if inf:
                inf.stop()
            task = self._grace_tasks.pop(key, None)
            if task:
                task.cancel()
        self._pubsubs.pop(cluster_id, None)

    def get_informer_cache(self, cluster_id: str, kind: str) -> list[dict] | None:
        """供同步端点调用：如果 informer 活跃，直接返回缓存数据。"""
        key = (cluster_id, kind)
        informer = self._informers.get(key)
        if informer and informer.running:
            return list(informer.get_cache().values())
        return None
