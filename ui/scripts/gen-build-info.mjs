/** Writes src/build-info.json at build time (prebuild). */
import { execSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const git = (cmd, fallback) => {
  try {
    return execSync(cmd, { cwd: root, stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim()
  } catch {
    return fallback
  }
}

const info = {
  sha: process.env.BUILD_SHA || git('git rev-parse --short HEAD', 'unknown'),
  branch: process.env.BUILD_BRANCH || git('git rev-parse --abbrev-ref HEAD', ''),
  date: new Date().toISOString(),
}
fs.writeFileSync(path.join(root, 'src/build-info.json'), JSON.stringify(info, null, 2) + '\n')
console.log(`build-info: ${info.sha} ${info.date}`)
