/**
 * v1.1 wave 1 session 5(J2.2)— `@testing-library/user-event` 工作链示例。
 *
 * 用 inline 渲染的最小受控按钮证明 user-event 14.x + jsdom 29.x + vitest 4.x
 * 全链路通,**不绑定任何业务组件**。
 *
 * 不选 `ConfirmDialog` 之类用 Radix Portal + Focus trap 的真实组件作为首批
 * user-interaction 示例,因为 jsdom 下需要额外的 `Element.prototype.hasPointerCapture`
 * / `scrollIntoView` polyfill,会让首批示例看起来在调环境而不是在测组件
 * (1st 质量收益)。复杂业务组件留 wave 4 J2.1 wrapper hook 单测时再补 polyfill。
 */

import { useState } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

function ClickCounter({ onClick }: { onClick?: (count: number) => void }) {
  const [count, setCount] = useState(0)
  return (
    <button
      type="button"
      data-testid="counter-button"
      onClick={() => {
        const next = count + 1
        setCount(next)
        onClick?.(next)
      }}
    >
      clicked {count} times
    </button>
  )
}

function ControlledInput() {
  const [value, setValue] = useState('')
  return (
    <input
      data-testid="ce-input"
      value={value}
      onChange={(e) => setValue(e.target.value)}
    />
  )
}

describe('user-event smoke test', () => {
  it('click() updates internal state and fires callback', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    render(<ClickCounter onClick={onClick} />)

    const button = screen.getByTestId('counter-button')
    expect(button).toHaveTextContent('clicked 0 times')

    await user.click(button)
    expect(button).toHaveTextContent('clicked 1 times')

    await user.click(button)
    await user.click(button)
    expect(button).toHaveTextContent('clicked 3 times')

    expect(onClick).toHaveBeenCalledTimes(3)
    expect(onClick).toHaveBeenLastCalledWith(3)
  })

  it('type() writes characters to an input element', async () => {
    const user = userEvent.setup()
    render(<ControlledInput />)
    const input = screen.getByTestId('ce-input') as HTMLInputElement

    await user.type(input, 'hello')
    expect(input.value).toBe('hello')
  })
})
