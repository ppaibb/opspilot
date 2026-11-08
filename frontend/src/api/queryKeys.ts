/** 跨页面共享的查询键工厂；all 键用于 mutation 精确失效整个资源域。 */
export const queryKeys = {
  assets: {
    all: ['assets-all'] as const,
    list: (params?: unknown) => ['assets-all', 'list', params] as const,
    detail: (id: string) => ['assets-all', 'detail', id] as const,
  },
  applications: {
    all: ['applications-all'] as const,
    list: (params?: unknown) => ['applications-all', 'list', params] as const,
    detail: (id: string) => ['applications-all', 'detail', id] as const,
  },
  ai: {
    sessions: ['ai-sessions-all'] as const,
    skills: ['ai-skills-all'] as const,
  },
  logs: {
    all: ['monitoring-logs-all'] as const,
    query: (params: unknown) => ['monitoring-logs-all', params] as const,
  },
} as const
