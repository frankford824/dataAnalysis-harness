// Reconcile UI drafts after a neighboring section refreshes the same record.
// This module never constructs a write payload or computes a business amount.
const clone = value => JSON.parse(JSON.stringify(value))
function equivalent(current, incoming, field = '') {
  // Match only the fields the existing person endpoint trims. Notes and employee
  // numbers retain their exact input, including meaningful whitespace/leading zeros.
  if (typeof current === 'string' && typeof incoming === 'string' && ['name','alias','parent_id'].includes(field)) return current.trim() === incoming.trim()
  if (typeof incoming === 'number' && typeof current === 'string' && current.trim() !== '') return Number(current) === incoming
  if (current == null || incoming == null) return (current ?? '') === (incoming ?? '')
  if (typeof current !== 'object' || typeof incoming !== 'object') return current === incoming
  const keys = [...new Set([...Object.keys(current), ...Object.keys(incoming)])].filter(key => key !== 'revision')
  return keys.every(key => equivalent(current[key], incoming[key], key))
}
export function reconcileOrgDraft(current, original, incoming, samePerson) {
  const untouched = JSON.stringify(current) === JSON.stringify(original)
  const accepted = !samePerson || untouched || equivalent(current, incoming)
  return accepted
    ? { draft: clone(incoming), original: clone(incoming), accepted: true }
    : { draft: clone(current), original: clone(original), accepted: false }
}
