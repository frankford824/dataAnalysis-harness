// Local reading groups contain identifiers only; no accounting data or commands.
export function createReviewGroups(identifiers, scope) {
  let ids = [...new Set(identifiers)], currentScope = scope, undo = [], redo = []
  let membership = Object.fromEntries(ids.map(id => [id, null]))
  const validGroup = group => group === null || group === 'A' || group === 'B'
  const copy = () => ({ ...membership })
  function apply(next) {
    if (!ids.every(id => id in next && validGroup(next[id]))) return false
    if (ids.every(id => membership[id] === next[id])) return false
    undo.push(copy()); undo = undo.slice(-24); redo = []; membership = { ...next }; return true
  }
  return {
    get scope() { return currentScope },
    get membership() { return copy() },
    get canUndo() { return undo.length > 0 },
    get canRedo() { return redo.length > 0 },
    reset(nextIds, nextScope) {
      ids = [...new Set(nextIds)]; currentScope = nextScope
      membership = Object.fromEntries(ids.map(id => [id, null])); undo = []; redo = []
    },
    move(selected, group) {
      if (!validGroup(group) || !selected.length || !selected.every(id => ids.includes(id))) return false
      const next = copy(); selected.forEach(id => { next[id] = group }); return apply(next)
    },
    merge(from, into) {
      if (!['A', 'B'].includes(from) || !['A', 'B'].includes(into) || from === into) return false
      const next = copy(); ids.forEach(id => { if (next[id] === from) next[id] = into }); return apply(next)
    },
    split(group) {
      if (!['A', 'B'].includes(group)) return false
      const next = copy(); ids.forEach(id => { if (next[id] === group) next[id] = null }); return apply(next)
    },
    undo() { if (!undo.length) return false; redo.push(copy()); membership = undo.pop(); return true },
    redo() { if (!redo.length) return false; undo.push(copy()); membership = redo.pop(); return true },
  }
}
