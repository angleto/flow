import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import { readBuildEnv } from './scripts/env.mjs'

const env = readBuildEnv()

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  // Relative, because the pages are loaded from chrome-extension://<id>/
  // and an absolute /assets/... would resolve against the extension root
  // only by luck of how Chrome serves it.
  base: './',
  define: {
    __MYC_ORIGIN__: JSON.stringify(env.baseUrl),
    __MYC_VERSION_NAME__: JSON.stringify(env.versionName),
    // The panel needs to know it cannot connect BEFORE offering to.
  },
  resolve: {
    alias: {
      // The barrel, and only the barrel: rules that belong to the REST
      // API rather than to either client (the error envelope, the entity
      // code, the recents contract, the query grammar, the connect
      // handshake) live once and are compiled into both packages.
      '@shared': fileURLToPath(new URL('../web/src/shared/index.ts', import.meta.url)),
    },
  },
  build: {
    // Deliberately not a dot-directory: a person picks this folder by
    // hand in Chrome's "Load unpacked" dialog, which hides hidden ones.
    outDir: fileURLToPath(new URL('./dist/unpacked', import.meta.url)),
    emptyOutDir: true,
    target: 'chrome116',
    // A source map for the panel documents only. The worker's map would
    // ship the credential-handling code's structure for no debugging
    // benefit anyone outside development gets.
    sourcemap: false,
    rollupOptions: {
      input: {
        popup: fileURLToPath(new URL('./popup.html', import.meta.url)),
        sidepanel: fileURLToPath(new URL('./sidepanel.html', import.meta.url)),
        background: fileURLToPath(new URL('./src/bg/index.ts', import.meta.url)),
      },
      output: {
        format: 'es',
        entryFileNames: '[name].js',
        // Chrome refuses to load any root-level file whose name starts
        // with an underscore, and Rollup names a shared chunk after its
        // first module. Left at the default, a package can install and
        // then silently fail to start, with no error anywhere: the popup
        // simply never renders. Fixed names avoid the whole class.
        chunkFileNames: 'assets/chunk-[hash].js',
        assetFileNames: 'assets/asset-[hash][extname]',
      },
    },
  },
})
