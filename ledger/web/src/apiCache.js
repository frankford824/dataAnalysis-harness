/* 读请求的本地副本：先拿上次的结果马上画，再悄悄问服务端变没变。
 *
 * 服务端在后台重算时，一次冷读要二三十秒；切店、切月、回到页面都盯着转圈，不如
 * 先把上次看到的给人。副本存在 IndexedDB 里，启动时整份读进内存，命中时不用等
 * 任何 I/O；浏览器不给用 IndexedDB 时只留内存这一份。
 *
 * 一条副本八秒内跟服务端核对过就不再问；写操作成功后所有副本都算过期。比对看的是
 * 原文，内容没变不通知，所以「核对 → 通知 → 页面重读 → 命中」不会转圈打转。
 */

import { reactive } from 'vue'

const DB_NAME = 'ledger-reads'
const STORE = 'responses'
const MAX_BYTES = 40 * 1024 * 1024
const MAX_ENTRIES = 400
const RECHECK_AFTER = 8000
const PRELOAD_WAIT = 1500
// 同一页的读一般在一两秒内发完；隔得更久的读算下一屏，不再拿来算「数据更新于」。
const VIEW_GAP = 2000

// Map 的顺序就是最近使用顺序，最前面的先被挤掉。
let memory = new Map()
let bytes = 0
let db = null
// askedAt 是最近一次核对的发出时间，拿来跟「过期」时刻比；checkedAt 是回来的时间，拿来管八秒。
const askedAt = new Map()
const checkedAt = new Map()
let staleSince = 0
const listeners = new Set()

export const cacheState = reactive({ shownAt: Date.now(), revalidating: 0 })

function keep(entry) {
  const old = memory.get(entry.url)
  if (old) { bytes -= old.size; memory.delete(entry.url) }
  memory.set(entry.url, entry)
  bytes += entry.size
}

function persist(entry) {
  if (!db) return
  try { db.transaction(STORE, 'readwrite').objectStore(STORE).put(entry) } catch { /* 存不进去就只留内存这份 */ }
}

function forget(url) {
  const old = memory.get(url)
  if (!old) return
  bytes -= old.size
  memory.delete(url)
  if (db) try { db.transaction(STORE, 'readwrite').objectStore(STORE).delete(url) } catch { /* 下次启动再挤掉 */ }
}

function evict() {
  for (const url of memory.keys()) {
    if (bytes <= MAX_BYTES && memory.size <= MAX_ENTRIES) break
    forget(url)
  }
}

function open() {
  return new Promise((resolve) => {
    let request
    try { request = indexedDB.open(DB_NAME, 1) } catch { return resolve() }
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: 'url' })
    request.onerror = request.onblocked = () => resolve()
    request.onsuccess = () => {
      const handle = request.result
      handle.onversionchange = () => { handle.close(); db = null }
      const all = handle.transaction(STORE).objectStore(STORE).getAll()
      all.onsuccess = () => {
        const fetched = memory
        memory = new Map()
        bytes = 0
        for (const entry of all.result.sort((a, b) => a.usedAt - b.usedAt)) if (!fetched.has(entry.url)) keep(entry)
        for (const entry of fetched.values()) keep(entry)
        db = handle
        evict()
        resolve()
      }
      all.onerror = () => { db = handle; resolve() }
    }
  })
}

const ready = typeof indexedDB === 'undefined'
  ? Promise.resolve()
  : Promise.race([open(), new Promise((resolve) => setTimeout(resolve, PRELOAD_WAIT))])

export async function cached(url) {
  if (!memory.has(url)) await ready
  const entry = memory.get(url)
  if (!entry) return null
  memory.delete(url)
  memory.set(url, entry)
  entry.usedAt = Date.now()
  return entry
}

export function asked(url, at) {
  askedAt.set(url, at)
}

export function checked(url) {
  checkedAt.set(url, Date.now())
}

/** 存一份新取回的原文，返回内容是否跟原来的不一样。 */
export function store(url, text, at) {
  const now = Date.now()
  const old = memory.get(url)
  const changed = !old || old.text !== text
  checked(url)
  const size = text.length * 2
  if (size > MAX_BYTES / 4) { forget(url); return changed }
  const entry = { url, text, size, fetchedAt: at, usedAt: now }
  keep(entry)
  evict()
  persist(entry)
  refreshShown()
  return changed
}

export function isStale(url) {
  return (askedAt.get(url) || 0) <= staleSince
}

export function needsCheck(url) {
  return isStale(url) || Date.now() - (checkedAt.get(url) || 0) > RECHECK_AFTER
}

export function markAllStale() {
  staleSince = Date.now()
}

export function onDataChanged(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function dataChanged(url) {
  for (const fn of listeners) fn(url)
}

// 「数据更新于」写的是这一屏用到的副本里最旧那份的取回时间。
let shown = new Set()
let lastServed = 0

function refreshShown() {
  let oldest = Infinity
  for (const url of shown) oldest = Math.min(oldest, memory.get(url)?.fetchedAt ?? Infinity)
  if (oldest < Infinity) cacheState.shownAt = oldest
}

export function served(url) {
  const now = Date.now()
  if (now - lastServed > VIEW_GAP) shown = new Set()
  lastServed = now
  shown.add(url)
  refreshShown()
}

export function newView() {
  shown = new Set()
  lastServed = 0
}
