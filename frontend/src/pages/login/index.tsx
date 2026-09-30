import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Form, Input, Button, Typography, Alert, App, ConfigProvider, theme as antdTheme } from 'antd'
import { UserOutlined, LockOutlined } from '@ant-design/icons'
import { authApi, loginPageApi } from '@/api'
import { useAuthStore } from '@/store/auth'

const { Title, Text } = Typography

// OpsPilot 原创插图：火箭 + 轨道 + 星空
function HeroIllustration() {
  return (
    <svg viewBox="0 0 320 320" width="300" height="300" aria-hidden="true">
      {/* ── 背景星点 ── */}
      {[
        [30, 40], [55, 90], [20, 160], [40, 220], [70, 270],
        [260, 30], [290, 80], [300, 150], [275, 210], [255, 270],
        [150, 20], [200, 55], [100, 55],
      ].map(([cx, cy], i) => (
        <circle key={i} cx={cx} cy={cy} r={i % 3 === 0 ? 2.5 : 1.5}
          fill="rgba(255,255,255,0.6)" />
      ))}

      {/* ── 地球（左下） ── */}
      <circle cx="72" cy="258" r="38" fill="#1d4ed8" opacity="0.9" />
      <ellipse cx="72" cy="258" rx="38" ry="14" fill="none"
        stroke="rgba(255,255,255,0.25)" strokeWidth="1.5" />
      <ellipse cx="72" cy="258" rx="20" ry="38" fill="none"
        stroke="rgba(255,255,255,0.2)" strokeWidth="1.5" />
      {/* 大陆色块 */}
      <ellipse cx="60" cy="248" rx="12" ry="8" fill="#16a34a" opacity="0.7" />
      <ellipse cx="82" cy="265" rx="8" ry="6" fill="#16a34a" opacity="0.6" />

      {/* ── 轨道环（穿过火箭） ── */}
      <ellipse cx="170" cy="170" rx="115" ry="40"
        fill="none" stroke="rgba(255,255,255,0.18)" strokeWidth="1.5"
        strokeDasharray="8 5" />
      {/* 轨道上的小卫星 */}
      <circle cx="60" cy="178" r="6" fill="#60a5fa" />
      <circle cx="280" cy="163" r="5" fill="#a78bfa" />

      {/* ── 火箭主体 ── */}
      {/* 尾焰 */}
      <ellipse cx="170" cy="255" rx="14" ry="22" fill="#f97316" opacity="0.9" />
      <ellipse cx="170" cy="268" rx="8" ry="14" fill="#fbbf24" opacity="0.8" />
      <ellipse cx="170" cy="278" rx="4" ry="8" fill="#fde68a" opacity="0.7" />

      {/* 火箭体 */}
      <rect x="150" y="130" width="40" height="110" rx="8" fill="white" />
      {/* 舱体细节条纹 */}
      <rect x="150" y="165" width="40" height="3" fill="#e0e7ff" />
      <rect x="150" y="175" width="40" height="3" fill="#e0e7ff" />
      {/* 舷窗 */}
      <circle cx="170" cy="150" r="11" fill="#1e40af" />
      <circle cx="170" cy="150" r="7" fill="#60a5fa" />
      <circle cx="167" cy="147" r="2.5" fill="rgba(255,255,255,0.6)" />

      {/* 两侧翼片 */}
      <polygon points="150,220 130,250 150,240" fill="#c7d2fe" />
      <polygon points="190,220 210,250 190,240" fill="#c7d2fe" />

      {/* 火箭鼻锥 */}
      <polygon points="150,130 190,130 170,80" fill="#6366f1" />
      {/* 鼻锥高光 */}
      <polygon points="163,118 170,80 170,118" fill="rgba(255,255,255,0.2)" />

      {/* ── 数据流点（运维感） ── */}
      {[
        [100, 110, '#34d399'],
        [88, 130, '#60a5fa'],
        [95, 150, '#f472b6'],
        [240, 100, '#34d399'],
        [252, 125, '#fbbf24'],
        [244, 148, '#60a5fa'],
      ].map(([cx, cy, fill], i) => (
        <circle key={`dot-${i}`} cx={cx as number} cy={cy as number} r="3.5"
          fill={fill as string} opacity="0.85" />
      ))}
      {/* 连线 */}
      <polyline points="100,110 88,130 95,150"
        fill="none" stroke="rgba(255,255,255,0.2)" strokeWidth="1" />
      <polyline points="240,100 252,125 244,148"
        fill="none" stroke="rgba(255,255,255,0.2)" strokeWidth="1" />
    </svg>
  )
}

