/**
 * WebSocket 消息类型(P1-F-06,**手写**)。
 *
 * 字面对齐:
 * - API_CONTRACT §5.1 客户端消息(`subscribe` / `unsubscribe`)
 * - API_CONTRACT §5.2 服务端推送统一格式(`{channel, event, timestamp, data}`)
 * - API_CONTRACT §5.3 标准频道(P1 只用 `chapter.{id}.events` 的 `state_transition`)
 * - API_CONTRACT §5.4 心跳(服务端 → ping,客户端 → pong)
 *
 * **WS 不进 OpenAPI**(P1-B-20 决策 7):前端 `pnpm gen-types` 仅覆盖 REST,
 * WS 协议帧字段在前后端两侧分别手写维护。
 *
 * **跨阶段红线**:P2+ 的 `pipeline.events` / `system.alerts` /
 * `task.{id}.progress` 多事件型频道在 P1 不实现(P1-B-20 "P1 不实现"清单)。
 */
export const STATE_TRANSITION = "state_transition" as const;

/** 服务端 → 客户端的心跳 ping。 */
export interface ServerPingFrame {
  type: "ping";
}

/** 客户端 → 服务端的心跳 pong(响应 ping)。 */
export interface ClientPongFrame {
  type: "pong";
}

/** 客户端 → 服务端的订阅请求。 */
export interface ClientSubscribeFrame {
  type: "subscribe";
  channels: string[];
}

/** 客户端 → 服务端的取消订阅请求。 */
export interface ClientUnsubscribeFrame {
  type: "unsubscribe";
  channels: string[];
}

/** 客户端发往服务端的全部协议帧。 */
export type ClientFrame =
  | ClientPongFrame
  | ClientSubscribeFrame
  | ClientUnsubscribeFrame;

/**
 * 服务端推送的频道消息(P1 推送 = state_transition)。
 *
 * 字面对齐 `src/api/websocket/manager.py:_build_message`。`timestamp` 是
 * ISO 8601 UTC,Z 后缀。
 */
export interface ServerPushFrame<TData = unknown> {
  channel: string;
  event: string;
  timestamp: string;
  data: TData;
}

/** 频道事件 + ping 的 union(凡是 onmessage 收到的服务端帧都是这个)。 */
export type ServerFrame<TData = unknown> =
  | ServerPingFrame
  | ServerPushFrame<TData>;

/**
 * `chapter.{id}.events` 频道的 `state_transition` 事件 data 形状。
 *
 * 字面对齐 P1-B-20 决策 6:`{from, to}`,值为 `ChapterState.value`
 * (snake_case 字符串,如 `writing` / `reviewing` / `passed`)。
 */
export interface StateTransitionData {
  from: string;
  to: string;
}

/**
 * WS 客户端运行态(供 UI / 调试展示)。
 *
 * - `connecting`:正在建立 WS(首次或重连尝试中)
 * - `open`:连接 ready(`readyState=1`)
 * - `closed`:正常关闭(主动 `close()`,不会自动重连)
 * - `reconnecting`:连接已断,排队中等下一次退避重连
 */
export type WsConnectionState =
  | "connecting"
  | "open"
  | "closed"
  | "reconnecting";

/**
 * 频道监听器签名。`frame` 是完整服务端帧;listener 自行判断 `frame.event`
 * + `frame.data` 形状(P1 只发 state_transition,后续多事件时按 event 分流)。
 */
export type ChannelListener<TData = unknown> = (
  frame: ServerPushFrame<TData>,
) => void;
