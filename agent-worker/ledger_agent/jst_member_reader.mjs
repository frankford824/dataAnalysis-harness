// Read-only, bounded order evidence. Never imports collectors, refreshes a login,
// scans recent pages, writes a database, or assigns a financial company namespace.
import { createHash } from 'node:crypto';

const BASE = 'https://www.erp321.com';
const LIST = '/app/order/order/list.aspx';
const DETAIL = '/app/order/order/OrderEditorNew.aspx';
const RELATIONS = '/app/order/order/MergeOrder.aspx';
const MAX_BYTES = 16 * 1024 * 1024;
const sha = value => createHash('sha256').update(value).digest('hex');

export class EvidencePending extends Error {
  constructor(code) { super(code); this.name = 'EvidencePending'; this.code = code; }
}
function pending(code) { throw new EvidencePending(code); }
function internalId(value) {
  if (typeof value === 'number' && !Number.isSafeInteger(value)) pending('unsafe_identity_number');
  const id = String(value ?? '');
  if (!/^[1-9][0-9]{0,19}$/.test(id)) pending('invalid_internal_identity');
  return id;
}
function rawHtml(bytes) {
  if (!Buffer.isBuffer(bytes) || bytes.length > MAX_BYTES) pending('invalid_raw_response');
  let html;
  // Preserve a UTF8 BOM as a character so source byte offsets stay exact.
  try { html = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes); }
  catch { pending('invalid_utf8_response'); }
  if (/loginReact|erp-login|无访问权限/.test(html.slice(0, 10000))) pending('session_or_access_unavailable');
  return html;
}

function quotedEnd(text, start) {
  const quote = text[start]; let escaped = false;
  for (let i = start + 1; i < text.length; i++) {
    if (escaped) escaped = false;
    else if (text[i] === '\\') escaped = true;
    else if (text[i] === quote) return i + 1;
  }
  pending('order_data_truncated');
}
function objectEnd(text, start) {
  let depth = 0;
  for (let i = start; i < text.length; i++) {
    if (text[i] === '"') { i = quotedEnd(text, i) - 1; continue; }
    if (text[i] === '{' || text[i] === '[') depth++;
    else if (text[i] === '}' || text[i] === ']') { if (--depth === 0) return i + 1; }
  }
  pending('order_data_truncated');
}
function orderDataSpans(html) {
  const spans = [];
  for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)) {
    const body = script[1], offset = script.index + script[0].indexOf('>') + 1;
    if (!/\b_OrderData\s*=/.test(body)) continue;
    let braceDepth = 0, previous = ';';
    for (let i = 0; i < body.length;) {
      const c = body[i];
      if (c === '"' || c === "'" || c === '`') { i = quotedEnd(body, i); previous = 'quoted'; continue; }
      if (body.startsWith('//', i)) { const end = body.indexOf('\n', i + 2); i = end < 0 ? body.length : end; continue; }
      if (body.startsWith('/*', i)) { const end = body.indexOf('*/', i + 2); if (end < 0) pending('order_data_truncated'); i = end + 2; continue; }
      // This source contract is a JSON assignment, not an arbitrary JS
      // program. Regex/division outside quoted text is unsupported: rejecting
      // it prevents a regex literal from impersonating a data assignment.
      if (c === '/') pending('order_data_unsupported_script_syntax');
      const match = body.slice(i).match(/^_OrderData\b\s*=\s*(\{)/);
      if (match && (i === 0 || !/[\w$.]/.test(body[i - 1]))) {
        const declaration = /(?:^|[;}])\s*(?:var|let|const)\s*$/.test(body.slice(0, i));
        if (braceDepth !== 0 || (!declaration && previous !== ';' && previous !== '}')) pending('order_data_unsupported_script_syntax');
        const start = i + match[0].length - 1, end = objectEnd(body, start);
        spans.push({ start: offset + start, end: offset + end });
        // Skip this complete JSON literal, including all free-text strings.
        i = end; previous = '}'; continue;
      }
      if (c === '{') braceDepth++;
      if (c === '}') braceDepth--;
      if (!/\s/.test(c)) previous = c;
      i++;
    }
  }
  return spans;
}
// JSON.parse discards duplicate keys. Reject ambiguous source objects before
// it can replace an identity, quantity or price with the last occurrence.
function rejectDuplicateKeys(text) {
  let i = 0;
  const space = () => { while (/\s/.test(text[i] ?? '') && i < text.length) i++; };
  function value(depth = 0) {
    if (depth > 64) pending('order_data_too_deep');
    space(); const c = text[i];
    if (c === '"') { i = quotedEnd(text, i); return; }
    if (c === '{') {
      i++; space(); const keys = new Set();
      if (text[i] === '}') { i++; return; }
      while (i < text.length) {
        space(); if (text[i] !== '"') pending('order_data_invalid_json');
        const end = quotedEnd(text, i); let key;
        try { key = JSON.parse(text.slice(i, end)); } catch { pending('order_data_invalid_json'); }
        if (keys.has(key)) pending('order_data_duplicate_key'); keys.add(key); i = end;
        space(); if (text[i++] !== ':') pending('order_data_invalid_json');
        value(depth + 1); space();
        const next = text[i++]; if (next === '}') return;
        if (next !== ',') pending('order_data_invalid_json');
      }
      pending('order_data_truncated');
    }
    if (c === '[') {
      i++; space(); if (text[i] === ']') { i++; return; }
      while (i < text.length) {
        value(depth + 1); space(); const next = text[i++];
        if (next === ']') return; if (next !== ',') pending('order_data_invalid_json');
      }
      pending('order_data_truncated');
    }
    const start = i; while (i < text.length && !/[\s,}\]]/.test(text[i])) i++;
    if (i === start) pending('order_data_invalid_json');
  }
  value(); space(); if (i !== text.length) pending('order_data_invalid_json');
}

