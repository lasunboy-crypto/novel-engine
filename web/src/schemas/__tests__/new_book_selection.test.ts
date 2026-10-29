import { describe, expect, it } from "vitest";

import {
  buildPostV8AbcSelectionBookSchema,
  newBookSchema,
} from "@/schemas/new_book";
import { buildPostV8AbcSelectionValues } from "@/selection/postV8AbcSelection";

describe("POST-V8 ABC selection-only 三章 schema", () => {
  it("普通 New Book 继续拒绝每卷 3 章", () => {
    const values = buildPostV8AbcSelectionValues("hy3");
    expect(newBookSchema.safeParse(values).success).toBe(false);
  });

  it("私有 selection schema 只接受该候选逐字冻结输入", () => {
    const values = buildPostV8AbcSelectionValues("hy3");
    const schema = buildPostV8AbcSelectionBookSchema(values);
    expect(schema.safeParse(values).success).toBe(true);
    expect(
      schema.safeParse({
        ...values,
        premise: "人为调低一家难度",
      }).success,
    ).toBe(false);
    expect(
      schema.safeParse({
        ...values,
        chapters_per_volume: 6,
      }).success,
    ).toBe(false);
  });

  it("qwen3.8-27b 只在 selection schema 接受带点号的精确保留 workspace", () => {
    const values = buildPostV8AbcSelectionValues("qwen3.8-27b");
    const schema = buildPostV8AbcSelectionBookSchema(values);

    expect(newBookSchema.safeParse(values).success).toBe(false);
    expect(schema.safeParse(values).success).toBe(true);
    expect(
      schema.safeParse({
        ...values,
        workspace_id: "post-v8-abc-qwen3-8-27b-selection-v1",
      }).success,
    ).toBe(false);
  });
});
