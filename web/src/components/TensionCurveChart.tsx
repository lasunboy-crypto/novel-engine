/**
 * 张力曲线图(P5-F-04 落简版只读 + onChange 表格驱动;P6-F-07 扩 SVG pointer
 * event 拖拽 UI,P5 留观项 1 v1 内解决,纯 Recharts + 不引入 dndkit / visx)。
 *
 * 与 `MetricsTensionCurve`(P4-F-03)的差异:
 *   - 数据来源:`Volume.tension_curve`(章节 1-N,target only),非 target vs actual
 *   - 用途:VolumePlanPage 编辑态实时反映 useState 草稿;Metrics 是只读看板
 *   - X 轴:`chapter_number`(1-N 整数),Y 轴 1-10
 *   - is_arc_finale 标记为不同色 dot(emerald → amber),与表格行徽章对齐
 *
 * **拖拽 UI(P6-F-07)**:
 *   - 若提供 `onTensionChange` 且未 `disabled`,在每个 dot 上挂 `onPointerDown`
 *     启动拖拽(`setPointerCapture` 保证后续 move/up 即便鼠标飞出 dot 也能收到)
 *   - 外层容器挂 `onPointerMove` / `onPointerUp` / `onPointerCancel`
 *   - Y 像素 → tension:利用 `.recharts-cartesian-grid` 的 BBox(真实 plot
 *     area)反推 Y 域 [0, 10] → tension,clamp 1-10 + round 整数
 *   - throttle 50ms(`lastEmitRef` 时间戳;末位拖拽值释放时再 emit 一次保证
 *     落值精确)
 *   - 触屏 fallback:Pointer Events 标准已涵盖 touch + mouse + pen(MDN 标
 *     准),无需独立 touch handler
 *   - 拖拽中 cursor=`ns-resize`,dot 显示 `cursor-ns-resize` 提示可拖
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { TensionCurvePoint } from "@/api/volumes";

interface TensionCurveChartProps {
  /** 曲线点列表(已按 chapter_number 排序);空时显示占位 */
  points: readonly TensionCurvePoint[];
  /**
   * 拖拽 onChange 回调(P6-F-07);提供则启用 SVG pointer event 拖拽。
   * 不提供则与 P5-F-04 行为相同(只读展示)。
   */
  onTensionChange?: (chapter_number: number, tension: number) => void;
  /** 是否禁用拖拽(保存中 / 应用模板中);拖拽中即便切 disabled 也立即放手 */
  disabled?: boolean;
}

interface DotPayload {
  cx?: number;
  cy?: number;
  payload?: TensionCurvePoint;
  index?: number;
}

const THROTTLE_MS = 50;
const Y_MIN = 1;
const Y_MAX = 10;

