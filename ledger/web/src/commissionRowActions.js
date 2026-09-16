export function reportRowActions(row, flags) {
  const actions = []
  if (flags.profit) actions.push({key: 'profit', label: '利润构成', kind: 'quiet'})
  if (flags.payout) {
    actions.push({
      key: 'payout',
      label: row.status?.includes('已人工确认') ? '修改确认' : '确认提成',
      kind: 'main',
    })
  }
  if (flags.detail) actions.push({key: 'detail', label: '明细', kind: 'quiet'})
  return actions
}
