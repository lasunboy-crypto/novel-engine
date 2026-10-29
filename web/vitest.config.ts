/// <reference types="vitest" />
import path from 'node:path'
import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// v1.1 wave 1 session 5(J2.2)— vitest + @testing-library/react 测试基建。
// 复用 vite.config.ts 的 alias / plugin 配置,避免维护两份;test 段独立配置。
//
// 不设 coverage 硬阈值(plan §4 w1-s5 自决项 Q3)— 本 session 只建基础设施,
// coverage 配起来但不做硬阈值,wave 2-5 累积测试覆盖后再设(避免 wave 1
// 末单测稀疏时跑 coverage 必报红)。
export default mergeConfig(
  viteConfig,
  defineConfig({
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      coverage: {
        provider: 'v8',
        reporter: ['text', 'html'],
        include: ['src/**/*.{ts,tsx}'],
        exclude: [
          'src/**/*.{test,spec}.{ts,tsx}',
          'src/test/**',
          'src/main.tsx',
          'src/vite-env.d.ts',
          'src/api/types.ts',
        ],
      },
    },
  }),
)
