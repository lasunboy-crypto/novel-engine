/**
 * 卷章节张力表(P5-F-04)。
 *
 * 三列:章号 / tension(可编辑 1-10) / is_arc_finale(只读,由 arcs 末位推断)。
 *
 * 表格-曲线双向绑定(自决项 2 + 留观项 1):
 *   - 表格行 input 改 tension → onChange 回传父组件 → 父 useState 更新 →
 *     TensionCurveChart 实时重绘
 *   - tension 输入范围 1-10 整数;< 1 / > 10 / 非整 被 input 拒绝或提交时由
 *     后端 422 拦截
 *   - is_arc_finale 列只读(由 arcs 末位章号决定,不在本表内编辑)
 */

import type { TensionCurvePoint } from "@/api/volumes";

interface VolumeChapterTableProps {
  /** 当前草稿 curve(按 chapter_number 升序);空时显示占位 */
  points: readonly TensionCurvePoint[];
  /** 改单章 tension(整数 1-10);父组件更新对应索引并触发曲线重绘 */
  onTensionChange: (chapter_number: number, tension: number) => void;
  /** 是否禁用编辑(保存中 / 应用模板中) */
  disabled?: boolean;
}

export function VolumeChapterTable({
  points,
  onTensionChange,
  disabled,
}: VolumeChapterTableProps) {
  if (points.length === 0) {
    return (
      <div
        className="rounded border border-dashed border-border p-4 text-sm text-muted-foreground"
        data-testid="volume-chapter-table-empty"
      >
        当前卷无张力曲线 — 应用模板或手动填入数值。
      </div>
    );
  }

  return (
    <div
      className="max-h-96 overflow-y-auto rounded border border-border"
      data-testid="volume-chapter-table"
    >
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-secondary/60 text-xs uppercase tracking-wide text-muted-foreground">
          <tr>
            <th className="w-20 px-3 py-2 text-left">章号</th>
            <th className="w-32 px-3 py-2 text-left">张力(1-10)</th>
            <th className="px-3 py-2 text-left">弧线终章</th>
          </tr>
        </thead>
        <tbody>
          {points.map((point) => (
            <tr
              key={point.chapter_number}
              className="border-t border-border"
              data-testid={`chapter-row-${point.chapter_number}`}
            >
              <td className="px-3 py-1.5 tabular-nums text-muted-foreground">
                第 {point.chapter_number} 章
              </td>
              <td className="px-3 py-1.5">
                <input
                  type="number"
                  min={1}
                  max={10}
                  step={1}
                  value={point.tension}
                  disabled={disabled}
                  onChange={(e) => {
                    const next = Number(e.target.value);
                    if (Number.isFinite(next)) {
                      onTensionChange(point.chapter_number, next);
                    }
                  }}
                  className="w-20 rounded border border-input bg-background px-2 py-0.5 text-sm tabular-nums disabled:cursor-not-allowed disabled:opacity-50"
                  data-testid={`tension-input-${point.chapter_number}`}
                />
              </td>
              <td className="px-3 py-1.5">
                {point.is_arc_finale ? (
                  <span
                    className="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-800"
                    data-testid={`finale-badge-${point.chapter_number}`}
                  >
                    弧线终章
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
