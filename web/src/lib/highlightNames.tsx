/**
 * highlightNames(v2 W3.5 UI 缺口1/2):在自由文字里红字高亮"涉及的实体名"。
 *
 * 背景:大纲一致性 / 结构闸 validator 只返**文字描述**(不返精确字位),所以无法
 * 按 offset 高亮。折中 = 拿已知实体名列表(角色 / 卷 / 弧 等)在文字里做子串搜索,
 * 命中处包 `<mark>`。纯展示用,匹配不到就原样返回(优雅降级)。
 *
 * 实现要点:
 * - 按名字长度降序匹配(长名优先,避免"陆" 抢先于 "小陆")。
 * - 大小写 / 全角不敏感处理从简(中文名为主,直接子串匹配)。
 * - 名字去重 + 过滤空串,避免空匹配死循环。
 */

import type { ReactNode } from "react";

export function highlightNames(text: string, names: string[]): ReactNode[] {
  const cleaned = Array.from(
    new Set(names.map((n) => n.trim()).filter((n) => n.length > 0)),
  ).sort((a, b) => b.length - a.length);

  if (cleaned.length === 0) return [text];

  // 构造交替匹配正则;转义正则特殊字符。
  const escaped = cleaned.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escaped.join("|")})`, "g");

  const nameSet = new Set(cleaned);
  const parts = text.split(re);
  return parts.map((part, i) =>
    nameSet.has(part) ? (
      <mark
        key={i}
        data-testid="highlighted-name"
        className="rounded bg-transparent px-0.5 font-semibold text-red-600 dark:text-red-400"
      >
        {part}
      </mark>
    ) : (
      part
    ),
  );
}
