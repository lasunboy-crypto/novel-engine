# 静态资产导入

把人物 / 世界观 / 伏笔 / 时间线静态资产从一个 markdown 目录批量导入到工作
区的 SQLite + Chroma:

```bash
uv run novel workspace import-assets <md_dir> [--workspace WS_ID]
```

`md_dir` 下识别 4 个文件,均**可选**:

| 文件 | 落 SQLite 表 | 落 Chroma collection |
|------|-------------|---------------------|
| `characters.md` | `characters` | `memories`(`entity_type=character`) |
| `plot_threads.md` | `plot_threads` | `memories`(`entity_type=plot_thread`) |
| `timeline.md` | `timeline_entries` | `memories`(`entity_type=timeline`) |
| `world.md` | (不落表;P2 阶段) | `memories`(`entity_type=fact`) |

同名条目幂等覆盖:SQLite 走 get-or-update-or-create,Chroma 同 ID 是 upsert。

### 文件格式约定

每个文件由若干 **`## <显示名>`** 节组成,每节后接一个 markdown 管道表
`| 字段 | 值 |`,实体 ID 由表内 `*_id` 字段提供(`character_id` /
`thread_id` / `entry_id` / `fact_id`)。`##` 之上的 `# 文件标题` 与节外文
本被忽略。

#### `characters.md` 示例

```markdown
## 林衍

| 字段 | 值 |
|------|----|
| character_id | lin-yan |
| name | 林衍 |
| archetype | 萌新剑修 |
| appearance | 黑衣黑发 |
| personality | 沉静内敛 |
| background | 寒门出身,机缘获剑 |
| tone | 冷静 |
| sentence_style | 短句直白 |
| self_address | 我 |
| aliases | 衍儿,小林 |
| catchphrases | 剑随心动 |
| forbidden_phrases | 现代俚语 |
| addresses_others | hero_001:小子;villain_001:阁下 |
| relationships | hero_001:同门;villain_001:宿敌 |
| goals_long_term | 修剑成圣 |
| goals_short_term | 突破筑基 |
| abilities | 御剑诀,基础剑法 |
| secrets | 玉佩中藏剑魂 |
| first_appearance_chapter | ch-0001 |
```

必填字段:`character_id` / `archetype` / `appearance` / `personality` /
`background` / `tone` / `sentence_style` / `self_address`(其余可空)。

#### `plot_threads.md` 示例

```markdown
## 玉佩之约

| 字段 | 值 |
|------|----|
| thread_id | yu-pei-zhi-yue |
| title | 玉佩之约 |
| status | planted |
| importance | 4 |
| planted_chapter | ch-0001 |
| description | 林衍获赠玉佩;来历未明 |
| related_characters | lin-yan,su-li |
| planned_payoff | ch-0050 |
```

必填:`thread_id` / `status` / `importance` / `planted_chapter` /
`description`。`status` 取值 `planted | developing | resolved | abandoned`;
`importance` 范围 `1-5`。`created_at` 由服务自动填,update 路径保留原值。

#### `timeline.md` 示例

```markdown
## 林衍上山

| 字段 | 值 |
|------|----|
| entry_id | lin-yan-shang-shan |
| chapter_id | ch-0001 |
| description | 林衍登青崖山求道 |
| time_label | 建元三年春 |
| time_order | 100 |
| involved_characters | lin-yan |
```

必填:`entry_id` / `chapter_id` / `description` / `time_label` /
`time_order`(整数,严格递增由 Reviewer 后续校验)。

#### `world.md` 示例

```markdown
## 修真等级体系

| 字段 | 值 |
|------|----|
| fact_id | xiu-zhen-deng-ji |
| description | 修真分九境,每三境为一阶,境界突破需吸纳天地灵气并悟道 |
| involved_characters | (无) |
```

必填:`fact_id` / `description`。`world.md` 每节是一个语义 fact,**只入
Chroma**(P2 阶段无 facts 表,P3+ 视需要再加)。

### 字段值约定

- **列表字段**(`aliases` / `goals_long_term` / `catchphrases` /
  `forbidden_phrases` / `related_characters` / `involved_characters` 等):
  英文逗号分隔。`(无)` / `(空)` / `-` 等占位字面 → 空列表。
- **dict 字段**(`addresses_others` / `relationships`):分号分隔的 `key:value`
  对,例 `hero_001:小子;villain_001:阁下`。
- **整数字段**(`importance` / `time_order`):纯整数。
- **可选字段**省略行 → 用 Pydantic 默认值(空列表 / `None`)。
- **Chroma metadata 五字段**:`entity_type` / `entity_id` / `chapter_id` /
  `workspace_id` / `created_at`。静态资产无章节归属,`chapter_id` 占位
  `__static__`(对齐 P2-B-13 决策)。

### 边界

- `md_dir` 不存在 / 非目录 → CLI 返退出码 1 并 stderr 报错。
- 必填字段缺失 / `importance` 非整数 / `status` 非合法枚举 → 同上。
- `md_dir` 下 4 个文件全缺失 → counts 全 0,无报错。
- 工作区 SQLite 文件首次访问由 CLI 自动 `Base.metadata.create_all` 建 schema
  (idempotent;不与 alembic 迁移冲突)。
