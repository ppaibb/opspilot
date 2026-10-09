/**
 * K8s YAML 工作台（dry-run 预览 + apply 部署）。
 *
 * 两步安全流（对标 Rancher 的 YAML 部署）：
 * 1. 「试运行」——后端 server-side dry-run 校验多文档 YAML，逐条返回合法性与错误；
 * 2. 「应用」——仅在有试运行结果时可用；一次最多 20 个文档（后端限制）。
 *
 * 预置常用模板（Deployment/Service/Ingress/ConfigMap）降低上手成本。
 */
import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import axios from 'axios'
import {
  Modal, Input, Button, Space, Typography, Alert, App, Tag, Tabs, Tooltip,
} from 'antd'
import { SafetyCertificateOutlined, RocketOutlined, ClearOutlined } from '@ant-design/icons'
import { clusterOpsApi } from '@/api'
import { usePermission, useRiskConfirm } from '@/hooks/usePermission'

const { Text } = Typography

/** 从 AxiosError 中提取后端 detail 文案；对象型 detail（如 apply 拦截返回 {message, errors}）取 message */
function errorDetail(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string | { message?: string } } | undefined
    if (typeof data?.detail === 'string' && data.detail) return data.detail
    if (data?.detail && typeof data.detail === 'object' && data.detail.message) return data.detail.message
  }
  return fallback
}

const TEMPLATES: Record<string, string> = {
  deployment: `apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo-nginx
  namespace: default
  labels:
    app: demo-nginx
spec:
  replicas: 2
  selector:
    matchLabels:
      app: demo-nginx
  template:
    metadata:
      labels:
        app: demo-nginx
    spec:
      containers:
        - name: nginx
          image: nginx:1.27-alpine
          ports:
            - containerPort: 80
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
`,
  service: `apiVersion: v1
kind: Service
metadata:
  name: demo-nginx
  namespace: default
spec:
  selector:
    app: demo-nginx
  ports:
    - port: 80
      targetPort: 80
  type: ClusterIP
`,
  namespace: `apiVersion: v1
kind: Namespace
metadata:
  name: demo-team
  labels:
    team: demo
`,
}

/**
 * dry-run 响应（后端 cluster_dry_run）：
 * - {success, results}：全部文档校验通过；
 * - {success: false, results, errors}：errors 元素是 {kind,name,namespace,status,error} 对象。
 */
interface DryRunResult {
  success: boolean
  results?: Array<{ kind?: string; name?: string; namespace?: string; status?: string }>
  errors?: Array<{ kind?: string; name?: string; namespace?: string; status?: string; error?: string }>
}

interface Props {
  open: boolean
  clusterId: string
  clusterName?: string
  onClose: () => void
}