/** Keep the exact JSON lexeme. `data` is for identities/structure only; quantities
 * and prices must be parsed independently from json_text with Decimal. */
export function parseOrderData(bytes, expectedId) {
  const expected = internalId(expectedId), html = rawHtml(bytes);
  const spans = orderDataSpans(html);
  if (spans.length !== 1) pending('order_data_schema_changed');
  const { start, end } = spans[0];
  const jsonText = html.slice(start, end);
  rejectDuplicateKeys(jsonText);
  let data;
  try { data = JSON.parse(jsonText); } catch { pending('order_data_invalid_json'); }
  if (internalId(data.o_id) !== expected) pending('detail_identity_mismatch');
  if (!Array.isArray(data.items)) pending('incomplete_items');
  for (const item of data.items) {
    if (!item || internalId(item.o_id) !== expected) pending('item_identity_mismatch');
  }
  return {
    schema: 'jst_readonly_order_raw.v1', raw_sha256: sha(bytes),
    order_data: { start_byte: Buffer.byteLength(html.slice(0, start)),
      end_byte: Buffer.byteLength(html.slice(0, end)), sha256: sha(Buffer.from(jsonText)) },
    json_text: jsonText, data,
    interpretation: 'raw_source_observation_not_financial_approval',
  };
}

/** This page is a related-order set, not a directed membership graph. A caller
 * must prove direction from typed detail fields, source item identity and qty. */
