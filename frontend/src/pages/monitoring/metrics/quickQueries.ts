/** 常用指标快捷 PromQL（node_exporter 体系），点击填入查询框。 */

export interface QuickMetric {
  label: string
  value: string
}

export const QUICK_METRICS: QuickMetric[] = [
  { label: 'CPU', value: '100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)' },
  { label: '内存', value: '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100' },
  { label: '磁盘', value: '(1 - node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100' },
  { label: '网络', value: 'rate(node_network_receive_bytes_total[5m])' },
  { label: 'Load', value: 'node_load1' },
]
