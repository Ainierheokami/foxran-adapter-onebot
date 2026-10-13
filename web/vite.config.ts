import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import cssInjectedByJsPlugin from 'vite-plugin-css-injected-by-js'

// Builds ../index.umd.js; the Foxran WebUI loads it at boot (foxran.yaml adapter.frontend_bundle).
export default defineConfig({
  plugins: [vue(), tailwindcss(), cssInjectedByJsPlugin()],
  build: {
    outDir: '../',
    emptyOutDir: false,
    lib: {
      entry: 'index.ts',
      name: 'FoxranAdapterOneBot',
      formats: ['umd'],
      fileName: () => 'index.umd.js',
    },
    rollupOptions: {
      external: ['vue', 'lucide-vue-next'],
      output: {
        globals: { vue: 'Vue', 'lucide-vue-next': 'LucideVueNext' },
      },
    },
  },
})
