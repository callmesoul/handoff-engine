---
description: 把当前会话里需要后端做的部分交接出去
---

> **已废弃（2026-11-05）**：本文件不再作为执行入口。现行做法是 `.agents/skills/handoff/SKILL.md`，
> 它只调用 `handoffctl create`，不自己拼 `git add/commit/push`，也不直接写 `tasks/*.json`。
> 正文保留作为早期设计的历史记录。

分析当前对话，把需要后端实现的部分抽出来，写成一张任务单。

**先判断**：需求清楚就直接写。有歧义先问我一句，别自己猜。

## 抽什么

**requirement**：字段级。哪个接口、什么方法、请求什么样、响应什么样、边界怎么办。不复述我说过的话，写成后端能照着做的。

**acceptance.checks**：可执行的验收命令。能用 `{{base_url}}` 引用验收环境地址。写不出可执行命令，说明需求还没想清楚，回头问我。

**out_of_scope**：明确不在范围内的事，防止后端顺手改别的。

**title**：一句话，二十字以内。

**base_commit**：前端仓库当前 HEAD，`git rev-parse HEAD`。

## 写到哪

交接仓的 `tasks/` 目录，文件名 `<id>.json`。

id 格式 `ho-YYYYMMDD-序号`。序号当天从 001 起，先看目录里已有哪些文件，别撞。

字段严格按 `IMPLEMENTATION.md` 里的任务单定义，不要自己加字段。

## 然后

```
git -C <交接仓路径> add -A
git -C <交接仓路径> commit -m "ho-xxx open"
git -C <交接仓路径> push
```

推上去就是送达。不需要额外通知。

## 报告

把 id 告诉我，加一句「要验的时候跑 /deliver ho-xxx」。

## 不要

不要塞整段会话进去，只要契约。

不要动业务仓库。
