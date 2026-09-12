import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from 'tailwindcss';
import autoprefixer from 'autoprefixer';

// PostCSS is configured inline here; there is intentionally no postcss.config.js.

// Where `npm run dev` proxies /api and /health. Defaults to the documented backend port; set
// VITE_API_TARGET to point the dev server at a backend running on a different port, e.g.
//   VITE_API_TARGET=http://localhost:18907 npm run dev -- --port 5187
const apiTarget = process.env.VITE_API_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  css: {
    postcss: {
      plugins: [tailwindcss(), autoprefixer()],
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': apiTarget,
      '/health': apiTarget,
    },
  },
});
