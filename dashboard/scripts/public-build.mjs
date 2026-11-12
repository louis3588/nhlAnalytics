import { readFile, readdir, mkdir, cp, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';

export async function verifyRelease(root, id) {
  if (!/^[a-f0-9]{64}$/.test(id)) throw new Error('Invalid public release ID');
  const folder = path.join(root, 'releases', id), manifest = JSON.parse(await readFile(path.join(folder, 'manifest.json'), 'utf8'));
  if (manifest.schema !== 1 || manifest.release_id !== id) throw new Error('Invalid public manifest');
  for (const [name, info] of Object.entries(manifest.files)) {
    if (!/^(players\/\d+|metadata|catalog|players|intervals|fixtures|standings|prospective)\.json$/.test(name)) throw new Error('Unexpected public file');
    const bytes = await readFile(path.join(folder, name));
    if (bytes.length !== info.bytes || createHash('sha256').update(bytes).digest('hex') !== info.sha256) throw new Error(`Corrupt public file: ${name}`);
  }
  return manifest;
}

export function publicBuild(root, sourceOverride, outputOverride, template) {
  return { name: 'reviewed-public-snapshot', async closeBundle() {
    const source = sourceOverride || path.join(root, 'public/publication'), output = outputOverride || path.join(root, 'dist-public');
    const pointer = JSON.parse(await readFile(path.join(source, 'latest.json'), 'utf8'));
    await verifyRelease(source, pointer.release_id);
    const previous = (await readdir(path.join(source, 'releases'))).filter(id => /^[a-f0-9]{64}$/.test(id) && id !== pointer.release_id);
    // Preserve two prior immutable releases for tabs open during an update.
    const { stat } = await import('node:fs/promises');
    const times = await Promise.all(previous.map(async id => [id, (await stat(path.join(source, 'releases', id))).mtimeMs]));
    const known = JSON.parse(process.env.NHL_PUBLIC_PREVIOUS_RELEASES || '[]').filter(id => previous.includes(id));
    const recent = times.sort((a, b) => b[1] - a[1]).map(r => r[0]);
    const selected = [pointer.release_id, ...[...new Set([...known, ...recent])].slice(0, 2)];
    for (const id of selected) {
      await verifyRelease(source, id);
      const manifest = JSON.parse(await readFile(path.join(source, 'releases', id, 'manifest.json'), 'utf8'));
      const destination = path.join(output, 'publication/releases', id);
      await mkdir(destination, { recursive: true });
      // Copy only allowlisted JSON, not research files or compression sidecars.
      for (const name of ['manifest.json', ...Object.keys(manifest.files)]) {
        await mkdir(path.dirname(path.join(destination, name)), { recursive: true });
        await cp(path.join(source, 'releases', id, name), path.join(destination, name));
      }
    }
    await cp(path.join(source, 'latest.json'), path.join(output, 'publication/latest.json'));
    const display = template || path.join(root, 'public');
    for (const name of ['rink.svg', 'inter-LICENSE.txt']) await cp(path.join(display, name), path.join(output, name));
    await mkdir(path.join(output, 'team-logos'), { recursive: true });
    for (const entry of await readdir(path.join(display, 'team-logos'), { withFileTypes: true })) {
      if (entry.isFile() && /^\d+(?:-\d{8})?\.svg$/.test(entry.name)) await cp(path.join(display, 'team-logos', entry.name), path.join(output, 'team-logos', entry.name));
    }
    // Pages enables its native SPA fallback only without a top-level 404.html.
    await cp(template ? path.join(template, '_headers') : path.join(root, 'cloudflare/_headers'), path.join(output, '_headers'));
    await writeFile(path.join(output, 'publication/README.txt'), 'Public presentation data only. The local engine, database and live collector are not deployed.\n');
    let count = 0;
    async function audit(directory, prefix = '') {
      for (const entry of await readdir(directory, { withFileTypes: true })) {
        if (entry.isSymbolicLink()) throw new Error('Symlink in public build');
        const relative = prefix + entry.name, full = path.join(directory, entry.name);
        if (entry.isDirectory()) { await audit(full, `${relative}/`); continue; }
        if (!/^(?:index\.html|404\.html|_headers|_redirects|rink\.svg|inter-LICENSE\.txt|assets\/[^/]+\.(?:js|css|woff2)|team-logos\/\d+(?:-\d{8})?\.svg|publication\/(?:README\.txt|latest\.json|releases\/[a-f0-9]{64}\/(?:manifest\.json|(?:players\/\d+|metadata|catalog|players|intervals|fixtures|standings|prospective)\.json)))$/.test(relative)) throw new Error(`Unexpected public asset: ${relative}`);
        if ((await stat(full)).size > 25 * 1024 * 1024 || ++count > 20000) throw new Error('Cloudflare static asset budget exceeded');
        if (/\.(js|html|json)$/.test(relative) && /(?:localhost|127\.0\.0\.1):8000|[A-Z]:\\\\Users\\\\|postgres(?:ql)?:\/\//i.test(await readFile(full, 'utf8'))) throw new Error('Private endpoint/path in public build');
      }
    }
    await audit(output);
    console.log(`Public snapshot ${pointer.release_id.slice(0, 12)} packaged; no Python service required.`);
  } };
}
