import test from 'node:test'
import assert from 'node:assert/strict'
import { CARD_H, CARD_W, H_GAP, layoutTree } from '../src/orgLayout.js'

test('horizontal tidy tree places children to the right and centers the parent', () => {
  const tree = [{
    id: 'lead', name: '宋永康', children: [
      { id: 'a', name: '宗玲', children: [] },
      { id: 'b', name: '吴娟', children: [] },
    ],
  }]
  const { boxes, edges } = layoutTree(tree)
  const byId = Object.fromEntries(boxes.map(box => [box.id, box]))
  assert.equal(boxes.length, 3)
  assert.equal(edges.length, 2)
  assert.equal(byId.a.x, byId.lead.x + CARD_W + H_GAP)
  assert.equal(byId.b.x, byId.a.x)
  assert.ok(byId.a.y < byId.lead.y)
  assert.ok(byId.b.y > byId.lead.y)
  assert.equal(Math.round(byId.lead.y + CARD_H / 2), Math.round((byId.a.y + byId.b.y + CARD_H) / 2))
})

test('collapsed parent hides descendants from the canvas boxes', () => {
  const tree = [{ id: 'lead', children: [{ id: 'a', children: [{ id: 'b', children: [] }] }] }]
  const { boxes } = layoutTree(tree, { lead: true })
  assert.deepEqual(boxes.map(box => box.id), ['lead'])
})
