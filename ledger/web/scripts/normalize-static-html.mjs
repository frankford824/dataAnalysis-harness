import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
// Windows checkouts may contain CRLF. Release HTML is emitted with stable LF bytes.
const file = fileURLToPath(new URL('../../ledger/static/index.html', import.meta.url))
const html = readFileSync(file, 'utf8')
writeFileSync(file, html.replace(/\r\n?/g, '\n'))
