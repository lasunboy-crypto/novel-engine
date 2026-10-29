/**
 * API client 基类(P1-F-03)。
 *
 * 用 `fetch` 封装 GET / POST / PUT / DELETE 四谓词,统一处理:
 *
 * - URL 拼接(baseURL + path)
 * - `Content-Type: application/json` + body 序列化
 * - 可选注入 `X-Workspace-Id` header(章节路由需要,见 P1-B-19 决策 1)
 * - 解析 SuccessEnvelope.data 返回(对齐 API_CONTRACT §3.1)
 * - 失败统一抛 ApiError(对齐 API_CONTRACT §3.2 + ./errors.ts)
 *
 * baseURL 默认 `http://localhost:8000/api/v1`,可被 `VITE_API_BASE_URL` 覆盖。
 *
 * 设计取舍:
 * - **stateless**:client 不维护"当前活动工作区"。后续 P1-F-10 起 zustand store
 *   由调用方读 + 透传 `workspaceId`,client 自己保持纯函数式。
 * - **泛型 = data 段类型**:调用方传 `T = components["schemas"]["XxxOut"]`,
 *   client 内部解 `envelope.data` 后返回 `T`,而非整个 envelope。
 */

import { ApiError, parseErrorEnvelope } from "@/api/errors";

const DEFAULT_BASE_URL = "http://localhost:8000/api/v1";

/**
 * 读 `VITE_API_BASE_URL` 环境变量;未设置则用默认值。
 *
 * Vite 把 `import.meta.env.VITE_*` 静态注入,build 期内联,不走运行时。
 */
function resolveBaseURL(): string {
  const envBase = import.meta.env.VITE_API_BASE_URL;
  if (typeof envBase === "string" && envBase.length > 0) {
    return envBase.replace(/\/$/, "");
  }
  return DEFAULT_BASE_URL;
}

const BASE_URL = resolveBaseURL();

/**
 * 单次请求可选项。
 */
export interface RequestOptions {
  /**
   * 工作区 ID。提供则注入 `X-Workspace-Id` header(章节路由必需)。
   * 工作区路由(`/workspaces/*`)不需要此 header。
   */
  workspaceId?: string;
  /** 额外 header,会与默认 header 合并(同名 key 由调用方覆盖)。 */
  headers?: Record<string, string>;
  /** 取消信号,透传给 fetch。 */
  signal?: AbortSignal;
}

/**
 * 标准成功响应包络形状(对齐 src/api/schemas/common.py:SuccessEnvelope)。
 *
 * 这里手写而非从 types.ts import 是因为 types.ts 把每个具体 T 都生成了一个
 * 单态版本(`SuccessEnvelope_WorkspaceOut_` 等),没生成泛型基类。client 只关心
 * 包络外壳形状,内部 T 已由调用方泛型参数化。
 */
interface SuccessEnvelopeShape<T> {
  data: T;
  meta?: unknown;
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

interface InternalRequestArgs {
  method: Method;
  path: string;
  body?: unknown;
  options?: RequestOptions;
}

async function request<T>(args: InternalRequestArgs): Promise<T> {
  const { method, path, body, options } = args;

  const url = `${BASE_URL}${path}`;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (options?.workspaceId) {
    headers["X-Workspace-Id"] = options.workspaceId;
  }
  if (options?.headers) {
    for (const [k, v] of Object.entries(options.headers)) {
      headers[k] = v;
    }
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: options?.signal,
    });
  } catch (err) {
    // 网络错误 / fetch 抛 TypeError(CORS / DNS / 连接拒绝等);AbortError 也走这里
    const reason = err instanceof Error ? err.message : String(err);
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: `网络请求失败: ${reason}`,
      httpStatus: 0,
      requestId: null,
    });
  }

  // 204 No Content / 空体:不期望 envelope。调用方应传 T = void / undefined。
  if (response.status === 204) {
    return undefined as T;
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 响应。2xx 视为空体返回;非 2xx 抛 UNKNOWN_ERROR。
    if (response.ok) {
      return undefined as T;
    }
    throw new ApiError({
      code: "UNKNOWN_ERROR",
      message: `响应解析失败,HTTP ${response.status}`,
      httpStatus: response.status,
      requestId: null,
    });
  }

  if (!response.ok) {
    const errorBody = parseErrorEnvelope(payload);
    if (errorBody === null) {
      throw new ApiError({
        code: "UNKNOWN_ERROR",
        message: `非标准错误响应,HTTP ${response.status}`,
        httpStatus: response.status,
        requestId: null,
        details: { raw: payload as Record<string, unknown> | null },
      });
    }
    throw new ApiError({
      code: errorBody.code,
      message: errorBody.message,
      httpStatus: response.status,
      requestId: errorBody.request_id,
      traceId: errorBody.trace_id ?? null,
      details: errorBody.details ?? null,
    });
  }

  // 2xx:期望 SuccessEnvelope.data 形状。
  if (
    typeof payload === "object" &&
    payload !== null &&
    "data" in (payload as object)
  ) {
    return (payload as SuccessEnvelopeShape<T>).data;
  }
  // 2xx 但不是标准包络(理论不应出现,后端所有路由都用 SuccessEnvelope):
  // 直接把 payload 作为 T 返回,把判断责任丢给调用方泛型约束。
  return payload as T;
}

/**
 * GET 请求。
 *
 * @example
 *   const ws = await apiGet<components["schemas"]["WorkspaceOut"]>("/workspaces/demo")
 */
export function apiGet<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>({ method: "GET", path, options });
}

/**
 * POST 请求。
 *
 * `body` 可省略(对应 `:write` 这种空 body 路由)。
 *
 * @example
 *   const task = await apiPost<components["schemas"]["TaskInfo"]>(
 *     "/chapters/v1c001:write",
 *     undefined,
 *     { workspaceId: "demo" },
 *   )
 */
export function apiPost<TResp, TBody = unknown>(
  path: string,
  body?: TBody,
  options?: RequestOptions,
): Promise<TResp> {
  return request<TResp>({ method: "POST", path, body, options });
}

/**
 * PUT 请求。
 */
export function apiPut<TResp, TBody = unknown>(
  path: string,
  body: TBody,
  options?: RequestOptions,
): Promise<TResp> {
  return request<TResp>({ method: "PUT", path, body, options });
}

/**
 * DELETE 请求。
 *
 * P1 后端没有 DELETE 路由(workspaces 用 `:purge` 动作而非 DELETE);本谓词
 * 为 P2+ 预留,签名稳定。
 */
export function apiDelete<T>(
  path: string,
  options?: RequestOptions,
): Promise<T> {
  return request<T>({ method: "DELETE", path, options });
}

/**
 * 暴露 baseURL 供 WS 客户端等模块复用(P1-F-06 起会用)。
 */
export function getApiBaseURL(): string {
  return BASE_URL;
}
