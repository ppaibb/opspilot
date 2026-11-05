import { useState } from 'react'
import { Alert, App, Button, Drawer, Flex, Form, Select, Table } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { credentialsApi, domainsApi } from '@/api'
import { extractAxiosDetail } from '@/hooks/usePermission'
import type { DomainProviderItem } from '@/types/api'
import { credentialMatches } from './providerForm'

/** 云厂商选择、凭证引用及纳管；不会在页面读取已保存的密钥。 */
export default function CloudDomainImportDrawer({ providers, onClose }: {
  providers: DomainProviderItem[]; onClose: () => void
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [provider, setProvider] = useState(providers.find((item) => item.key === 'huawei')?.key ?? providers[0]?.key ?? '')
  const [credentialId, setCredentialId] = useState('')
  const [scanRequested, setScanRequested] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [failures, setFailures] = useState<string[]>([])
  const spec = providers.find((item) => item.key === provider)
  const { data: credentials = [] } = useQuery({ queryKey: ['credentials'], queryFn: credentialsApi.list })
  const candidates = useQuery({
    queryKey: ['domain-candidates', provider, credentialId],
    queryFn: () => domainsApi.accountDomains({ provider, account_id: credentialId }),
    enabled: scanRequested && !!provider && !!credentialId,
    retry: false,
  })
  const importMutation = useMutation({
    mutationFn: async () => {
      const failed: string[] = [], errors: string[] = []
      let count = 0
      // 串行导入避免打满云API；部分成功必须保留失败项，不能整体提示成功。
      for (const domain of selected) {
        try {
          await domainsApi.importDomain({ provider, account_id: credentialId, domain })
          count += 1
        } catch (error: unknown) {
          failed.push(domain)
          errors.push(`${domain}：${extractAxiosDetail(error) ?? '导入失败'}`)
        }
      }
      return { count, failed, errors }
    },
    onSuccess: ({ count, failed, errors }) => {
      queryClient.invalidateQueries({ queryKey: ['domains'] })
      queryClient.invalidateQueries({ queryKey: ['domain-candidates'] })
      setSelected(failed)
      setFailures(errors)
      if (failed.length) message.warning(`已导入 ${count} 个域名，${failed.length} 个失败`)
      else {
        message.success(`已导入 ${count} 个域名`)
        onClose()
      }
    },
  })
  const resetSelection = () => {
    setScanRequested(false)
    setSelected([])
    setFailures([])
  }
  return (
    <Drawer title="导入云域名" open width={760} onClose={onClose} closable={!importMutation.isPending}
      maskClosable={!importMutation.isPending}
      extra={<Button type="primary" disabled={!selected.length} loading={importMutation.isPending}
        onClick={() => importMutation.mutate()}>导入 {selected.length} 个域名</Button>}
    >
      <Flex vertical gap={16}>
        <Form layout="vertical" disabled={importMutation.isPending}>
          <Form.Item label="云厂商" htmlFor="domain-import-provider">
            <Select id="domain-import-provider" value={provider} options={providers.filter((item) => item.supports_import)
              .map((item) => ({ value: item.key, label: item.label }))}
              onChange={(value) => { setProvider(value); setCredentialId(''); resetSelection() }} />
          </Form.Item>
          <Form.Item label="已保存凭证" htmlFor="domain-import-credential"
            extra={spec?.credential_types?.length ? `所需凭证类型：${spec.credential_types.join(' / ')}` : undefined}>
            <Select id="domain-import-credential" value={credentialId || undefined} placeholder="选择凭证"
              options={credentials.filter((item) => item.status !== 'expired' && credentialMatches(item.type, spec?.credential_types ?? []))
                .map((item) => ({ value: item.cred_id, label: item.name }))}
              onChange={(value) => { setCredentialId(value); resetSelection() }} />
          </Form.Item>
          <Button disabled={!credentialId} loading={candidates.isFetching} onClick={() => {
            setSelected([])
            if (scanRequested) candidates.refetch()
            else setScanRequested(true)
          }}>获取域名</Button>
        </Form>
        {candidates.isError && <Alert type="error" showIcon message={extractAxiosDetail(candidates.error) ?? '获取域名失败'} />}
        {failures.length > 0 && <Alert type="error" showIcon message="部分域名导入失败" description={failures.join('；')} />}
        {scanRequested && candidates.isSuccess && (
          <Table rowKey="domain" size="small" dataSource={candidates.data.domains}
            rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys as string[]),
              getCheckboxProps: () => ({ disabled: importMutation.isPending }) }}
            columns={[{ title: '域名', dataIndex: 'display_domain' }, { title: '云端 Zone ID', dataIndex: 'zone_id', ellipsis: true }]}
            pagination={{ pageSize: 10 }} locale={{ emptyText: '没有可导入的域名' }} />
        )}
      </Flex>
    </Drawer>
  )
}
