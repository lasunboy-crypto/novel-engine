import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'

export default function NotFoundPage() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">404 — 页面未找到</h1>
        <p className="text-sm text-muted-foreground">
          请检查 URL 是否正确,或回到主页继续。
        </p>
      </div>
      <Button asChild>
        <Link to="/">回到主页</Link>
      </Button>
    </div>
  )
}
