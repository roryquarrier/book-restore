// @ts-check
import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwindcss from '@tailwindcss/vite';
import node from '@astrojs/node';
import clerk from '@clerk/astro';

export default defineConfig({
  integrations: [
    react(),
    clerk({
      appearance: {
        variables: {
          colorBackground: 'var(--tv-surface)',
          colorText: 'var(--tv-fg)',
          colorTextSecondary: 'var(--tv-fg-muted)',
          colorPrimary: 'var(--tv-accent)',
          colorInputBackground: 'var(--tv-bg)',
          colorInputText: 'var(--tv-fg)',
          colorInputBorder: 'var(--tv-border)',
          borderRadius: 'var(--tv-radius)',
          fontFamily: 'var(--tv-font-body)',
        },
      },
    }),
  ],
  vite: { plugins: [tailwindcss()] },
  adapter: node({ mode: 'standalone' }),
  security: {
    // CSRF protection: verify the browser Origin header matches the request URL.
    // Re-enabled after the root cause (proxy header trust) was fixed below.
    checkOrigin: true,
    // Fly.io terminates TLS and proxies to the app over plain HTTP on the
    // internal port, so the request URL is otherwise computed as
    // http://<fly-internal-host>, which never matches the browser's
    // https://restorepdfbooks.com Origin header and trips the 403
    // "Cross-site POST form submissions are forbidden".
    //
    // allowedDomains tells Astro which X-Forwarded-Host values are safe to
    // trust. Only matching forwarded hosts rewrite Astro.url, so a spoofed
    // X-Forwarded-Host from an untrusted source is ignored (Astro falls back
    // to the raw Host header). This keeps CSRF protection intact behind the
    // proxy instead of disabling it globally.
    allowedDomains: [
      { hostname: 'restorepdfbooks.com', protocol: 'https' },
      { hostname: 'www.restorepdfbooks.com', protocol: 'https' },
    ],
  },
});
