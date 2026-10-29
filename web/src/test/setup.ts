/**
 * v1.1 wave 1 session 5(J2.2)— vitest 全局 setup 文件。
 *
 * 由 `vitest.config.ts::test.setupFiles` 在每个测试文件运行前加载。
 *
 * - `@testing-library/jest-dom/vitest`:导入 `toBeInTheDocument` /
 *   `toHaveClass` / `toHaveAttribute` 等 DOM 断言 matcher 到 `expect`。
 * - `cleanup`:每个测试结束后清理 RTL 渲染的 DOM(vitest 4.x + RTL 16.x
 *   组合下不会自动 cleanup,需要显式注册 afterEach)。
 */

import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
})
