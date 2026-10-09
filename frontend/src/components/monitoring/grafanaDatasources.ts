/**
 * OpsPilot 内置 Grafana 数据源引用（与 deploy provisioning 的 uid 保持一致）。
 *
 * 单独成文件：GrafanaLink 是组件文件，react-refresh/only-export-components
 * 不允许它再导出常量。
 */

/** Grafana 数据源引用（uid + 插件类型）。 */
export interface GrafanaDatasourceRef {
  uid: string
  type: string
}

/** 指标库（Prometheus 类型，指向 VictoriaMetrics）。 */
export const GRAFANA_DS_METRICS = { uid: 'opspilot-vm', type: 'prometheus' } as const

/** 日志库（VictoriaLogs 官方插件，手动装进 grafana 插件目录，见 deploy/README.md）。 */
export const GRAFANA_DS_LOGS = { uid: 'opspilot-vlogs', type: 'victoriametrics-logs-datasource' } as const
