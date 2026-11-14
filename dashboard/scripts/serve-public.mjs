// Small local-only preview: built-in Node modules, no Vite/API/database runtime.
import { createServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
const root = path.resolve(process.env.NHL_PUBLIC_BUILD_OUTPUT || 'dist-public');
const portFlag = process.argv.indexOf('--port');
const port = portFlag < 0 ? 4174 : Number(process.argv[portFlag + 1]);
const rules = [];
for (const line of (await readFile(path.join(root, '_headers'), 'utf8')).split('\n')) {
  if (!line.trim()) continue;
  if (!/^\s/.test(line)) rules.push({ pattern: line.trim(), headers: {} });
  else { const colon = line.indexOf(':'); if (colon > 0) rules.at(-1).headers[line.slice(0, colon).trim()] = line.slice(colon + 1).trim(); }
}
const matches = (pattern, url) => pattern.endsWith('*') ? url.startsWith(pattern.slice(0, -1)) : pattern === url;
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.txt': 'text/plain; charset=utf-8' };
createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
    let relative = pathname === '/' ? '/index.html' : pathname, statusCode = 200;
    let full = path.resolve(root, `.${relative}`);
    if (!full.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
    try { if (!(await stat(full)).isFile()) throw new Error('Not a file'); }
    catch {
      // Match Pages' native SPA fallback. Missing JSON is rejected by the
      // client's JSON/schema/hash checks, even when HTML receives HTTP 200.
      relative = '/index.html'; statusCode = 200;
      full = path.join(root, relative);
    }
    const headers = Object.assign({}, ...rules.filter(rule => matches(rule.pattern, pathname)).map(rule => rule.headers));
    response.writeHead(statusCode, { ...headers, 'Content-Type': types[path.extname(full)] || 'application/octet-stream', 'Content-Length': (await stat(full)).size });
    if (request.method === 'HEAD') response.end(); else createReadStream(full).pipe(response);
  } catch { response.writeHead(500).end('Static preview unavailable'); }
}).listen(port, '127.0.0.1', () => console.log(`Static-only preview: http://127.0.0.1:${port}`));
