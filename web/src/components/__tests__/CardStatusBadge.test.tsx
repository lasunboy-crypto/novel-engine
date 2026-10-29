/**
 * v1.1 wave 1 session 5(J2.2)— `CardStatusBadge` 示例测试。
 *
 * 覆盖纯 presentational 组件的 render + props + fallback + className
 * 合并模式。**不**覆盖 user interaction(本组件无交互)— 见
 * `__tests__/example-user-event.test.tsx` 演示 user-event。
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { CardStatusBadge } from '@/components/CardStatusBadge'

describe('CardStatusBadge', () => {
  it('renders the 4 known statuses with the right label and color class', () => {
    const cases: Array<{
      status: 'draft' | 'pending_review' | 'approved' | 'rejected'
      label: string
      colorClass: string
    }> = [
      { status: 'draft', label: '草稿', colorClass: 'bg-slate-100' },
      { status: 'pending_review', label: '待审核', colorClass: 'bg-amber-100' },
      { status: 'approved', label: '已通过', colorClass: 'bg-emerald-100' },
      { status: 'rejected', label: '已退回', colorClass: 'bg-rose-100' },
    ]

    for (const { status, label, colorClass } of cases) {
      const { unmount } = render(<CardStatusBadge status={status} />)
      const badge = screen.getByTestId('card-status-badge')
      expect(badge).toBeInTheDocument()
      expect(badge).toHaveTextContent(label)
      expect(badge).toHaveClass(colorClass)
      expect(badge).toHaveAttribute('data-card-status', status)
      unmount()
    }
  })

  it('falls back to "未知状态" with a zinc class for unknown / null status', () => {
    render(<CardStatusBadge status={null} />)
    const badge = screen.getByTestId('card-status-badge')
    expect(badge).toHaveTextContent('未知状态')
    expect(badge).toHaveClass('bg-zinc-100')
    expect(badge).toHaveAttribute('data-card-status', 'unknown')
  })

  it('falls back for unrecognised string values', () => {
    render(<CardStatusBadge status="bogus" />)
    const badge = screen.getByTestId('card-status-badge')
    expect(badge).toHaveTextContent('未知状态')
    expect(badge).toHaveAttribute('data-card-status', 'bogus')
  })

  it('merges custom className via cn() without dropping the base badge classes', () => {
    render(<CardStatusBadge status="approved" className="ml-2 text-base" />)
    const badge = screen.getByTestId('card-status-badge')
    expect(badge).toHaveClass('ml-2')
    expect(badge).toHaveClass('text-base')
    expect(badge).toHaveClass('rounded')
    expect(badge).toHaveClass('bg-emerald-100')
  })
})
