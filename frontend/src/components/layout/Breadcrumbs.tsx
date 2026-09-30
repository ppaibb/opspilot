import { Breadcrumb } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { resolveBreadcrumb } from './menuConfig'

/** 顶栏面包屑：根据当前路由自动生成层级 */
export default function Breadcrumbs() {
  const location = useLocation()
  const navigate = useNavigate()
  const crumbs = resolveBreadcrumb(location.pathname)

  return (
    <Breadcrumb
      items={crumbs.map((c) => ({
        title: c.path ? <a onClick={() => navigate(c.path!)}>{c.label}</a> : c.label,
      }))}
    />
  )
}
