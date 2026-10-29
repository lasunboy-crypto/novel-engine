import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { components } from "@/api/types";
import { CoreStateAuditPanel } from "@/components/CoreStateAuditPanel";

const mockAuditQuery = vi.fn();

vi.mock("@/api/core_state", () => ({
  useCoreStateAuditQuery: () => mockAuditQuery(),
}));

function auditData(): components["schemas"]["CoreStateAuditOut"] {
  return {
    projection: {
      projection_hash: "a".repeat(64),
      book_time_contract_version: 0,
      book_time: null,
      book_clock: {
        minimum_minutes: 11_520,
        maximum_minutes: 15_840,
      },
      facts: [
        {
          revision_id: "csr-mirror",
          state_key: "object:青铜镜:surface",
          entity_type: "object",
          entity_id: "青铜镜",
          attribute: "surface",
          value: "无锈无氧化",
          source_kind: "approved_text",
          source_chapter_id: "ch-0001",
          source_version_id: 1,
          source_path: "chapter_summaries/ch-0001.core_state_candidates",
          source_text_hash: "b".repeat(64),
          evidence_quote: "青铜镜镜面无锈无氧化。",
          valid_from_chapter: 1,
          valid_to_chapter: null,
        },
      ],
    },
    items: [
      {
        item_type: "revision",
        revision_id: "csr-mirror",
        state_key: "object:青铜镜:surface",
        entity_type: "object",
        entity_id: "青铜镜",
        attribute: "surface",
        value: "无锈无氧化",
        status: "active",
        source_chapter_id: "ch-0001",
        source_version_id: 1,
        source_path: "chapter_summaries/ch-0001.core_state_candidates",
        source_text_hash: "b".repeat(64),
        evidence_quote: "青铜镜镜面无锈无氧化。",
        valid_from_chapter: 1,
        valid_to_chapter: null,
        reason: "首次建立",
      },
      {
        item_type: "candidate",
        revision_id: null,
        state_key: "object:青铜镜:surface",
        entity_type: "object",
        entity_id: "青铜镜",
        attribute: "surface",
        value: "暗绿色铜锈",
        status: "rejected_conflict",
        source_chapter_id: "ch-0008",
        source_version_id: 1,
        source_path: "chapter_summaries/ch-0008.core_state_candidates",
        source_text_hash: "c".repeat(64),
        evidence_quote: "暗绿色的锈。",
        valid_from_chapter: 8,
        valid_to_chapter: null,
        reason: "卡片无匹配 from→to 许可",
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAuditQuery.mockReturnValue({
    data: auditData(),
    isPending: false,
    isError: false,
  });
});

describe("CoreStateAuditPanel", () => {
  it("展示当前值、书内时钟、来源证据和冲突候选", async () => {
    const user = userEvent.setup();
    render(
      <CoreStateAuditPanel
        workspaceId="ws-core"
        card={{
          core_state_contract_version: 1,
          core_state_projection_hash: "a".repeat(64),
          core_state_transitions: [
            {
              state_key: "object:青铜镜:surface",
              entity_type: "object",
              entity_id: "青铜镜",
              attribute: "surface",
              from_value: "无锈无氧化",
              to_value: "暗绿色铜锈",
              reason: "卡片明示腐蚀过程",
            },
          ],
          book_clock_constraint: {
            time_gap: "次日",
            previous_interval: {
              minimum_minutes: 10_080,
              maximum_minutes: 14_400,
            },
            gap_interval: {
              minimum_minutes: 1_440,
              maximum_minutes: 1_440,
            },
            current_interval: {
              minimum_minutes: 11_520,
              maximum_minutes: 15_840,
            },
            is_hard: true,
            soft_reason: "",
          },
        }}
      />,
    );

    expect(screen.getByText("全书核心状态与书内时钟")).toBeInTheDocument();
    expect(screen.getByText(/青铜镜\.surface = 无锈无氧化/)).toBeInTheDocument();
    expect(screen.getAllByText(/11520–15840 分钟/)).toHaveLength(2);
    expect(screen.getAllByText(/青铜镜镜面无锈无氧化/)).toHaveLength(2);
    expect(screen.getByText(/无锈无氧化 → 暗绿色铜锈/)).toBeInTheDocument();
    expect(screen.getByText(/请从对应章节执行 Rewind/)).toBeInTheDocument();

    await user.click(screen.getByText("Revision / 来源历史（1）"));
    expect(screen.getAllByText(/ch-0001\/v1/)).toHaveLength(2);

    await user.click(screen.getByText("未准入 / 冲突候选（1）"));
    expect(screen.getByText(/卡片无匹配 from→to 许可/)).toBeInTheDocument();
  });

  it("typed 书内时间只展示自然关系，不显示累计分钟", () => {
    const data = auditData();
    const currentNode: components["schemas"]["BookTimeNode"] = {
      node_id: "d".repeat(64),
      chapter_id: "ch-0002",
      kind: "ordered_relative",
      raw_time_gap: "同夜稍后，约一小时后",
      anchor_node_id: "e".repeat(64),
      relations: ["after", "same_night"],
      day_phase: "night",
      approximate_quantity: { value: 1, unit: "hour" },
      gap_interval: null,
      absolute_interval: null,
      unanchored_reason: null,
    };
    data.projection = {
      ...data.projection,
      book_clock: null,
      book_time_contract_version: 1,
      book_time: {
        contract_version: 1,
        current_node: currentNode,
        latest_mainline_node_id: "d".repeat(64),
        latest_absolute_anchor: {
          node_id: "e".repeat(64),
          interval: { minimum_minutes: 0, maximum_minutes: 0 },
        },
        active_relation_chain: [currentNode],
        relation_chain_hash: "f".repeat(64),
      },
    };
    mockAuditQuery.mockReturnValue({ data, isPending: false, isError: false });

    render(
      <CoreStateAuditPanel
        workspaceId="ws-core"
        card={{
          core_state_contract_version: 1,
          core_state_projection_hash: "a".repeat(64),
          core_state_transitions: [],
          book_clock_constraint: {
            contract_version: 1,
            time_gap: "同夜稍后，约一小时后",
            previous_interval: null,
            gap_interval: null,
            current_interval: null,
            kind: "ordered_relative",
            node_id: "d".repeat(64),
            node_chapter_id: "ch-0002",
            anchor_node_id: "e".repeat(64),
            relations: ["after", "same_night"],
            day_phase: "night",
            approximate_quantity: { value: 1, unit: "hour" },
            unanchored_reason: null,
            relation_chain_hash: "f".repeat(64),
            is_hard: true,
            soft_reason: "",
          },
        }}
      />,
    );

    expect(screen.getAllByText(/同夜稍后，约一小时后/)).toHaveLength(2);
    expect(screen.getAllByText(/晚于上一主线节点/)).toHaveLength(2);
    expect(screen.getAllByText(/同一叙事夜/)).toHaveLength(2);
    expect(screen.getAllByText(/时段 夜间/)).toHaveLength(2);
    expect(screen.getAllByText(/不生成分钟上下界/)).toHaveLength(2);
    expect(screen.queryByText(/same_night|night/)).not.toBeInTheDocument();
    expect(screen.queryByText(/相对开篇/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0–0 分钟/)).not.toBeInTheDocument();
  });
});
