import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { VitePWA } from 'vite-plugin-pwa';

// Bind to 0.0.0.0 and accept any Host header so a cloudflared tunnel can reach
// dev/preview. iOS gives you no OPFS, no service worker and no home-screen
// install without real HTTPS, so the tunnel is not optional.
// Everything goes through one origin: the app and /api both come from Vite, so
// there is no CORS and a cloudflared tunnel in front works without the client
// knowing anything about it. That matters because the phone is tested through
// exactly such a tunnel.
const net = {
  host: true,
  allowedHosts: true,
  proxy: {
    '/api': {
      target: 'http://127.0.0.1:8000',
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/api/, ''),
    },
  },
};

export default defineConfig({
  // Stamped into the readiness panel. A service worker keeps serving the
  // previous shell until a second load, so "which build am I actually running"
  // is a real question on a phone you cannot attach a debugger to.
  define: { __BUILD__: JSON.stringify(new Date().toISOString().slice(0, 19) + 'Z') },
  plugins: [
    svelte(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icon-192.png', 'icon-512.png'],
      manifest: {
        name: 'Tarmac',
        short_name: 'Tarmac',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#0b0b0c',
        theme_color: '#0b0b0c',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // Shell only. Media must NEVER be precached: if the service worker can
        // serve audio.m4a the offline test proves nothing, because the bytes
        // would come from the SW cache rather than from OPFS. No m4a/jpg here.
        globPatterns: ['**/*.{js,css,html,ico,png,svg,webmanifest}'],
        globIgnores: ['media/**'],
        // navigateFallback defaults to index.html, which is what FM-1 requires.
        // The denylist keeps the shell from ever being served in place of an
        // API response — an offline /api call must fail, not return HTML.
        navigateFallbackDenylist: [/^\/api\//],
      },
    }),
  ],
  server: net,
  preview: net,
});