export default function LoginPage() {
  const { modal } = App.useApp()
  const [loading, setLoading] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [config, setConfig] = useState<{ hero_title?: string; hero_subtitle?: string }>({})
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)

  useEffect(() => {
    loginPageApi.getConfig().then(setConfig).catch(() => {})
  }, [])

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    setErrorMsg(null)
    try {
      const res = await authApi.login(values)
      setAuth(res.access_token, { id: res.user.id, username: res.user.username, role: res.user.role })
      navigate('/', { replace: true })
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status
      if (status === 401 || status === 400) {
        setErrorMsg('用户名或密码错误，请重新输入')
      } else {
        setErrorMsg('服务器繁忙，请稍后重试')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    // 登录页固定「左深右白」：强制 antd 浅色算法，避免全局深色主题把白底文字/输入框染成浅色
    <ConfigProvider
      theme={{
        algorithm: antdTheme.defaultAlgorithm,
        token: {
          colorPrimary: '#1677ff',
          colorBgContainer: '#ffffff',
          colorBgLayout: '#ffffff',
          colorText: '#0f172a',
          colorBorder: '#e2e8f0',
          borderRadius: 6,
        },
      }}
    >
    <div style={{ display: 'flex', minHeight: '100vh', background: '#fff' }}>

      {/* ── 左侧深色面板 ── */}
      <div
        style={{
          flex: '0 0 60%',
          background: 'linear-gradient(160deg, #1e1b4b 0%, #312e81 60%, #4338ca 100%)',
          display: 'flex',
          flexDirection: 'column',
          padding: '28px 36px',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        {/* Logo 区 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div
            style={{
              width: 32, height: 32, borderRadius: 8,
              background: 'rgba(255,255,255,0.15)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 16, color: '#fff', fontWeight: 700,
            }}
          >
            O
          </div>
          <span style={{ color: '#fff', fontWeight: 600, fontSize: 16 }}>OpsPilot</span>
        </div>

        {/* 插图 */}
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <HeroIllustration />
        </div>

        {/* 底部链接 */}
        <div style={{ display: 'flex', gap: 16 }}>
          <a href="#" style={{ color: 'rgba(255,255,255,0.5)', fontSize: 13, textDecoration: 'none' }}>帮助中心</a>
          <a href="#" style={{ color: 'rgba(255,255,255,0.5)', fontSize: 13, textDecoration: 'none' }}>隐私政策</a>
        </div>
      </div>

      {/* ── 右侧表单区 ── */}
      <div
        className="login-form-card"
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '48px 64px',
          background: '#fff',
        }}
      >
        <div style={{ width: '100%', maxWidth: 360 }}>
          {/* 标题 */}
          <div style={{ textAlign: 'center', marginBottom: 36 }}>
            <Title level={2} style={{ marginBottom: 6, fontWeight: 700, color: '#0f172a' }}>
              {config.hero_title ?? 'OpsPilot'}
            </Title>
            <Text type="secondary" style={{ fontSize: 14, color: '#64748b' }}>
              {config.hero_subtitle ?? '企业级智能运维管理平台'}
            </Text>
          </div>

          {/* 表单 */}
          <Form name="login" size="large" onFinish={onFinish} autoComplete="off">
            <div style={{ marginBottom: 6, fontSize: 13, color: '#374151' }}>账号</div>
            <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
              <Input
                prefix={<UserOutlined style={{ color: '#9ca3af' }} />}
                placeholder="请输入账号"
                style={{ borderRadius: 8 }}
              />
            </Form.Item>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <span style={{ fontSize: 13, color: '#374151' }}>密码</span>
              <a
                style={{ fontSize: 13 }}
                onClick={() => modal.info({
                  title: '找回密码',
                  content: '请联系管理员重置密码',
                  okText: '知道了',
                  centered: true,
                })}
              >
                忘记密码？
              </a>
            </div>
            <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password
                prefix={<LockOutlined style={{ color: '#9ca3af' }} />}
                placeholder="请输入密码"
                style={{ borderRadius: 8 }}
              />
            </Form.Item>
            <Form.Item style={{ marginTop: 8 }}>
              {errorMsg && (
                <Alert
                  message={errorMsg}
                  type="error"
                  showIcon
                  style={{ marginBottom: 12, borderRadius: 8 }}
                />
              )}
              <Button
                type="primary"
                htmlType="submit"
                loading={loading}
                block
                style={{ borderRadius: 8, height: 44, fontWeight: 600, fontSize: 16 }}
              >
                登 录
              </Button>
            </Form.Item>
          </Form>

          {/* 底部提示 */}
          <div style={{ textAlign: 'center', marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 13, color: '#64748b' }}>
              暂无账号？请联系管理员申请开通
            </Text>
          </div>
        </div>
      </div>
    </div>
    </ConfigProvider>
  )
}
