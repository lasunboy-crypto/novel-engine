import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { WorldOutput } from "@/api/outline";
import { OutlineSectionView } from "@/components/OutlineSectionView";

const world: WorldOutput = {
  setting: "山村与山门", power_system: "修习渐进", factions: "山门", culture: "人情", unique_rules: "",
  story_intent: {
    version: 3, reader_interest: "实际求道经历", protagonist_start: "少年",
    personal_drive: "改变生活", central_pressure: "机会难得", narrative_tone: "朴实温暖",
    opening_orientation: "认识来路并接触机会",
    user_request: { version: 1, premise: "少年求道", genre: "修仙", source_sha256: "a".repeat(64) },
    progression_design: {
      story_engine: "用学到的办法争取新机会", opening_development: "生活交代与实际尝试共同展开",
      stage_progression: "尝试的结果改变后续处境", payoff_rhythm: "所得派上用场并引出新期待",
      time_and_detail: "关键交锋展开，重复修习概述",
    },
  },
};

describe("故事推进设计展示", () => {
  it("按中文含义展示实际生成的推进设计", () => {
    render(<OutlineSectionView outline={{ world }} section="world" />);
    expect(screen.getByText("故事推进设计")).toBeInTheDocument();
    for (const label of ["持续吸引读者的动力", "开篇如何进入故事", "阶段怎样向前发展", "阶段收获与后续期待", "时间跨度与详略"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    for (const value of Object.values(world.story_intent!.progression_design!)) {
      expect(screen.getByText(value)).toBeInTheDocument();
    }
    expect(screen.queryByText("progression_design")).not.toBeInTheDocument();
  });

  it("旧书保持原定位，不展示不存在的设计", () => {
    const legacy: WorldOutput = { ...world, story_intent: { ...world.story_intent!, version: 2, progression_design: null } };
    render(<OutlineSectionView outline={{ world: legacy }} section="world" />);
    expect(screen.getByText("故事定位")).toBeInTheDocument();
    expect(screen.queryByText("故事推进设计")).not.toBeInTheDocument();
  });
});


it("新范围和阶段经历以中文展示，百分比明确是规划参考", () => {
  const current: WorldOutput = { ...world, story_intent: { ...world.story_intent!, version: 4,
    planning_scope: { mode: "opening_segment", total_volumes: 1, chapters_per_volume: 12, reference_chapter_words: 2000 },
    scope_outcome: "所学派上用场，关系改变",
  } };
  const { rerender } = render(<OutlineSectionView outline={{ world: current }} section="world" />);
  expect(screen.getByText(/长篇开局；1卷，每卷12章/)).toBeInTheDocument();
  expect(screen.getByText("所学派上用场，关系改变")).toBeInTheDocument();
  rerender(<OutlineSectionView outline={{ plot: {
    core_conflict: "普通人的选择", main_phases: [{ phase_id: "p1", title: "尝试", summary: "遭遇反馈", development: {
      experience: "试用所学", resulting_change: "获得新办法", space_share: 30, summary_processes: "重复练习",
    } }], major_turning_points: [], long_threads: [], character_arcs: {}, load_bearing_objects: [],
  } }} section="plot" />);
  expect(screen.getByText(/实际经历：试用所学/)).toBeInTheDocument();
  expect(screen.getByText(/参考篇幅占比：30%（规划参考）/)).toBeInTheDocument();
});
