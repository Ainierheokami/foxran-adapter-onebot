# 已归档测试

这里的测试已被取代，pytest 不收集，仅供追溯。

- `test_group_history_only.py`：Foxran 核心重构 R3a 把入站决策（只记历史、前置命令等）移到了宿主的入站管线，
  行为由宿主 `tests/inbound/test_onebot_inbound_behaviour.py` 覆盖；提及 ID 提取由本仓库 `tests/test_event_facts.py` 覆盖。
