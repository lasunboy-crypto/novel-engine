# 已确认小说规划的全书展开

输出符合VolumePlansOutput的完整JSON；稳定ID、已有人物/主线转折/伏笔/物件引用必须来自输入。authoring_plan的已选内容是用户确定的故事方向，不能重新设计成另一部小说。备选与历史稿不是当前事实。

先决定哪些经历独立展开、哪些融入主要事件、哪些概述，再分配章节。各卷章数若非fixed_chapter_counts则是参考，可合理重新分配；开局尺度独立于全书长度。家事、愿望、牵挂随行动展开，不必先结清；保留真正有分量的相处、告别和静戏，普通准备和反复验证可合并概述。

每个规划milestone恰好分配给一个实际演出相应事件的beat.planning_milestones。不是贴上ID就算完成；summary必须包含真正的相应经历。core_entry必须进入本书核心活动，不是仅获得消息或决定出发。direct首章，brief第4章前，extended第7章前；suggested服从故事。涉及节点的整个beat范围应落在窗口内，不能把节点藏在跨越十多章的段落里。

每章恰好归属一个beat。使用唯一稳定ID。volumes与用户卷数一致；各卷linked_phases/linked_turning_points/linked_threads来自主线。tension_curve与章数一致；张力不要求每章对抗。

高价值转折可使用dramatic_promise及dramatic_contract，contract_version=2，required_events只含必要的实际事件和结果；load_bearing_objects逐字沿用plot目录的机制和来源。普通探索、独处、关系发展允许没有戏剧合同。forbidden_advance只限确实不能提前发生的结果与披露，不限制自然心理、表情、对话或生活感。不写成台词、机位、动作顺序清单。

# 与现有程序检查一致的结构填写

所有 arc.chapter_range 和 beat.chapter_range 都用本卷的卷内编号：每一卷从 1 开始，到本卷 chapter_count 结束；不要把第二卷写成 21—40 这类全书累计章号。跨卷顺序由 volumes 数组表示。

使用 beat.dramatic_contract 时，contract_version=2，required_events 至少一条明确的实际事件或结果；同一 beat.must_advance 也必须非空，登记同一必要推进。thread_id 或 turning_id 至少绑定一个输入中的既有引用，并同时加入本卷 linked_threads 或 linked_turning_points。不得新造引用，也不为填字段虚构冲突。

凡本卷有 dramatic_promise 或任一 typed dramatic_contract，本卷必须有一个 dramatic_promise，并且全卷恰好一个 function=finale 的 dramatic_contract，其所属 beat.chapter_range 的终点等于本卷 chapter_count。这个卷末合同与 promise 使用相同 actor/counterforce/thread/turning，按既有 outcome 规则登记本卷结果或合法延期。卷末结果可以是获得机会、关系或处境改变，不必是战斗，也不代表全书已经结束；同一完成事件不要放在多个 beat 重演。没有需要使用的合同，普通探索、独处、关系和静戏仍可不设合同，不设对白、心理或爽点次数。
