export function sameSettlementScope(left = {}, right = {}) {
  const same = (a = [], b = []) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort())
  return left.start === right.start && left.end === right.end &&
    same(left.store_ids, right.store_ids) && same(left.person_ids, right.person_ids)
}

export function settlementDifference(current, settled) {
  if (current == null || settled == null) return null
  return Number((Number(current) - Number(settled)).toFixed(2))
}
