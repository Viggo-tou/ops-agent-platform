---
triggers:
  - 继续改动
  - 重新改
  - 再改
  - iterate
  - follow up
  - 续写
  - 之前的任务
  - 上次的 diff
  - 重新做
task_type:
  - iterate
---

# 任务续写 / 迭代 — 不要新建任务,用 /iterate

## 用户已经有一个任务,只是想换个改法

如果用户说"再改改 / 改完 README 也加点 / 那个任务跑挂了再来一次",**不要**输出 `TASK_INTENT` 创建新任务。

## 正确做法

告诉用户:

> 你之前那个任务在 `/tasks/{id}` 页面,底部有 **继续改动** 输入框。在那里写后续指令,会创建一个续写任务自动接住前一个任务的 plan + diff + 编译错误,不用从零再来。

并附上链接(如果你能从 history 拿到 task_id)。

## 后端的迭代链路(给你做心智模型,不要复述给用户)

- `POST /api/tasks/{id}/iterate { follow_up }` 创建子任务
- 子任务继承父任务的 `session_id` / `scenario` / `source_name`
- 父任务的 plan / 拒绝过的 patch / compile 错误自动塞进子任务的 prompt
- 父任务 in-flight (running / planning / executing) 时返回 409,等终态再迭代

## 你不应该做的

- ❌ 反复创建同 session 的新任务(scenario=jira_issue_develop)而不用 iterate
- ❌ 把"再改改"当成全新任务从头规划
- ❌ 推荐用户去前端"新建任务"而不是 iterate
