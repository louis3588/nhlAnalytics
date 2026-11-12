import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { publicBuild } from './scripts/public-build.mjs';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const isPublic = mode === 'public';
  const source = process.env.NHL_PUBLICATION_SOURCE, output = process.env.NHL_PUBLIC_BUILD_OUTPUT;
  if (isPublic && Boolean(source) !== Boolean(output)) throw new Error('Isolated publication source and output must be configured together');
  if (isPublic && env.VITE_DATA_MODE !== 'static') throw new Error('Public builds require VITE_DATA_MODE=static');
  const proxy = { '/api': { target: env.API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true } };
  return { plugins: [react(), ...(isPublic ? [publicBuild(process.cwd(), source, output)] : [])],
    envPrefix: 'VITE_DATA_MODE',
    publicDir: isPublic ? false : 'public', build: { outDir: isPublic ? output || 'dist-public' : 'dist', emptyOutDir: !output, sourcemap: false },
    server: { port: 5173, strictPort: true, proxy }, preview: { port: 4173, strictPort: true, ...(isPublic ? {} : { proxy }) } };
});
