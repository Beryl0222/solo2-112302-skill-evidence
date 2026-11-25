# 共同实训成果的文本对标与签发

本项目在原有语料读取能力之外，提供一项可反复办理、仅依赖 Python 标准库的“评语—标准—签认—更正—复审—补交”功能。学校学分组和企业技能组可以分别带入新一轮评语、各自采用的标准、收件方和结论用途，系统会分别给出可成立的能力/资格组合并办理签认。

## 运行检查

```bash
python3 -m unittest discover -s tests -v
```

运行内置合成案例（读取 `data/corpus.json`，输出完整办理结果 JSON）：

```bash
python3 -m accreditation demo
```

把状态保存下来，后续继续办理：

```bash
python3 -m accreditation --state /tmp/accreditation-state.json init
python3 -m accreditation --state /tmp/accreditation-state.json run \
  --request examples/round_request.json
python3 -m accreditation --state /tmp/accreditation-state.json notifications \
  --recipient 教务处
```

也可以使用等价入口 `python main.py ...`。

## 核心 Python API

```python
from accreditation import AccreditationService

office = AccreditationService(learner_name="林霁")

# 1. 企业组带入本轮采用的标准和评语
office.add_standard({
    "id": "enterprise-e1",
    "group": "enterprise",
    "text": "缺值核对与异常回填都须独立完成，且来自不同实训操作。",
})
office.adopt_standard("enterprise", "enterprise-e1",
                      purpose="enterprise_certification")
office.add_record({"id": "obs-mon", "text": "周一工位｜林霁自行核对缺值。"})
office.add_record({"id": "obs-tue",
                   "text": "周二工位｜林霁在同伴提示下完成异常回填。"})

# 2. “完成”不等于“独立完成”；这里尚不能签发企业认证
report = office.evaluate(
    "enterprise-e1", group="enterprise",
    purpose="enterprise_certification",
)
assert report["feasible"] is False

# 3. 学生补交后，查看实际缺口和新组合，再办理新签认
gap = office.submit_supplement(
    {"id": "obs-thu",
     "text": "周四独立实训｜林霁不问旁人，复填异常样表后解释了依据。"},
    standard_requests=[{
        "standard_id": "enterprise-e1", "group": "enterprise",
        "purpose": "enterprise_certification",
    }],
)
signoff = office.sign_off(
    signoff_id="ent-thu",
    group="enterprise",
    recipient="企业技能组",
    standard_id="enterprise-e1",
    purpose="enterprise_certification",
)

# 4. 教务员只能澄清人/动作归属；目标可写 fact_id、record+task 或 operation+task
office.clarify_attribution(
    decision_id="op-1",
    issuer={"id": "zhou", "role": "教务员"},
    target={"record_id": "obs-tue", "task": "exception_backfill"},
    attribution={"actor": "罗闻", "actor_kind": "peer"},
)

# 5. 标准负责人只能补发带作用范围的解释
office.issue_interpretation(
    decision_id="op-2",
    issuer={"id": "owner", "role": "标准负责人"},
    text="独立复跑的替代规则只适用于企业入门推荐。",
    standard_id="enterprise-e1",
    scope={
        "group": "enterprise",
        "purposes": ["enterprise_intro_recommendation",
                     "enterprise_certification"],
    },
    rule={
        "type": "restrict_alternative",
        "alternative_key": "independent_rerun",
        "allowed_purposes": ["enterprise_intro_recommendation"],
        "denied_purposes": ["enterprise_certification"],
    },
)

office.save("/tmp/case-state.json")
reopened = AccreditationService.open("/tmp/case-state.json")
```

也可以用 `AccreditationService.from_round(...)` 一次性传入 `comments`、`standards_by_group`。

## 输入模型

### 评语事实

中文规则解析器会从示例语体中提取：

- `actor` / `actor_kind`：完成者是学员、同伴还是转述者；
- `task`：`missing_value_check`、`exception_backfill`、`independent_rerun`、`explain_basis` 等；
- `completed`：是否完成；
- `independence`：`independent`、`prompted`、`unknown`；
- `mode`：`direct`（老师观察）或 `hearsay`（引用队友/转述）；
- `time_index`、`time_label`、`session_number`：星期、场次和时间顺序；
- `operation_id`：真实实训操作身份。

