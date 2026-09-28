/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  RESTART_PATH,
  createRestartHandler,
  readAccessKey,
  spawnLauncherRestart,
} from './server/restartEndpoint'

const repoDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')

// The phone's server-restart button; see server/restartEndpoint.ts for why it
// lives in this process rather than in the Python server it restarts.
function restartServer(): Plugin {
  const handler = createRestartHandler({
    readKey: readAccessKey,
    startRestart: () => spawnLauncherRestart(repoDir),
  })
  return {
    name: 'remote-iterm-restart-server',
    configureServer(server) { server.middlewares.use(RESTART_PATH, handler) },
    configurePreviewServer(server) { server.middlewares.use(RESTART_PATH, handler) },
  }
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), restartServer()],
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
