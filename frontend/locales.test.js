// Phase 295 (JUI-07): the plugin's own catalogs must stay key-identical so every
// Jira task-form string resolves in every shipped language. The host falls back
// to English for a language the plugin does not ship, but a missing or empty
// value in a shipped language is still a copy defect. Reads the JSON as data
// (not source) via fs.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const localesDir = join(here, 'locales')
const en = JSON.parse(readFileSync(join(localesDir, 'en.json'), 'utf8'))
const shipped = readdirSync(localesDir)
  .filter((file) => file.endsWith('.json'))
  .sort()

test('every shipped locale declares exactly the English key set', () => {
  assert.ok(shipped.includes('en.json'), 'en.json must exist')
  for (const file of shipped) {
    const bundle = JSON.parse(readFileSync(join(localesDir, file), 'utf8'))
    assert.deepEqual(Object.keys(bundle).sort(), Object.keys(en).sort(), file)
  }
})

test('every locale value is a non-empty string', () => {
  for (const file of shipped) {
    const lang = file.replace(/\.json$/, '')
    const bundle = JSON.parse(readFileSync(join(localesDir, file), 'utf8'))
    for (const [key, value] of Object.entries(bundle)) {
      assert.equal(typeof value, 'string', `${lang}.${key} must be a string`)
      assert.ok(value.length > 0, `${lang}.${key} must be non-empty`)
    }
  }
})

