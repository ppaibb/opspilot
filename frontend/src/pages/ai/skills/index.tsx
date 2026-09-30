/**
 * AI 技能管理页（/ai/skills）。
 *
 * 技能（Skill）是 Ops Agent 可被 @ 召唤的只读能力单元（如查告警、看资产）。
 * 本页管理技能注册表的整体启停：
 * - 按分类分组展示全部技能（风险等级、示例问法）
 * - 批量开关：支持「全开 / 全关 / 按分类切换」后一次性保存
 * - 保存的是完整注册表快照，服务端按 key 合并
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card, Row, Col, Tag, Switch, Button, Space, Flex, Typography, Empty, Spin, App, Tooltip, Input,
} from 'antd'
import { ReloadOutlined, SaveOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { aiPlatformApi } from '@/api'
import { usePermission, extractAxiosDetail } from '@/hooks/usePermission'
import type { AISkillItem } from '@/types/api'

const { Text } = Typography

/** 风险等级 → 标签色。技能均为只读能力，high 仅代表影响面大。 */
const RISK_COLOR: Record<string, string> = {
  high: 'red',
  medium: 'orange',
  low: 'green',
}

const RISK_LABEL: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
}

export default function AISkillsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const { has } = usePermission()
  const canManage = has('action.ai.skills.manage')

  const [search, setSearch] = useState('')

  const { data: registry, isLoading, refetch } = useQuery({
    queryKey: ['ai-skill-registry'],
    queryFn: aiPlatformApi.getSkillRegistry,
  })

  /** 本地草稿态：toggle 后需手动「保存」才落库，避免误触。 */
  const [draft, setDraft] = useState<Record<string, boolean> | null>(null)

  // 注册表拉到后初始化一次草稿；保存成功后重置草稿回归服务端状态。
  const skills = registry?.skills || []
  const effective = useMemo(
    () => skills.map((s) => ({ ...s, enabled: draft ? (draft[s.key] ?? s.enabled) : s.enabled })),
    [skills, draft],
  )

  const dirty = useMemo(
    () => skills.some((s) => (draft ? (draft[s.key] ?? s.enabled) : s.enabled) !== s.enabled),
    [skills, draft],
  )

  const byCategory = useMemo(() => {
    const filtered = effective.filter(
      (s) => !search
        || s.title.toLowerCase().includes(search.toLowerCase())
        || s.key.toLowerCase().includes(search.toLowerCase())
        || s.description.toLowerCase().includes(search.toLowerCase()),
    )
    const groups = new Map<string, AISkillItem[]>()
    for (const skill of filtered) {
      const list = groups.get(skill.category) || []
      list.push(skill)
      groups.set(skill.category, list)
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [effective, search])

  const saveMutation = useMutation({
    mutationFn: () =>
      aiPlatformApi.updateSkills(effective.map((s) => ({ key: s.key, enabled: s.enabled }))),
    onSuccess: () => {
      message.success('技能开关已保存')
      setDraft(null)
      queryClient.invalidateQueries({ queryKey: ['ai-skill-registry'] })
    },
    onError: (error: unknown) => message.error(extractAxiosDetail(error) ?? '保存失败'),
  })

  const toggleSkill = (key: string, enabled: boolean) => {
    setDraft((prev) => ({ ...(prev || {}), [key]: enabled }))
  }

  const setCategoryAll = (category: string, enabled: boolean) => {
    setDraft((prev) => {
      const next = { ...(prev || {}) }
      for (const skill of skills) {
        if (skill.category === category) next[skill.key] = enabled
      }
      return next
    })
  }

  const enabledCount = effective.filter((s) => s.enabled).length

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: '120px 0' }}>
        <Spin size="large" tip="加载技能注册表..." />
      </div>
    )
  }

  return (
    <div style={{ padding: '0 0 24px 0' }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 16 }} wrap gap="small">
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>技能管理</Typography.Title>
          <Text type="secondary">
            管理 Ops Agent 可召唤的只读技能 · 已启用 {enabledCount}/{skills.length} 个
            {registry?.updated_at ? ` · 上次更新 ${registry.updated_at}` : ''}
          </Text>
        </div>
        <Space>
          <Input.Search
            style={{ width: 220 }}
            placeholder="搜索技能名称 / 描述"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Button icon={<ReloadOutlined />} onClick={() => { setDraft(null); refetch() }}>重置</Button>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            disabled={!canManage || !dirty}
            loading={saveMutation.isPending}
            onClick={() => saveMutation.mutate()}
          >
            保存{dirty ? '（有未保存修改）' : ''}
          </Button>
        </Space>
      </Flex>

      {!canManage && (
        <Text type="warning" style={{ display: 'block', marginBottom: 12 }}>
          当前角色只有查看权限，修改开关需「Skill 管理写操作」权限。
        </Text>
      )}

      {byCategory.length === 0 && <Empty description="没有匹配的技能" style={{ padding: '80px 0' }} />}

      {byCategory.map(([category, items]) => (
        <Card
          key={category}
          size="small"
          title={
            <Space>
              <Text strong>{category}</Text>
              <Tag>{items.filter((i) => i.enabled).length}/{items.length} 启用</Tag>
            </Space>
          }
          extra={
            canManage && (
              <Space size={4}>
                <Button size="small" type="text" onClick={() => setCategoryAll(category, true)}>全开</Button>
                <Button size="small" type="text" onClick={() => setCategoryAll(category, false)}>全关</Button>
              </Space>
            )
          }
          style={{ marginBottom: 12 }}
        >
          <Row gutter={[12, 12]}>
            {items.map((skill) => (
              <Col xs={24} md={12} xl={8} key={skill.key}>
                <Card
                  size="small"
                  styles={{ body: { padding: 12 } }}
                  style={{ height: '100%', opacity: skill.enabled ? 1 : 0.6 }}
                >
                  <Flex justify="space-between" align="flex-start" gap={8}>
                    <Space direction="vertical" size={2} style={{ flex: 1, minWidth: 0 }}>
                      <Space size={6}>
                        <ThunderboltOutlined style={{ color: skill.enabled ? '#1677ff' : '#bfbfbf' }} />
                        <Text strong ellipsis style={{ maxWidth: 180 }}>{skill.title}</Text>
                        <Tag color={RISK_COLOR[skill.risk_level] || 'default'}>
                          {RISK_LABEL[skill.risk_level] || skill.risk_level}
                        </Tag>
                      </Space>
                      <Text type="secondary" style={{ fontSize: 12 }} ellipsis>{skill.description}</Text>
                      {skill.sample_queries.length > 0 && (
                        <Tooltip title={skill.sample_queries.join('\n')}>
                          <Text code style={{ fontSize: 11 }}>
                            {skill.sample_queries[0]}
                          </Text>
                        </Tooltip>
                      )}
                    </Space>
                    <Switch
                      size="small"
                      checked={skill.enabled}
                      disabled={!canManage}
                      onChange={(checked) => toggleSkill(skill.key, checked)}
                    />
                  </Flex>
                </Card>
              </Col>
            ))}
          </Row>
        </Card>
      ))}
    </div>
  )
}
