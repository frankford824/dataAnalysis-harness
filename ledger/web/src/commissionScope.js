// An explicit empty URL filter means all stores; only a missing filter inherits the current store.
export function initialStoreIds(query, currentStore) {
  if (Object.hasOwn(query, 'shops')) return String(query.shops || '').split(',').filter(Boolean)
  return currentStore ? [currentStore] : []
}
