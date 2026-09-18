export const CARD_W = 188
export const CARD_H = 68
export const H_GAP = 72
export const V_GAP = 18
export const ROOT_GAP = 40

export function layoutTree(roots, collapsed = {}) {
  const boxes = []
  const edges = []
  const visible = node => collapsed[node.id] ? [] : (node.children || [])

  function measure(node) {
    const kids = visible(node)
    if (!kids.length) {
      node._h = CARD_H
      return CARD_H
    }
    let h = 0
    for (const child of kids) h += measure(child) + V_GAP
    node._h = Math.max(CARD_H, h - V_GAP)
    return node._h
  }

  function place(node, x, y, parent) {
    node._x = x
    node._y = y + (node._h - CARD_H) / 2
    boxes.push({ id: node.id, node, x: node._x, y: node._y, w: CARD_W, h: CARD_H, parentId: parent?.id || '' })
    if (parent) {
      edges.push({
        from: { x: parent._x + CARD_W, y: parent._y + CARD_H / 2 },
        to: { x: node._x, y: node._y + CARD_H / 2 },
      })
    }
    let cy = y
    for (const child of visible(node)) {
      place(child, x + CARD_W + H_GAP, cy, node)
      cy += child._h + V_GAP
    }
  }

  let y = 24
  for (const root of roots) {
    measure(root)
    place(root, 24, y, null)
    y += root._h + ROOT_GAP
  }
  return { boxes, edges }
}
