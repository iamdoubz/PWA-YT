import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { VitePWA } from 'vite-plugin-pwa';

// Bind to 0.0.0.0 and accept any Host header so a cloudflared tunnel can reach
// dev/preview. iOS gives you no OPFS, no service worker and no home-screen
// install without real HTTPS, so the tunnel is not optional.
const net = { host: true, allowedHosts: true };

export default defineConfig({
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
      },
    }),
  ],
  server: net,
  preview: net,
});
