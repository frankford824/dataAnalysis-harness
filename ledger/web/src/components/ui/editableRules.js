export function mergeRuleResponse(current, draft, incoming, force = false) {
  const dirty = !!current?.rules && JSON.stringify(draft) !== JSON.stringify(current.rules)
  return {
    data: { ...(current || {}), ...incoming },
    draft: incoming.rules && (!dirty || force) ? JSON.parse(JSON.stringify(incoming.rules)) : draft,
  }
}

export function feePreviewKey(rules, store) { return JSON.stringify({rules,store}) }
