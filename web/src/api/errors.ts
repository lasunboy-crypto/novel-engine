/**
 * API 错误类型与 ApiError 类(P1-F-03)。
 *
 * 对齐 API_CONTRACT.md §3.2 错误响应字面规范:
 *
 *   { error: { code, message, details?, trace_id?, request_id } }
 *
 * 为何 envelope 形状手写而非由 `types.ts` 自动生成:
 * 后端路由装饰器未显式声明 `responses={4xx: ErrorEnvelope}`,FastAPI 默认只把
 * 成功响应(`response_model`)收进 OpenAPI,导致 `ErrorEnvelope` / `ErrorPayload`
 * 不进 `web/src/api/types.ts`。本文件按 spec §3.2 字面手写,属协议层定式
 * (类比 P1-B-20 决策 7 "WebSocket 不进 OpenAPI,WS 客户端手写消息封装"),
 * 与 CLAUDE.md 硬约束 5 "禁止手写后端业务字段类型"的边界不同。
 *
 * 后端将来若在路由上补 `responses=` 显式声明,gen-types 会自动产出
 * `ErrorEnvelope` schema,届时本文件 `ApiErrorBody` 应改为 `import` 生成类型。
 */

/**
 * 错误响应的 `error` 字段形状(对齐 src/api/schemas/common.py:ErrorPayload)。
 */
export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
  trace_id?: string | null;
  request_id: string;
}

/**
 * 错误响应包络(对齐 src/api/schemas/common.py:ErrorEnvelope)。
 */
export interface ApiErrorEnvelope {
  error: ApiErrorBody;
}

/**
 * client 抛出的错误对象。所有 fetch 失败路径(网络错误 / 非 2xx / 包络解析
 * 失败)统一收敛到本类。
 *
 * - `code`:机器可读错误码(`<DOMAIN>_<REASON>`),网络错误为 `NETWORK_ERROR`,
 *   包络解析失败为 `UNKNOWN_ERROR`,后端返回的标准错误码原样透传。
 * - `httpStatus`:HTTP 状态码;网络错误为 0。
 * - `requestId`:`req_xxx`,后端必含;网络错误等无 request_id 时为 `null`。
 * - `traceId`:章节 trace(`{chapter_id}-{operation}{n}`),非章节路由通常 null。
 * - `details`:字段级错误等结构化 payload,可能为 null。
 */
export class ApiError extends Error {
  readonly code: string;
  readonly httpStatus: number;
  readonly requestId: string | null;
  readonly traceId: string | null;
  readonly details: Record<string, unknown> | null;

  constructor(params: {
    code: string;
    message: string;
    httpStatus: number;
    requestId: string | null;
    traceId?: string | null;
    details?: Record<string, unknown> | null;
  }) {
    super(params.message);
    this.name = "ApiError";
    this.code = params.code;
    this.httpStatus = params.httpStatus;
    this.requestId = params.requestId;
    this.traceId = params.traceId ?? null;
    this.details = params.details ?? null;
  }
}

/**
 * 类型守卫:判断 unknown 错误是否为 ApiError。
 *
 * 用 instanceof + 字段检查双保险,避免跨 bundle 边界 instanceof 失效场景
 * (P1 单 bundle 不会触发,但守卫鲁棒成本零)。
 */
export function isApiError(error: unknown): error is ApiError {
  return (
    error instanceof ApiError ||
    (typeof error === "object" &&
      error !== null &&
      (error as { name?: unknown }).name === "ApiError" &&
      typeof (error as { code?: unknown }).code === "string")
  );
}

/**
 * 尝试把 unknown payload 解析为 ApiErrorEnvelope。失败返回 null。
 *
 * 不抛异常,调用方根据 null 决定走"标准错误路径"还是"未知错误路径"。
 */
export function parseErrorEnvelope(payload: unknown): ApiErrorBody | null {
  if (typeof payload !== "object" || payload === null) return null;
  const error = (payload as { error?: unknown }).error;
  if (typeof error !== "object" || error === null) return null;
  const body = error as Record<string, unknown>;
  if (typeof body.code !== "string") return null;
  if (typeof body.message !== "string") return null;
  if (typeof body.request_id !== "string") return null;
  return {
    code: body.code,
    message: body.message,
    request_id: body.request_id,
    details:
      body.details === undefined
        ? null
        : (body.details as Record<string, unknown> | null),
    trace_id:
      body.trace_id === undefined ? null : (body.trace_id as string | null),
  };
}
