import { commissionRequest } from './components/commissionRequest'

export const DUTY_OPTIONS = [
  { label: '做货', value: 'produce' },
  { label: '抽点', value: 'cut' },
]

export function dutyLabel(duty) {
  return duty === 'cut' ? '抽点' : duty === 'produce' ? '做货' : ''
}

export function dutyTagType(duty) {
  return duty === 'cut' ? 'warning' : duty === 'produce' ? 'success' : 'default'
}

export async function loadStoreMembers(storeIds, at = '') {
  const ids = [...new Set((storeIds || []).filter(Boolean))]
  if (!ids.length) return []
  const params = new URLSearchParams()
  for (const id of ids) params.append('store_ids', id)
  if (at) params.set('at', at)
  const result = await commissionRequest(`/store-members?${params}`)
  return result.members || []
}

export async function saveStoreMembers(storeIds, members, reason = '设置本店身份', times = {}) {
  const ids = [...new Set((storeIds || []).filter(Boolean))]
  if (!ids.length || !members?.length) return { count: 0, stores: 0 }
  return commissionRequest('/store-members/batch', {
    body: {
      store_ids: ids,
      valid_from: times.valid_from || '',
      valid_to: times.valid_to || '',
      members: members.map(m => ({
        person_id: m.person_id,
        duty: m.duty || 'produce',
        leader_id: m.leader_id || '',
        valid_from: m.valid_from || times.valid_from || '',
        valid_to: m.valid_to || times.valid_to || '',
      })),
      reason,
    },
  })
}
