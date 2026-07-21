import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { requireProductionWebEnv } from './src/buildEnv';

export default defineConfig(({ command, mode }) => {
  if (command === 'build') {
    requireProductionWebEnv(loadEnv(mode, '.', ''));
  }

  return {
    plugins: [react()],
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.ts',
    },
  };
});