解析器保守地认为：没有独立线索时，完成事实仍是 `unknown`，不会自动算独立。`同伴提示下`、`照着`、`同伴演示` 会标成 `prompted`；`自行`、`独立`、`没请人提示`、`不问旁人` 才标成 `independent`。

新语体若规则解析不充分，可以直接在记录中带权威的结构化 `facts`：

```json
{
  "id": "obs-x",
  "text": "原始评语必须保留",
  "facts": [
    {
      "fact_id": "f-x-1",
      "task": "exception_backfill",
      "actor_kind": "learner",
      "completed": true,
      "independence": "independent",
      "mode": "direct",
      "operation_id": "op-thu-real-sample"
    }
  ]
}
```

### 标准的“且/或”和操作独立性

标准可以只给 `text`，由 `parse_standard_text` 解析示例中的“和/且……，或……并……”。更复杂的标准建议直接给 DNF 结构：

```python
{
  "id": "enterprise-e1",
  "alternatives": [
    {
      "id": "must-both",
      "requirements": [
        {"id": "r1", "key": "check", "task": "missing_value_check",
         "competency_id": "missing_value_check", "independent": True},
        {"id": "r2", "key": "backfill", "task": "exception_backfill",
         "competency_id": "exception_backfill", "independent": True}
      ]
    }
  ],
  # 这两个 key 选中的证据必须来自不同 operation_id
  "distinct_operation_keys": [["check", "backfill"]]
}
```

- 校方标准没有不同操作限制时，同一 `operation_id` 可以佐证多个能力。
- 企业标准声明 `distinct_operation_keys` 后，系统会搜索所有证据组合并强制操作不同。
- 把一次操作切成两句不会产生新的 `operation_id`，因此不能满足企业的独立性组合。
- 老师记录和队友复述若具有相同 `operation_id`，组合搜索只把它作为一个操作槽位；所有来源仍保留在 `all_evidence_fact_ids` / `record_ids` 中供追溯。

### 通用能力与专业能力

能力只按标准要求中的显式 `competency_id` 授予。`general.*` 通过不会自动满足 `professional.*`；只有标准或带作用范围的标准解释明确建立映射时才会改变口径，系统不做类别推断。

## 时间与历史签认

- 每个事实保留自己的 `time_index` 和 `operation_id`。
- `evaluate(..., as_of=2)` 排除更晚的记录，不能用周二的进步回填周一截止时的结论。
- 新签认只有在当前/指定时点存在合格组合时才能创建。
- `import_sent_signoff()` / `import_handoff_text()` 用于登记已经送出的历史签认，不篡改其原文。
- 签认保存：原始记录文本、当时采用的标准结构、适用解释、证据快照、组合和口径。后续标准或事实变化不会改写该快照。

## 权限、更正与复审

- `clarify_attribution()` 只允许 `教务员` / `academic_registrar` 角色调用，用于澄清人或动作归属。
- `issue_interpretation()` 只允许 `标准负责人` / `standard_owner` 角色调用，并且必须给非空 `scope`（组别、用途/结论类型、标准范围等）。
- 跨角色调用抛出 `PermissionDeniedError`（它是内置 `PermissionError` 的子类）。

同伴操作更正后，系统会找出受影响的校方学分签认和企业推荐/认证签认，分别用各自快照中的标准和剩余有效证据复审：

- 校方可能因一件作业可佐证多项能力或其他剩余组合而 `upheld`；
- 企业可能因两项能力只剩同一操作而令资格结论 `review_pending`；
- 收件方通知列出待复核的资格结论；
- 已送出的签认本身仍保留 `status: sent`、原文和当时口径，复核结果以独立 `Review` 和通知追加。

## 补交与新组合

`submit_supplement()` 会：

1. 分别计算补交前/后的标准报告；
2. 标出补交证据实际填补的是缺失要求还是“缺少第二个独立操作”的组合缺口；
3. 列出此前已引用和未引用的证据；
4. 搜索仅由旧的未引用证据组成的合格组合；
5. 搜索包含补交记录且使用了未引用旧证据的新组合。

## 持久状态

`AccreditationService.save(path)` 原子写入 JSON；`AccreditationService.open(path)` 重新打开后可继续导入新轮次、办理更正、复审和补交。状态中的对象均为 JSON 友好的字典、列表和标量。

## 语料说明

`data/corpus.json` 仍是合成演练资料。`materials.load_corpus()` 保持不变；新增代码位于 `accreditation/`，新增回归测试位于 `tests/test_accreditation.py`。
