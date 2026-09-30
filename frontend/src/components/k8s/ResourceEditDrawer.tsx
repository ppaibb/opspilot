/**
 * K8s 资源可视化编辑抽屉 —— KubeSphere/Rancher 式表单编辑。
 *
 * 对接后端 GET/PATCH /resources/{ns}/{kind}/{name}/edit（安全白名单字段协议）：
 * - Deployment/StatefulSet：副本数 + 容器镜像
 * - DaemonSet：容器镜像
 * - Service：类型 + 端口表（port/targetPort/protocol/nodePort）
 * - ConfigMap：data 键值编辑（Secret 后端刻意不下发，走 YAML 通道）
 * - 通用：labels / annotations（k=v 行编辑）
 *
 * 保存走 PATCH edit-form（服务端二次白名单校验，不允许任意 spec 字段）；
 * 保存成功后失效宿主页面相关 query。
 */
import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Drawer, Form, InputNumber, Input, Select, Button, Space, Typography, App, Alert, Spin, Empty, Tabs, Flex,
} from 'antd'
import { SaveOutlined, PlusOutlined, DeleteOutlined } from '@ant-design/icons'
import client from '@/api/client'
import { useRiskConfirm, extractAxiosDetail } from '@/hooks/usePermission'

const { Text } = Typography
const { TextArea } = Input

export interface EditTarget {
  clusterId: string
  namespace: string
  kind: string
  name: string
}

interface EditPayload {
  kind: string
  name: string
  namespace: string
  editable: boolean
  labels: Record<string, string>
  annotations: Record<string, string>
  fields: { metadata: boolean; replicas: boolean; containers: boolean; service: boolean; data: boolean }
  replicas: number | null
  containers: Array<{ name: string; image: string }>
  service: { type: string; ports: Array<{ name: string; port: number; target_port: string; protocol: string; node_port: string | number }> } | null
  data: Record<string, string> | null
}

/** 可视化编辑表单的值（与上方 Form.List / Form.Item 的 name 一一对应）。 */
interface EditFormValues {
  replicas?: number
  containers?: Array<{ name?: string; image?: string }>
  service_type?: string
  service_ports?: Array<{ name?: string; port?: number | string; target_port?: string | number; protocol?: string; node_port?: string | number }>
}

/** k=v 多行文本 ↔ dict（-key 行删除），与节点标签编辑同语义。 */
function parseKv(text: string): Record<string, string | null> {
  const result: Record<string, string | null> = {}
  for (const line of text.split('\n')) {
    const row = line.trim()
    if (!row) continue
    if (row.startsWith('-')) {
      const key = row.slice(1).split('=')[0].trim()
      if (key) result[key] = null
      continue
    }
    const [k, v] = row.split('=')
    if (k?.trim()) result[k.trim()] = (v ?? '').trim()
  }
  return result
}

function renderKv(map: Record<string, string>): string {
  return Object.entries(map || {}).map(([k, v]) => `${k}=${v}`).join('\n')
}

