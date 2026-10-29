/**
 * OutlineHistoryDropdown(P5-F-05 简版)。
 *
 * **P5 简版**:plan §自决项一览 #3 字面"P5 简版 — 历史版本走
 * `outline/world_history/`,只读对比 + 不带 diff highlight;P6 升级 diff"。
 * grep 实证后端 P5-B-04 期未暴露 `world_history/` 历史目录 GET 端点(同名子目录
 * 仅落盘约定,无 HTTP 接口);**类 C 处置 — 启动指令字面授权"占位",不顺延后端
 * 任务**。组件展示为只读 placeholder 下拉,提示"P6 升级真实历史 + diff"。
 *
 * 接口预留:`items` prop 与 `onSelect` 回调按真实历史接口设计(P6 落
 * `GET /outline/history` 后,父组件传入 items 即可启用,不改组件签名)。
 *
 * **不上 zustand** / **不接 mutation**:纯展示组件;父组件 OutlinePage 决定
 * 何时填 items(P5 始终为空)。
 */

import { useState, useRef, useEffect } from "react";

import { Button } from "@/components/ui/button";

export interface OutlineHistoryItem {
  /** 保存时间戳(ISO 8601 UTC),用于展示与排序。 */
  saved_at: string;
  /** 段名(world / characters / plot / volume_plans);P5 简版未启用,P6 接入。 */
  section: string;
  /** 可选 label(P6 配套 diff 摘要,如"+ 2 字段 / - 1 角色")。 */
  label?: string;
}

interface OutlineHistoryDropdownProps {
  items: OutlineHistoryItem[];
  onSelect?: (item: OutlineHistoryItem) => void;
  disabled?: boolean;
}

export function OutlineHistoryDropdown(props: OutlineHistoryDropdownProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const isEmpty = props.items.length === 0;

  return (
    <div className="relative" ref={containerRef}>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setOpen((v) => !v)}
        disabled={props.disabled}
        data-testid="outline-history-trigger"
      >
        历史版本{isEmpty ? "" : `(${props.items.length})`} ▾
      </Button>
      {open && (
        <div
          className="absolute right-0 z-10 mt-1 w-72 rounded border border-border bg-background p-2 shadow-md"
          data-testid="outline-history-dropdown"
        >
          {isEmpty ? (
            <p className="text-xs text-muted-foreground leading-relaxed">
              暂无历史版本。
              <br />
              <span className="text-muted-foreground/70">
                P5 简版:后端历史目录 GET 端点未暴露,本下拉作占位;
                P6 接 <code>outline/world_history/</code> 列表 + diff highlight。
              </span>
            </p>
          ) : (
            <ul className="space-y-0.5 text-sm">
              {props.items.slice(0, 10).map((item, idx) => (
                <li key={`${item.saved_at}-${idx}`}>
                  <button
                    type="button"
                    onClick={() => {
                      props.onSelect?.(item);
                      setOpen(false);
                    }}
                    className="block w-full rounded px-2 py-1 text-left hover:bg-secondary"
                  >
                    <span className="text-xs text-muted-foreground">
                      {item.saved_at}
                    </span>
                    {item.label && (
                      <span className="ml-1 text-xs">{item.label}</span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
