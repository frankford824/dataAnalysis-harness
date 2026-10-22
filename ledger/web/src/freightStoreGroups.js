const key='ledger.freight-review.store-groups.v1'
export function readStoreGroups(storage){
  try{
    const value=JSON.parse((storage || globalThis.localStorage).getItem(key) || '[]')
    return Array.isArray(value)?value.filter(g=>g && typeof g.name==='string' && g.name.trim() && Array.isArray(g.store_ids) && g.store_ids.every(id=>typeof id==='string')):[]
  }catch{return []}
}
export function writeStoreGroups(groups,storage=globalThis.localStorage){storage.setItem(key,JSON.stringify(groups))}
