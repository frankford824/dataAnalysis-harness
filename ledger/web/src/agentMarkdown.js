/* 助手回答用的极简 markdown。
 *
 * 先整体转义 HTML，再只认少数几种写法：段落、标题、粗体、斜体、行内代码、链接（外链和左栏页面）、
 * 列表、引用、表格、分隔线。转义在最前面，所以回答里无论夹带什么标签都只会原样
 * 显示成文字，v-html 不会执行任何东西。
 */

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }
export const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g, c => ESC[c])

function inline(text) {
  const codes = []
  let s = text.replace(/`([^`\n]+)`/g, (_, code) => `\u0000${codes.push(code) - 1}\u0000`)
  s = s
    // 「#/路由」的链接是让左边打开某一页，面板拦下点击交给路由，不整页跳转。
    .replace(/\[([^\]\n]+)\]\((#\/[^)\s]*)\)/g, '<a href="$2" class="md-jump" data-route="$2">$1</a>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?![*\w])/g, '$1<em>$2</em>')
    .replace(/~~([^~\n]+?)~~/g, '<del>$1</del>')
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`)
}

const cells = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim())
const isTableRule = row => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(row)
const isTableStart = (lines, i) => lines[i].includes('|') && i + 1 < lines.length && isTableRule(lines[i + 1])
const LIST = /^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/
const FENCE = /^\s*```/
const HEADING = /^\s*(#{1,6})\s+(.*)$/
const QUOTE = /^\s*&gt;\s?/
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/

// 入参是已经转义过的行。
function blocks(lines) {
  const out = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) { i++; continue }

    if (FENCE.test(line)) {
      const body = []
      i++
      while (i < lines.length && !FENCE.test(lines[i])) body.push(lines[i++])
      i++
      out.push(`<pre><code>${body.join('\n')}</code></pre>`)
      continue
    }

    const heading = line.match(HEADING)
    if (heading) {
      const level = Math.min(6, heading[1].length + 2)
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`)
      i++
      continue
    }

    if (RULE.test(line)) { out.push('<hr>'); i++; continue }

    if (isTableStart(lines, i)) {
      const head = cells(line)
      const align = cells(lines[i + 1]).map(c => (c.endsWith(':') ? (c.startsWith(':') ? 'center' : 'right') : ''))
      const cell = (tag, c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ''}>${inline(c)}</${tag}>`
      i += 2
      const rows = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]))
      out.push(
        `<div class="md-table"><table><thead><tr>${head.map((c, k) => cell('th', c, k)).join('')}</tr></thead>` +
          `<tbody>${rows.map(r => `<tr>${head.map((_, k) => cell('td', r[k] ?? '', k)).join('')}</tr>`).join('')}</tbody></table></div>`,
      )
      continue
    }

    if (QUOTE.test(line)) {
      const body = []
      while (i < lines.length && QUOTE.test(lines[i])) body.push(lines[i++].replace(QUOTE, ''))
      out.push(`<blockquote>${blocks(body)}</blockquote>`)
      continue
    }

    const first = line.match(LIST)
    if (first) {
      const ordered = !!first[1]
      const items = []
      while (i < lines.length) {
        const m = lines[i].match(LIST)
        if (m && !!m[1] === ordered) { items.push(m[2]); i++; continue }
        // 缩进的续行并入上一项。
        if (items.length && /^\s{2,}\S/.test(lines[i]) && !m) { items[items.length - 1] += ` ${lines[i].trim()}`; i++; continue }
        break
      }
      const tag = ordered ? 'ol' : 'ul'
      const start = ordered && first[1] !== '1' ? ` start="${Number(first[1])}"` : ''
      out.push(`<${tag}${start}>${items.map(t => `<li>${inline(t)}</li>`).join('')}</${tag}>`)
      continue
    }

    const para = [lines[i++].trim()]
    while (
      i < lines.length && lines[i].trim() &&
      !FENCE.test(lines[i]) && !HEADING.test(lines[i]) && !QUOTE.test(lines[i]) &&
      !LIST.test(lines[i]) && !RULE.test(lines[i]) && !isTableStart(lines, i)
    ) para.push(lines[i++].trim())
    out.push(`<p>${para.map(inline).join('<br>')}</p>`)
  }
  return out.join('')
}

/** caret: 流式输出时在最后一个字后面放一个闪烁的光标。 */
export function renderMarkdown(source, { caret = false } = {}) {
  const lines = escapeHtml(String(source ?? '').replace(/\u0000/g, '')).replace(/\r\n?/g, '\n').split('\n')
  let html = blocks(lines)
  if (caret) {
    const mark = '<span class="md-caret" aria-hidden="true"></span>'
    html = html ? html.replace(/((?:<\/[a-z0-9]+>)*)$/, `${mark}$1`) : mark
  }
  return html
}
