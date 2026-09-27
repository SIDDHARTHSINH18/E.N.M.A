import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Vitest config. Reuses the app's React plugin; tests run in
// jsdom so component tests can render real markup.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{js,jsx}'],
  },
})
