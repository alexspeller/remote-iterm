/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // Listen on all addresses, including LAN and public IPs
    port: 7292
  },
  // What the launcher runs for the phone: `vite preview` serving the built
  // bundle in dist/. The dev server is for development only — its HMR client
  // reloads the page whenever its websocket drops and comes back, which on a
  // phone is every screen lock (see ensure_client_build in ../iterm-server).
  preview: {
    host: true,
    port: 7292,
    strictPort: true
  },
  test: {
    setupFiles: ['./src/testSetup.ts']
  }
})
