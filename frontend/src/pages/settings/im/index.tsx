/**
 * 个人 IM 通知设置（飞书群 @）
 *
 * 模块说明：
 * 每个登录用户在这里绑定自己的飞书身份。绑定后，平台里与"我"相关的事件
 * （证书签发/续期、SQL 工单审批与执行结果、虚机同步、Agent 安装、告警规则
 * 里指定"我"为通知人等）都会在飞书群里 @ 本人，点击链接可回到平台处理。
 *
 * 第一版范围：仅飞书、仅群自定义机器人 @；私信与 IM 内操作需飞书自建应用
 * 消息能力与公网回调地址，见开发文档 29 期规划。
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Card, Descriptions, Form, Input, Radio, Button, Space, Tag, Popconfirm, App, Typography,
} from 'antd'
import { SendOutlined, SaveOutlined, DisconnectOutlined } from '@ant-design/icons'
import { imApi } from '@/api'

const { Text } = Typography

export default function PersonalImPage() {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [testing, setTesting] = useState(false)

  const { data: binding, isLoading } = useQuery({
    queryKey: ['im-binding'],
    queryFn: imApi.getBinding,
  })
  const bound = !!binding?.bound

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['im-binding'] })

  const bindMut = useMutation({
    mutationFn: (values: { identity: string; webhook_url?: string; notify_mode: string }) => imApi.bind(values),
    onSuccess: () => {
      message.success('绑定成功，平台事件将在飞书群里 @ 你')
      invalidate()
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '绑定失败，请检查手机号/邮箱与飞书应用配置')
    },
  })

  const unbindMut = useMutation({
    mutationFn: imApi.unbind,
    onSuccess: () => {
      message.success('已解绑')
      invalidate()
      form.resetFields()
    },
    onError: () => message.error('解绑失败'),
  })

  const modeMut = useMutation({
    mutationFn: (notify_mode: string) => {
      // 切换通知方式复用绑定接口：identity 用已绑定值，open_id 由后端重新解析
      return imApi.bind({ identity: binding?.identity || '', notify_mode })
    },
    onSuccess: () => {
      message.success('通知方式已更新')
      invalidate()
    },
    onError: () => message.error('更新失败'),
  })

  const handleTest = async () => {
    setTesting(true)
    try {
      await imApi.test('')
      message.success('测试消息已发送，请到飞书群查看是否 @ 了你')
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      message.error(detail || '发送失败，请检查群机器人配置')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message="飞书群 @ 通知"
          description={
            <span>
              绑定后，证书签发/续期、SQL 工单审批与执行结果、虚机同步、Agent
              安装等与你相关的事件，都会在飞书群里 @ 你并附平台处理链接；
              告警规则里也可以把"你"选为通知人。未绑定或选择关闭时，只在站内信通知。
            </span>
          }
        />

        <Card
          title="我的飞书绑定"
          loading={isLoading}
          extra={
            bound && (
              <Space>
                <Button icon={<SendOutlined />} loading={testing} onClick={handleTest}>
                  发送测试消息
                </Button>
                <Popconfirm
                  title="确认解绑？"
                  description="解绑后将不再收到飞书群 @ 通知"
                  onConfirm={() => unbindMut.mutate(undefined)}
                >
                  <Button danger icon={<DisconnectOutlined />}>解绑</Button>
                </Popconfirm>
              </Space>
            )
          }
        >
          {bound ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions column={2} size="small" bordered>
                <Descriptions.Item label="绑定账号">{binding?.identity}</Descriptions.Item>
                <Descriptions.Item label="飞书 ID">
                  <Text code>{binding?.open_id_masked}</Text>
                </Descriptions.Item>
                <Descriptions.Item label="群机器人">
                  {binding?.webhook_url
                    ? <Text code style={{ fontSize: 12 }}>{binding?.webhook_url}</Text>
                    : <Tag color="blue">系统默认群</Tag>}
                </Descriptions.Item>
                <Descriptions.Item label="绑定时间">{binding?.bound_at || '-'}</Descriptions.Item>
              </Descriptions>

              <Form layout="inline">
                <Form.Item label="通知方式" style={{ marginBottom: 0 }}>
                  <Radio.Group
                    value={binding?.notify_mode || 'group_at'}
                    onChange={(e) => modeMut.mutate(e.target.value)}
                    disabled={modeMut.isPending}
                    options={[
                      { value: 'group_at', label: '群内 @ 我' },
                      { value: 'off', label: '关闭' },
                    ]}
                  />
                </Form.Item>
              </Form>
            </Space>
          ) : (
            <Form
              form={form}
              layout="vertical"
              style={{ maxWidth: 520 }}
              initialValues={{ notify_mode: 'group_at' }}
              onFinish={(values) => bindMut.mutate(values)}
            >
              <Form.Item
                name="identity"
                label="手机号或邮箱"
                rules={[
                  { required: true, message: '请输入绑定用的手机号或邮箱' },
                  {
                    pattern: /^(1\d{10}|[^@\s]+@[^@\s]+\.[^@\s]+)$/,
                    message: '格式不正确：需为 11 位手机号或邮箱',
                  },
                ]}
                extra="须是已加入公司飞书组织的账号，后端据此解析你的飞书身份"
              >
                <Input placeholder="例如：13800000001 或 zhangsan@company.com" />
              </Form.Item>

              <Form.Item
                name="webhook_url"
                label="群自定义机器人地址（可选）"
                rules={[
                  {
                    validator: (_, value: string) =>
                      !value || value.startsWith('https://')
                        ? Promise.resolve()
                        : Promise.reject(new Error('必须是 https:// 开头的飞书机器人地址')),
                  },
                ]}
                extra={
                  <span>
                    不填则使用管理员配置的系统默认群。获取方式：飞书群 → 设置 → 群机器人 →
                    添加"自定义机器人"→ 复制 Webhook 地址（仅支持 open.feishu.cn 域名）
                  </span>
                }
              >
                <Input placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..." />
              </Form.Item>

              <Form.Item name="notify_mode" label="通知方式" rules={[{ required: true }]}>
                <Radio.Group
                  options={[
                    { value: 'group_at', label: '群内 @ 我' },
                    { value: 'off', label: '先关闭（仅站内信）' },
                  ]}
                />
              </Form.Item>

              <Button
                type="primary"
                htmlType="submit"
                icon={<SaveOutlined />}
                loading={bindMut.isPending}
              >
                绑定飞书
              </Button>
            </Form>
          )}
        </Card>

        <Card title="收不到通知？按以下顺序排查" size="small">
          <ol style={{ margin: 0, paddingLeft: 20, color: 'rgba(0,0,0,0.65)' }}>
            <li>确认管理员已在「系统管理 → IM 接入（飞书）」配置好飞书应用凭证（App ID / App Secret）。</li>
            <li>确认你填写的手机号/邮箱与飞书账号一致，且已加入公司飞书组织。</li>
            <li>确认群机器人已在目标群里添加成功；用「发送测试消息」验证。</li>
            <li>只有 @ 需要你在该群里；若换了群，更新上方的群机器人地址。</li>
            <li>高危操作（删除、执行 SQL 等）不会推送到 IM，请登录平台处理。</li>
          </ol>
        </Card>
      </Space>
    </div>
  )
}