export default function ResourceEditDrawer({ target, onClose }: { target: EditTarget | null; onClose: () => void }) {
  const { message } = App.useApp()
  const { confirm } = useRiskConfirm()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()
  const [labelsText, setLabelsText] = useState('')
  const [annotationsText, setAnnotationsText] = useState('')
  const [cmData, setCmData] = useState<Array<{ key: string; value: string }>>([])

  const { data: payload, isLoading } = useQuery({
    queryKey: ['resource-edit-form', target],
    queryFn: () =>
      client.get(`/clusters/${target!.clusterId}/resources/${target!.namespace || 'default'}/${target!.kind}/${target!.name}/edit`)
        .then((r) => r.data as EditPayload),
    enabled: !!target,
  })

  // 表单回填（payload 到达后一次）。
  // （回填延后到微任务，避免在 effect 内同步 setState 触发级联渲染）
  useEffect(() => {
    if (!payload) return
    queueMicrotask(() => {
      setLabelsText(renderKv(payload.labels))
      setAnnotationsText(renderKv(payload.annotations))
      setCmData(payload.data ? Object.entries(payload.data).map(([key, value]) => ({ key, value: String(value) })) : [])
      form.setFieldsValue({
        replicas: payload.replicas ?? undefined,
        containers: payload.containers,
        service_type: payload.service?.type,
        service_ports: payload.service?.ports,
      })
    })
  }, [payload, form])

  const saveMutation = useMutation({
    mutationFn: (values: EditFormValues) => {
      const body: Record<string, unknown> = {}
      if (payload?.fields.replicas && values.replicas != null) body.replicas = values.replicas
      if (payload?.fields.containers && values.containers?.length) {
        body.containers = values.containers.map((c) => ({ name: c.name, image: c.image }))
      }
      if (payload?.fields.service) {
        body.service_type = values.service_type
        body.ports = (values.service_ports || []).map((p) => ({
          name: p.name || '', port: Number(p.port), target_port: String(p.target_port ?? ''),
          protocol: p.protocol || 'TCP', node_port: p.node_port ? Number(p.node_port) : undefined,
        }))
      }
      if (payload?.fields.data) {
        body.data = Object.fromEntries(cmData.filter((d) => d.key.trim()).map((d) => [d.key, d.value]))
      }
      const labels = parseKv(labelsText)
      if (Object.keys(labels).length) body.labels = labels
      const annotations = parseKv(annotationsText)
      if (Object.keys(annotations).length) body.annotations = annotations
      return client.patch(
        `/clusters/${target!.clusterId}/resources/${target!.namespace || 'default'}/${target!.kind}/${target!.name}/edit`,
        body,
      ).then((r) => r.data)
    },
    onSuccess: () => {
      message.success('资源已更新')
      // 失效宿主页面所有相关缓存。
      queryClient.invalidateQueries({ queryKey: ['cluster-workloads'] })
      queryClient.invalidateQueries({ queryKey: ['cluster-resources'] })
      queryClient.invalidateQueries({ queryKey: ['cluster-resource-yaml'] })
      onClose()
    },
    onError: (error: unknown) => {
      // 后端 dry-run 拦截时返回对象型 detail（{message, errors}），优先抽 message
      const data = axios.isAxiosError(error)
        ? (error.response?.data as { detail?: string | { message?: string } } | undefined)
        : undefined
      const detail = data?.detail
      const text = typeof detail === 'string'
        ? detail
        : detail?.message || extractAxiosDetail(error) || '保存失败（字段冲突或权限不足）'
      message.error(text)
    },
  })

  return (
    <Drawer
      title={`可视化编辑 - ${target ? `${target.kind}/${target.name}` : ''}`}
      open={!!target}
      onClose={onClose}
      width={640}
      destroyOnClose
      extra={
        <Button type="primary" icon={<SaveOutlined />} loading={saveMutation.isPending}
          disabled={!payload?.editable}
          // 后端资源表单编辑挂 edit 守卫动作，需确认 + 一次性票据
          onClick={() => confirm('clusters:edit', () => form.submit())}>
          保存
        </Button>
      }
    >
      {isLoading ? (
        <div style={{ textAlign: 'center', padding: '60px 0' }}><Spin tip="读取资源..." /></div>
      ) : !payload ? (
        <Empty description="资源不存在" />
      ) : (
        <Form form={form} layout="vertical" onFinish={(values) => saveMutation.mutate(values)}>
          {!payload.editable && (
            <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="Secret 不支持表单编辑，请走 YAML 通道" />
          )}
          <Tabs
            size="small"
            items={[
              ...(payload.fields.replicas || payload.fields.containers || payload.fields.service || payload.fields.data ? [{
                key: 'spec',
                label: 'Spec',
                children: (
                  <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                    {payload.fields.replicas && (
                      <Form.Item name="replicas" label="副本数" rules={[{ required: true, message: '必填' }]}>
                        <InputNumber min={0} max={999} style={{ width: 140 }} />
                      </Form.Item>
                    )}
                    {payload.fields.containers && (
                      <div>
                        <Text strong style={{ fontSize: 12 }}>容器镜像（按容器名修改，不支持新增容器）</Text>
                        <Form.List name="containers">
                          {(fields, { add, remove }) => (
                            <>
                              {fields.map((field) => (
                                <Space key={field.key} align="baseline" style={{ display: 'flex', width: '100%' }}>
                                  <Form.Item name={[field.name, 'name']} noStyle>
                                    <Input size="small" style={{ width: 130 }} disabled placeholder="容器名" />
                                  </Form.Item>
                                  <Form.Item
                                    name={[field.name, 'image']} noStyle
                                    rules={[{ required: true, message: '镜像必填' }]}
                                  >
                                    <Input size="small" style={{ width: 380, fontFamily: 'monospace' }} placeholder="nginx:1.27" />
                                  </Form.Item>
                                  {fields.length > 1 && (
                                    <Button size="small" type="text" danger icon={<DeleteOutlined />}
                                      onClick={() => remove(field.name)} />
                                  )}
                                </Space>
                              ))}
                              <Button size="small" type="dashed" icon={<PlusOutlined />}
                                onClick={() => add({ name: '', image: '' })} disabled>
                                新增容器（协议不允许，请走 YAML）
                              </Button>
                            </>
                          )}
                        </Form.List>
                      </div>
                    )}
                    {payload.fields.service && payload.service && (
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        <Form.Item name="service_type" label="Service 类型" style={{ marginBottom: 8 }}>
                          <Select style={{ width: 160 }} options={['ClusterIP', 'NodePort', 'LoadBalancer', 'ExternalName'].map((v) => ({ value: v, label: v }))} />
                        </Form.Item>
                        <Text strong style={{ fontSize: 12 }}>端口</Text>
                        <Form.List name="service_ports">
                          {(fields, { add, remove }) => (
                            <>
                              {fields.map((field) => (
                                <Space key={field.key} align="baseline" wrap>
                                  <Form.Item name={[field.name, 'name']} noStyle>
                                    <Input size="small" style={{ width: 100 }} placeholder="名称" />
                                  </Form.Item>
                                  <Form.Item name={[field.name, 'port']} noStyle rules={[{ required: true }]}>
                                    <InputNumber size="small" style={{ width: 90 }} placeholder="port" />
                                  </Form.Item>
                                  <Form.Item name={[field.name, 'target_port']} noStyle>
                                    <Input size="small" style={{ width: 90 }} placeholder="targetPort" />
                                  </Form.Item>
                                  <Form.Item name={[field.name, 'protocol']} noStyle>
                                    <Select size="small" style={{ width: 90 }} options={['TCP', 'UDP', 'SCTP'].map((v) => ({ value: v, label: v }))} />
                                  </Form.Item>
                                  <Form.Item name={[field.name, 'node_port']} noStyle>
                                    <InputNumber size="small" style={{ width: 90 }} placeholder="nodePort" />
                                  </Form.Item>
                                  <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
                                </Space>
                              ))}
                              <Button size="small" type="dashed" icon={<PlusOutlined />}
                                onClick={() => add({ name: '', port: 80, target_port: '80', protocol: 'TCP', node_port: '' })}>
                                新增端口
                              </Button>
                            </>
                          )}
                        </Form.List>
                      </Space>
                    )}
                    {payload.fields.data && (
                      <div>
                        <Flex justify="space-between" align="center">
                          <Text strong style={{ fontSize: 12 }}>ConfigMap 数据（键值）</Text>
                          <Button size="small" type="dashed" icon={<PlusOutlined />}
                            onClick={() => setCmData((prev) => [...prev, { key: '', value: '' }])}>
                            新增键
                          </Button>
                        </Flex>
                        {cmData.map((item, idx) => (
                          <Space key={idx} align="baseline" style={{ display: 'flex', width: '100%', marginTop: 6 }}>
                            <Input size="small" style={{ width: 160, fontFamily: 'monospace' }}
                              value={item.key}
                              onChange={(e) => setCmData((prev) => prev.map((d, i) => (i === idx ? { ...d, key: e.target.value } : d)))}
                              placeholder="键" />
                            <TextArea
                              autoSize style={{ width: 340, fontFamily: 'monospace', fontSize: 12 }}
                              value={item.value}
                              onChange={(e) => setCmData((prev) => prev.map((d, i) => (i === idx ? { ...d, value: e.target.value } : d)))}
                              placeholder="值" />
                            <Button size="small" type="text" danger icon={<DeleteOutlined />}
                              onClick={() => setCmData((prev) => prev.filter((_, i) => i !== idx))} />
                          </Space>
                        ))}
                      </div>
                    )}
                  </Space>
                ),
              }] : []),
              {
                key: 'metadata',
                label: 'Labels / Annotations',
                children: (
                  <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                    <div>
                      <Text strong style={{ fontSize: 12 }}>Labels（每行 k=v；-key 删除）</Text>
                      <TextArea rows={5} value={labelsText} onChange={(e) => setLabelsText(e.target.value)}
                        style={{ fontFamily: 'monospace', fontSize: 12 }} />
                    </div>
                    <div>
                      <Text strong style={{ fontSize: 12 }}>Annotations（每行 k=v；-key 删除）</Text>
                      <TextArea rows={4} value={annotationsText} onChange={(e) => setAnnotationsText(e.target.value)}
                        style={{ fontFamily: 'monospace', fontSize: 12 }} />
                    </div>
                  </Space>
                ),
              },
            ]}
          />
        </Form>
      )}
    </Drawer>
  )
}
