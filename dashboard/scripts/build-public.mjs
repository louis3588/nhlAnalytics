// Keep packaging predictable on the 8 GB development machine.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const cwd = fileURLToPath(new URL('../', import.meta.url));
const result = spawnSync(process.execPath, ['--max-old-space-size=128', '--max-semi-space-size=2', path.join(cwd, 'node_modules/vite/bin/vite.js'), 'build', '--mode', 'public'], {
  cwd, stdio: 'inherit', env: { ...process.env, RAYON_NUM_THREADS: '1', UV_THREADPOOL_SIZE: '1' },
});
if (result.error) console.error('Public build could not start:', result.error.message);
process.exitCode = result.status ?? 1;
