import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { tokenCss } from '../src/design/tokens.js'
const path = fileURLToPath(new URL('../src/design/tokens.css', import.meta.url))
const expected = tokenCss()
if (process.argv.includes('--check')) {
  if (readFileSync(path, 'utf8').replace(/\r\n/g, '\n') !== expected) throw new Error('Design tokens are stale. Run node scripts/design-tokens.mjs.')
} else writeFileSync(path, expected)
