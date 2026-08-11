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
});