export function TensionCurveChart({
  points,
  onTensionChange,
  disabled,
}: TensionCurveChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [draggingChapter, setDraggingChapter] = useState<number | null>(null);
  const lastEmitRef = useRef<{ t: number; ch: number; tension: number } | null>(
    null,
  );
  const pendingValueRef = useRef<{ ch: number; tension: number } | null>(null);
  const flushTimerRef = useRef<number | null>(null);

  const draggable = typeof onTensionChange === "function" && !disabled;

  /**
   * 把 client Y(屏幕坐标)转为 tension(整数 1-10,clamp)。
   *
   * 用 `.recharts-cartesian-grid` 元素的 BBox 拿真实 plot area;
   * 失败时(组件未挂载完成)返回 null,调用方忽略本次事件。
   */
  const clientYToTension = useCallback((clientY: number): number | null => {
    const root = containerRef.current;
    if (!root) return null;
    const gridEl = root.querySelector<SVGGraphicsElement>(
      ".recharts-cartesian-grid",
    );
    if (!gridEl) return null;
    const rect = gridEl.getBoundingClientRect();
    if (rect.height <= 0) return null;
    const ratio = (clientY - rect.top) / rect.height;
    const tension = Y_MAX - ratio * (Y_MAX - 0);
    const rounded = Math.round(tension);
    if (rounded < Y_MIN) return Y_MIN;
    if (rounded > Y_MAX) return Y_MAX;
    return rounded;
  }, []);

  const flushPending = useCallback(() => {
    flushTimerRef.current = null;
    const pending = pendingValueRef.current;
    if (!pending || !onTensionChange) return;
    const last = lastEmitRef.current;
    if (last && last.ch === pending.ch && last.tension === pending.tension) {
      return;
    }
    lastEmitRef.current = { t: Date.now(), ch: pending.ch, tension: pending.tension };
    onTensionChange(pending.ch, pending.tension);
    pendingValueRef.current = null;
  }, [onTensionChange]);

  const emitTension = useCallback(
    (ch: number, tension: number) => {
      if (!onTensionChange) return;
      const now = Date.now();
      const last = lastEmitRef.current;
      pendingValueRef.current = { ch, tension };
      if (!last || now - last.t >= THROTTLE_MS) {
        flushPending();
        return;
      }
      if (flushTimerRef.current == null) {
        const wait = Math.max(0, THROTTLE_MS - (now - last.t));
        flushTimerRef.current = window.setTimeout(flushPending, wait);
      }
    },
    [flushPending, onTensionChange],
  );

  const stopDragging = useCallback(() => {
    if (draggingChapter == null) return;
    setDraggingChapter(null);
    if (flushTimerRef.current != null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    // 释放时立即 flush 落值,保证最后一个 tension 一定落
    flushPending();
  }, [draggingChapter, flushPending]);

  // disabled 切到 true 时立即停拖
  useEffect(() => {
    if (disabled && draggingChapter != null) {
      stopDragging();
    }
  }, [disabled, draggingChapter, stopDragging]);

  // 卸载时清掉 timer 避免 memory leak
  useEffect(() => {
    return () => {
      if (flushTimerRef.current != null) {
        window.clearTimeout(flushTimerRef.current);
      }
    };
  }, []);

  const handlePointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (draggingChapter == null || !draggable) return;
      const next = clientYToTension(e.clientY);
      if (next == null) return;
      emitTension(draggingChapter, next);
    },
    [clientYToTension, draggable, draggingChapter, emitTension],
  );

  const handlePointerUp = useCallback(() => {
    stopDragging();
  }, [stopDragging]);

  /**
   * 用原生 SVG `<circle>` 渲染曲线点(取代 Recharts `<Dot>`):
   *   - Recharts `<Dot>` 的 `onPointerDown` prop 签名是 `(props: DotProps)`,
   *     拿不到 DOM `PointerEvent`(无 pointerId / clientY),无法 setPointerCapture
   *   - 直接用 `<circle>` 同视觉属性(cx/cy/r/stroke/fill/strokeWidth),换到
   *     标准 React.PointerEvent<SVGCircleElement>
   *   - 加 `r=8` 的透明 hit-target circle 扩大可点击 / 拖拽区域(SVG dot
   *     视觉半径 4-6px 太小,触屏命中率差)
   */
  function CurveDot(props: DotPayload) {
    const { cx, cy, payload, index } = props;
    if (cx === undefined || cy === undefined || !payload) return null;
    const isFinale = payload.is_arc_finale;
    const onPointerDown = (e: React.PointerEvent<SVGCircleElement>) => {
      if (!draggable) return;
      e.preventDefault();
      e.stopPropagation();
      (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
      setDraggingChapter(payload.chapter_number);
      lastEmitRef.current = {
        t: 0, // 立即允许首次 emit
        ch: payload.chapter_number,
        tension: payload.tension,
      };
    };
    const visualR = isFinale ? 6 : 4;
    const stroke = isFinale ? "#d97706" : "#10b981";
    const fill = isFinale ? "#fbbf24" : "#10b981";
    const strokeWidth = isFinale ? 2 : 1;
    return (
      <g key={`dot-${index ?? payload.chapter_number}`}>
        <circle
          cx={cx}
          cy={cy}
          r={visualR}
          stroke={stroke}
          strokeWidth={strokeWidth}
          fill={fill}
        />
        {draggable && (
          <circle
            cx={cx}
            cy={cy}
            r={10}
            fill="transparent"
            stroke="transparent"
            style={{ cursor: "ns-resize", touchAction: "none" }}
            onPointerDown={onPointerDown}
            data-testid={`tension-curve-dot-${payload.chapter_number}`}
          />
        )}
      </g>
    );
  }

  if (points.length === 0) {
    return (
      <div
        className="flex h-64 w-full items-center justify-center rounded border border-dashed border-border text-sm text-muted-foreground"
        data-testid="tension-curve-chart-empty"
      >
        张力曲线为空 — 应用模板或在表格中填入数值后曲线会出现。
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="h-64 w-full"
      data-testid="tension-curve-chart"
      data-dragging={draggingChapter != null ? "true" : "false"}
      onPointerMove={draggable ? handlePointerMove : undefined}
      onPointerUp={draggable ? handlePointerUp : undefined}
      onPointerCancel={draggable ? handlePointerUp : undefined}
      style={
        draggingChapter != null
          ? { cursor: "ns-resize", touchAction: "none", userSelect: "none" }
          : undefined
      }
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={points as TensionCurvePoint[]}
          margin={{ top: 8, right: 16, bottom: 8, left: 0 }}
        >
          <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
          <XAxis
            dataKey="chapter_number"
            tick={{ fontSize: 11 }}
            allowDecimals={false}
            label={{
              value: "章节序号",
              position: "insideBottom",
              offset: -2,
              style: { fontSize: 11, fill: "#71717a" },
            }}
          />
          <YAxis
            domain={[0, 10]}
            ticks={[0, 2, 4, 6, 8, 10]}
            tick={{ fontSize: 11 }}
            width={28}
            allowDecimals={false}
          />
          <Tooltip
            contentStyle={{ fontSize: 12 }}
            formatter={(value) => [String(value), "张力"]}
            labelFormatter={(label) => `第 ${String(label)} 章`}
          />
          <Line
            type="monotone"
            name="tension"
            dataKey="tension"
            stroke="#10b981"
            strokeWidth={2}
            dot={<CurveDot />}
            activeDot={{ r: 6 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
