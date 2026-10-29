import { describe, expect, it } from "vitest";

import { resolveChapterLocation } from "@/lib/chapterLocation";

describe("resolveChapterLocation", () => {
  const volumes = [
    { volume_id: "v1", chapter_count: 12 },
    { volume_id: "v2", chapter_count: 8 },
  ];

  it("maps a global chapter id to the first volume", () => {
    expect(resolveChapterLocation("ch-0002", volumes)).toEqual({
      volumeId: "v1",
      chapterNumber: 2,
    });
  });

  it("maps across volume boundaries to a volume-local number", () => {
    expect(resolveChapterLocation("ch-0013", volumes)).toEqual({
      volumeId: "v2",
      chapterNumber: 1,
    });
    expect(resolveChapterLocation("ch-0020", volumes)).toEqual({
      volumeId: "v2",
      chapterNumber: 8,
    });
  });

  it("fails closed for malformed or out-of-range chapter ids", () => {
    expect(resolveChapterLocation("chapter-2", volumes)).toBeNull();
    expect(resolveChapterLocation("ch-0000", volumes)).toBeNull();
    expect(resolveChapterLocation("ch-0021", volumes)).toBeNull();
  });
});
