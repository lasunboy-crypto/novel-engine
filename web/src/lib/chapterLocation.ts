export interface ChapterLocation {
  volumeId: string;
  chapterNumber: number;
}

interface VolumeChapterCount {
  volume_id: string;
  chapter_count: number;
}

/** 从全书 ``ch-NNNN`` 序号与已提交卷章数推导卷内位置。 */
export function resolveChapterLocation(
  chapterId: string,
  volumes: readonly VolumeChapterCount[],
): ChapterLocation | null {
  const match = /^ch-(\d+)$/.exec(chapterId);
  if (!match) return null;
  const absoluteNumber = Number(match[1]);
  if (!Number.isSafeInteger(absoluteNumber) || absoluteNumber < 1) return null;

  let offset = 0;
  for (const volume of volumes) {
    const nextOffset = offset + volume.chapter_count;
    if (absoluteNumber <= nextOffset) {
      return {
        volumeId: volume.volume_id,
        chapterNumber: absoluteNumber - offset,
      };
    }
    offset = nextOffset;
  }
  return null;
}