export default function ClusterYamlWorkbench({ open, clusterId, clusterName, onClose }: Props) {
  const { message, modal } = App.useApp()
  const { has } = usePermission()
  const riskConfirm = useRiskConfirm()
  const [yamlText, setYamlText] = useState('')
  const [dryRunResult, setDryRunResult] = useState<DryRunResult | null>(null)
  // 用 ref 跟踪"有未保存内容"：apply 成功后同步清空再 onClose，
  // 若读 state 会拿到尚未重渲染的旧值，误弹关闭确认。
  const dirtyRef = useRef(false)

  // 集群写操作权限键（backend/app/core/rbac.py: action.assets.clusters.manage）
  const canManage = has('action.assets.clusters.manage')

  const clearAll = () => { dirtyRef.current = false; setYamlText(''); setDryRunResult(null) }

  /** 关闭前拦截：有内容时确认一次，避免误点遮罩白填一场。 */
  const handleClose = () => {
    if (!dirtyRef.current) { onClose(); return }
    modal.confirm({
      title: '关闭 YAML 工作台？',
      content: '已填写的内容不会保留，确认关闭？',
      okText: '关闭', okButtonProps: { danger: true }, cancelText: '继续编辑',
      onOk: () => { dirtyRef.current = false; onClose() },
    })
  }

  const dryRunMutation = useMutation({
    mutationFn: () => clusterOpsApi.dryRun(clusterId, yamlText),
    onSuccess: (result: DryRunResult) => {
      setDryRunResult(result)
      if (result.success) message.success('试运行通过，可以应用')
      else message.warning('试运行发现问题，请修正后再应用')
    },
    onError: (error: unknown) => message.error(errorDetail(error, '试运行失败')),
  })

  const applyMutation = useMutation({
    mutationFn: () => clusterOpsApi.apply(clusterId, yamlText),
    onSuccess: () => {
      dirtyRef.current = false
      setYamlText('')
      setDryRunResult(null)
      message.success('YAML 已应用到集群')
      onClose()
    },
    onError: (error: unknown) => message.error(errorDetail(error, '应用失败')),
  })

  const canApply = !!dryRunResult?.success && yamlText.trim().length > 0

  return (
    <Modal
      title={`YAML 工作台${clusterName ? ` - ${clusterName}` : ''}`}
      open={open}
      onCancel={handleClose}
      maskClosable={false}
      width={860}
      footer={
        <Space>
          <Tooltip title="server-side dry-run 校验（不落地）">
            <Button icon={<SafetyCertificateOutlined />} loading={dryRunMutation.isPending}
              disabled={!yamlText.trim()} onClick={() => { setDryRunResult(null); dryRunMutation.mutate() }}>
              试运行
            </Button>
          </Tooltip>
          <Tooltip title={canManage ? undefined : '没有集群写操作权限（action.assets.clusters.manage）'}>
            <Button
              type="primary"
              danger
              icon={<RocketOutlined />}
              loading={applyMutation.isPending}
              disabled={!canApply || !canManage}
              onClick={() => riskConfirm.confirm('clusters:apply', () => applyMutation.mutate(),
                `将把当前 YAML 通过 server-side apply 写入集群${clusterName ? ` ${clusterName}` : ''}，同名资源将被覆盖`)}
            >
              应用到集群
            </Button>
          </Tooltip>
          <Button icon={<ClearOutlined />} onClick={clearAll}>清空</Button>
        </Space>
      }
    >
      <Space direction="vertical" size="small" style={{ width: '100%' }}>
        <Tabs
          size="small"
          items={Object.entries(TEMPLATES).map(([key]) => ({
            key,
            label: key === 'deployment' ? '模板：Deployment' : key === 'service' ? '模板：Service' : '模板：Namespace',
          }))}
          onChange={(key) => { dirtyRef.current = true; setYamlText(TEMPLATES[key] || ''); setDryRunResult(null) }}
        />
        <Input.TextArea
          value={yamlText}
          onChange={(e) => { dirtyRef.current = true; setYamlText(e.target.value); setDryRunResult(null) }}
          rows={14}
          spellCheck={false}
          placeholder={'粘贴 Kubernetes YAML（支持多文档，--- 分隔，单次最多 20 个文档）\n\n先「试运行」校验，通过后才能「应用」。'}
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, monospace', fontSize: 12 }}
        />
        {dryRunResult && (
          <Alert
            type={dryRunResult.success ? 'success' : 'error'}
            showIcon
            message={dryRunResult.success ? '试运行通过' : '试运行未通过'}
            description={
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                {(dryRunResult.results || []).map((r, idx) => (
                  <Text key={idx} style={{ fontSize: 12 }}>
                    ✅ {r.kind}/{r.namespace ? `${r.namespace}/` : ''}{r.name}
                  </Text>
                ))}
                {(dryRunResult.errors || []).map((err, idx) => (
                  <Text key={`e-${idx}`} type="danger" style={{ fontSize: 12 }}>
                    ❌ {err.kind}/{err.namespace ? `${err.namespace}/` : ''}{err.name}
                    {err.error ? ` — ${err.error}` : ''}
                  </Text>
                ))}
              </Space>
            }
          />
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          支持任意资源类型（含 CRD）；带 <Tag style={{ fontSize: 10 }}>试运行</Tag> 通过的多文档一键部署，操作会写入集群审计日志。
        </Text>
      </Space>
    </Modal>
  )
}