export function parseRelatedOrders(bytes) {
  const html = rawHtml(bytes), links = [];
  const spans = [...html.matchAll(/<span\b[^>]*onclick="ShowOrder\(([^)]*)\)"[^>]*>([^<]*)<\/span>/g)];
  for (const match of spans) {
    const id = internalId(match[1].trim());
    links.push({ internal_order_id: id,
      source_field: 'MergeOrder.aspx:span.onclick.ShowOrder',
      start_byte: Buffer.byteLength(html.slice(0, match.index)),
      end_byte: Buffer.byteLength(html.slice(0, match.index + match[0].length)),
      span_sha256: sha(Buffer.from(match[0])) });
  }
  const renderedCalls = [...html.matchAll(/onclick="ShowOrder\(/g)].length;
  if (!links.length || links.length !== renderedCalls) pending('relations_schema_changed');
  if (links.length > 30) pending('relation_scope_exceeds_limit');
  return { schema: 'jst_readonly_relations_raw.v1', raw_sha256: sha(bytes), links,
    interpretation: 'related_order_listing_not_directed_membership' };
}

export class MemberEvidenceReader {
  constructor({ jst, fetchImpl = globalThis.fetch }) {
    if (!jst || typeof jst.callback !== 'function' || typeof fetchImpl !== 'function') throw new TypeError('Read-only transport required');
    // This is a dedicated client instance. Auth failure is evidence_pending;
    // it must never silently trigger the collector's login/config writes.
    this.jst = jst;
    this.jst.freshenSession = async () => pending('session_refresh_required');
    this.fetch = fetchImpl;
  }

  async lookupInternal(value, { archive = false } = {}) {
    const id = internalId(value);
    if (typeof archive !== 'boolean') pending('invalid_query_scope');
    const where = [{ k: 'o_id', v: id, c: '=' }];
    let rv;
    try {
      rv = await this.jst.callback(LIST, 'LoadDataToJSON', ['2', JSON.stringify(where), '{}'], {
        owner_co_id: String(this.jst.coid), _jt_page_count_enabled: 'false',
        _jt_page_increament_enabled: 'true', _jt_page_increament_page_mode: 'next',
        _jt_page_increament_key_value: '', _jt_page_increament_business_values: '',
        _jt_page_increament_key_name: 'o_id', _jt_page_size: '20', _jt_page_action: '1',
      }, { query: archive ? { _c: 'jst-epaas', archive: 'true', no_data: 'true' } : { _c: 'jst-epaas' } });
    } catch (e) { if (e instanceof EvidencePending) throw e; pending('order_query_unavailable'); }
    if (!rv || !Array.isArray(rv.datas)) pending('query_schema_changed');
    const rows = rv.datas;
    if (!rows.length) return { state: 'pending', reason: 'not_found_in_this_query_scope', query_scope: archive ? 'archive' : 'current', rows: [] };
    if (rows.some(row => !row || typeof row !== 'object' || Array.isArray(row))) pending('query_schema_changed');
    if (rows.some(row => internalId(row.o_id) !== id)) pending('query_filter_not_enforced');
    if (rows.length !== 1) pending('ambiguous_internal_identity');
    if (typeof rows[0].o_id_en !== 'string' || !rows[0].o_id_en.trim()) pending('encrypted_identity_unavailable');
    return { state: 'found', query_scope: archive ? 'archive' : 'current',
      filter: { field: 'o_id', operator: '=', argument_location: 'JTable.Args[1]' }, rows };
  }

  async #get(path, query, { expanded = false } = {}) {
    const url = new URL(path, BASE);
    for (const [k, v] of Object.entries(query)) url.searchParams.set(k, String(v));
    const cookie = expanded ? this.jst.cookie.split(';').filter(s => !s.trim().startsWith('combine_show=')).join(';') + '; combine_show=false' : this.jst.cookie;
    let response, bytes;
    try {
      response = await this.fetch(url.href, { method: 'GET', redirect: 'error',
        headers: { Cookie: cookie, 'User-Agent': this.jst.ua, Accept: 'text/html', Referer: BASE + LIST + '?_c=jst-epaas' },
        signal: AbortSignal.timeout(25000) });
      bytes = Buffer.from(await response.arrayBuffer());
    } catch { pending('source_transport_unavailable'); }
    if (response.status === 405 || response.status === 429) pending('source_rate_limited');
    if (response.status !== 200) pending('source_http_unavailable');
    rawHtml(bytes);
    return bytes;
  }

  async readDetail(row, { archive = false } = {}) {
    const id = internalId(row?.o_id);
    if (typeof row.o_id_en !== 'string' || !row.o_id_en.trim()) pending('encrypted_identity_unavailable');
    if (typeof archive !== 'boolean') pending('invalid_query_scope');
    const bytes = await this.#get(DETAIL, { order_id: row.o_id_en, archive: archive ? '1' : '0',
      owner_co_id: this.jst.coid, fromList: 'true' }, { expanded: true });
    return { ...parseOrderData(bytes, id), raw_bytes: bytes, expanded_combinations_requested: true };
  }

  async readRelations(value, { archive = false } = {}) {
    const id = internalId(value);
    if (typeof archive !== 'boolean') pending('invalid_query_scope');
    const bytes = await this.#get(RELATIONS, { id, archive: String(archive) });
    return { ...parseRelatedOrders(bytes), raw_bytes: bytes };
  }
}
