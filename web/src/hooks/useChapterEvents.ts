/**
 * 章节事件订阅 hook(P1-F-06)。
 *
 * 订阅 `chapter.{chapterId}.events` 频道(对齐 API_CONTRACT §5.3 + P1-B-20
 * 决策 6),mount 时拿模块级单例 WS client `subscribe`,unmount 时取消。
 *
 * P1 范围内服务端只发 `state_transition` 事件(`{from, to}`);P2+ 才会有
 * `token_progress` / `review_done` 等。本 hook 把 `event === "state_transition"`
 * 的帧分发给 `onStateTransition` 回调,其他事件 mock 不识别静默丢弃(前向
 * 兼容,避免后端先发 P2 事件时前端报错)。
 *
 * 设计选择
 * --------
 *
 * - hook 不维护"最后一次 transition" state:UI 自取自的子状态(用 React state
 *   或 zustand store 都行,P1-F-15 才接);本 hook 保持纯回调,API 表面更稳
 * - 不在 hook 内创建 WS client:所有 hook 共享 `getWsClient()` 单例
 * - `chapterId` 变化时自动取消旧订阅 + 订阅新 channel(useEffect 依赖 chapterId)
 */

import { useEffect } from "react";

import { getWsClient } from "@/ws/client";
import { STATE_TRANSITION, type StateTransitionData } from "@/ws/types";

/** 章节事件频道名:`chapter.{chapterId}.events`(对齐后端 `chapter_events_channel`)。 */
function chapterEventsChannel(chapterId: string): string {
  return `chapter.${chapterId}.events`;
}

interface UseChapterEventsOptions {
  /** 收到 `state_transition` 事件时调用;data 已校验为 `{from, to}` 字符串对。 */
  onStateTransition?: (data: StateTransitionData) => void;
}

/**
 * 订阅章节实时事件。
 *
 * @param chapterId 章节 id;空字符串 / undefined 时不订阅(允许 UI 在 id 还没
 *   解出来时先挂 hook,等 router param ready 再触发订阅)
 * @param options 事件回调集合
 */
export function useChapterEvents(
  chapterId: string | undefined,
  options: UseChapterEventsOptions = {},
): void {
  const { onStateTransition } = options;

  useEffect(() => {
    if (!chapterId) {
      return;
    }
    const client = getWsClient();
    const channel = chapterEventsChannel(chapterId);
    const unsubscribe = client.subscribe<StateTransitionData>(
      channel,
      (frame) => {
        if (frame.event === STATE_TRANSITION) {
          onStateTransition?.(frame.data);
        }
        // P2+ 其他事件:静默忽略(前向兼容)。
      },
    );
    return unsubscribe;
  }, [chapterId, onStateTransition]);
}
