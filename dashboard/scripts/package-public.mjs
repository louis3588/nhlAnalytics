// Data-only deployment: reuse the explicitly pinned, already tested frontend.
import { cp, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { publicBuild } from './public-build.mjs';
const template = process.env.NHL_PUBLIC_FRONTEND_TEMPLATE, source = process.env.NHL_PUBLICATION_SOURCE, output = process.env.NHL_PUBLIC_BUILD_OUTPUT;
if (!template || !source || !output || path.resolve(output) === path.resolve(template)) throw new Error('Isolated template, snapshot and output required');
await mkdir(output, { recursive: true });
await cp(path.join(template, 'index.html'), path.join(output, 'index.html'));
await cp(path.join(template, 'assets'), path.join(output, 'assets'), { recursive: true });
await publicBuild(process.cwd(), source, output, template).closeBundle();
