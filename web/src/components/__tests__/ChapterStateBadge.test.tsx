/**
 * v1.1 wave 1 session 5(J2.2)— `ChapterStateBadge` 示例测试。
 *
 * 覆盖纯 presentational 组件的 7 档全状态枚举 + null fallback 模式;
 * 同时作为 `__tests__/CardStatusBadge.test.tsx` 的"姐妹组件"演示同型
 * 测试结构在多组件间的复用。
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ChapterStateBadge } from '@/components/ChapterStateBadge'

describe('ChapterStateBadge', () => {
  it.each([
    ['drafting', '起草中', 'bg-sky-100'],
    ['reviewing', '审稿中', 'bg-amber-100'],
    ['rewriting', '重写中', 'bg-indigo-100'],
    ['revising', '修订中', 'bg-violet-100'],
    ['approved', '已通过', 'bg-emerald-100'],
    ['human_intervention', '人工介入', 'bg-rose-100'],
    ['invalidated', '已失效', 'bg-orange-100'],
  ])('renders status=%s as label "%s" with class %s', (status, label, colorClass) => {
    render(<ChapterStateBadge status={status} />)
    const badge = screen.getByTestId('chapter-state-badge')
    expect(badge).toHaveTextContent(label)
    expect(badge).toHaveClass(colorClass)
    expect(badge).toHaveAttribute('data-status', status)
  })

  it('falls back to "等待事件" for null status (WS pre-observation)', () => {
    render(<ChapterStateBadge status={null} />)
    const badge = screen.getByTestId('chapter-state-badge')
    expect(badge).toHaveTextContent('等待事件')
    expect(badge).toHaveClass('bg-zinc-100')
    expect(badge).toHaveAttribute('data-status', 'waiting')
  })

  it('falls back for unknown status strings (defensive against new back-end states)', () => {
    render(<ChapterStateBadge status="bogus_new_state" />)
    const badge = screen.getByTestId('chapter-state-badge')
    expect(badge).toHaveTextContent('等待事件')
    expect(badge).toHaveAttribute('data-status', 'bogus_new_state')
  })

  it('falls back for legacy "published" status (B-04 hot-fix mapping removed)', () => {
    render(<ChapterStateBadge status="published" />)
    const badge = screen.getByTestId('chapter-state-badge')
    expect(badge).toHaveTextContent('等待事件')
  })
})
