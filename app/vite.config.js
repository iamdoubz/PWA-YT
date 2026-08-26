import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { VitePWA } from 'vite-plugin-pwa';

// One .env for the whole project, at the repo root, next to docker-compose.yml.
const ROOT_ENV_DIR = fileURLToPath(new URL('..', import.meta.url));

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

export default defineConfig(({ mode }) => {
  // The prefix is the exact key name, so this reads PUBLIC_ORIGIN and nothing
  // else out of a .env that also holds PWA_YT_COOKIE_KEY and the SMTP
  // password. `envPrefix` stays at its VITE_ default, so none of those reach
  // import.meta.env either — this value is only ever substituted into HTML.
  //
  // This covers `npm run dev` and a local `npm run build`. The Docker image
  // does NOT use it: a published image is pulled onto hostnames it cannot
  // know, so there the same variable is applied at container start instead
  // (app/docker-entrypoint.d/40-og-origin.sh). One name, two moments.
  const env = loadEnv(mode, ROOT_ENV_DIR, 'PUBLIC_ORIGIN');
  // Trailing slash stripped so `${origin}/imgs/...` can't become a double
  // slash — some scrapers treat that as a different URL and miss the cache.
  const origin = (env.PUBLIC_ORIGIN || '').replace(/\/+$/, '');

  return {
  envDir: ROOT_ENV_DIR,
  // Stamped into the readiness panel. A service worker keeps serving the
  // previous shell until a second load, so "which build am I actually running"
  // is a real question on a phone you cannot attach a debugger to.
  define: { __BUILD__: JSON.stringify(new Date().toISOString().slice(0, 19) + 'Z') },
  plugins: [
    svelte(),
    {
      // Open Graph wants absolute URLs. Vite's own %VITE_FOO% substitution
      // leaves the literal placeholder in the HTML when a var is unset, which
      // would be worse than no tag at all — so do it here, where unset simply
      // collapses to a relative URL that still works everywhere but
      // Facebook/X. Runs in dev too, so what you see at :5173 is what ships.
      name: 'pwa-yt:og-origin',
      transformIndexHtml: (html) => html.replaceAll('%OG_ORIGIN%', origin),
    },
    VitePWA({
      registerType: 'autoUpdate',
      // This is the app's one manifest. public/imgs/site.webmanifest came with
      // the favicon package and is deliberately NOT linked: a page gets one
      // manifest, and that one has no start_url or scope (so an installed app
      // would not know its own boundary) and a #404040 theme that contradicts
      // D-029's dark default. The icons from it are merged in here instead.
      manifest: {
        name: 'PWA-YT',
        short_name: 'PWA-YT',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#0b0b0c',
        theme_color: '#0b0b0c',
        icons: [
          { src: '/imgs/web-app-manifest-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: '/imgs/web-app-manifest-512x512.png', sizes: '512x512', type: 'image/png' },
          // Maskable is what Android crops into its icon shape. The favicon
          // package generates these already padded for it.
          { src: '/imgs/web-app-manifest-192x192.png', sizes: '192x192', type: 'image/png', purpose: 'maskable' },
          { src: '/imgs/web-app-manifest-512x512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        // Shell only. Media must NEVER be precached: if the service worker can
        // serve audio.m4a the offline test proves nothing, because the bytes
        // would come from the SW cache rather than from OPFS. No m4a/jpg here.
        globPatterns: ['**/*.{js,css,html,ico,png,svg,webmanifest}'],
        // opengraph.png is only ever fetched by link-preview scrapers, which
        // are online by definition — precaching 39 KB the app itself never
        // renders is dead weight in the shell. site.webmanifest is unused (see
        // above) and would only confuse a future reader of the precache list.
        globIgnores: ['media/**', 'imgs/opengraph.png', 'imgs/site.webmanifest'],
        // navigateFallback defaults to index.html, which is what FM-1 requires.
        // The denylist keeps the shell from ever being served in place of an
        // API response — an offline /api call must fail, not return HTML.
        navigateFallbackDenylist: [/^\/api\//],
      },
    }),
  ],
  server: net,
  preview: net,
  };
});
