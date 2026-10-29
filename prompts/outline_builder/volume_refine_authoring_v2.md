# 确认方向后的单卷重新编排

输出VolumePlansOutput，volumes只含输入当前一卷。你可以合并重复准备、概述等待、重排或补足实际进入主线后的不同经历；可以增删arc/beat并分配新稳定ID。不要因为原稿已给章号，就保护明显迟缓的安排。

保留世界事实、人物身份、主线因果、承重物件机制、本卷在全书中的分工和重要结果。linked_phases/linked_turning_points/linked_threads保持；跨卷主线变更留在全书规划阶段。原草稿的分镜、章数和排期不是已发生事实，排期型forbidden随新布局重算。固定章数用户要求仍遵守；参考章数允许调整。

每章恰好归属一个beat，tension_curve长度与章数一致。authoring_plan节点只挂在真正发生对应事件的beat上，不能仅贴ID；保留原本分配到本卷的节点。首卷按用户选择的开篇铺垫窗口进入核心经历，不能将全书长度按比例变成准备长度。

required_events只保留必要事件/结果；contract_version=2。情感、心理、生活趣味不设配额；有效的相处、告别和静戏允许充分展开，普通准备可以跳过。不给Writer提前写台词、表情和动作分镜，不要求证明每条背景或能力规则。

# 与现有程序检查一致的结构填写

所有 arc.chapter_range 和 beat.chapter_range 都用本卷的卷内编号：每一卷从 1 开始，到本卷 chapter_count 结束；不要把第二卷写成 21—40 这类全书累计章号。跨卷顺序由 volumes 数组表示。

使用 beat.dramatic_contract 时，contract_version=2，required_events 至少一条明确的实际事件或结果；同一 beat.must_advance 也必须非空，登记同一必要推进。thread_id 或 turning_id 至少绑定一个输入中的既有引用，并同时加入本卷 linked_threads 或 linked_turning_points。不得新造引用，也不为填字段虚构冲突。

凡本卷有 dramatic_promise 或任一 typed dramatic_contract，本卷必须有一个 dramatic_promise，并且全卷恰好一个 function=finale 的 dramatic_contract，其所属 beat.chapter_range 的终点等于本卷 chapter_count。这个卷末合同与 promise 使用相同 actor/counterforce/thread/turning，按既有 outcome 规则登记本卷结果或合法延期。卷末结果可以是获得机会、关系或处境改变，不必是战斗，也不代表全书已经结束；同一完成事件不要放在多个 beat 重演。没有需要使用的合同，普通探索、独处、关系和静戏仍可不设合同，不设对白、心理或爽点次数。
