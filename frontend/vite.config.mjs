// Jira plugin frontend bundle build (Phase 295, R-01).
//
// Mirrors plugins/example/frontend/vite.config.mjs: a plain object export (no
// `import ... from 'vite'` — there is no node_modules under plugins/, so a bare
// config import would not resolve). Library mode, ES format, single
// self-contained entry, no hashed file name, no HTML entry, no emitted CSS.
// Every host singleton in the import map is external so the plugin shares the
// host's React/router/Query/plugin-api instances instead of bundling copies.
export default {
  root: import.meta.dirname,
  build: {
    lib: {
      entry: 'index.jsx',
      formats: ['es'],
      fileName: () => 'jira.js',
    },
    outDir: '../static',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      external: [
        'react',
        'react/jsx-runtime',
        'react-dom',
        'react-dom/client',
        'react-router',
        'react-router-dom',
        '@tanstack/react-query',
        '@indramesh/plugin-api',
      ],
    },
  },
}
