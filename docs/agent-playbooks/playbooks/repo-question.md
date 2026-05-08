---
triggers:
  - readme
  - 项目结构
  - 目录结构
  - 文件结构
  - 哪个文件
  - 在哪里
  - 在哪
  - SessionManager
  - 代码结构
  - file tree
  - directory tree
  - repo structure
  - project structure
  - where is
task_type:
  - lookup
  - debug
---

# 仓库 / 文档查询 — 怎么回答 "README 写了啥""SessionManager 在哪""项目结构是啥"

## 不要凭模型记忆答这类问题

repo / 代码结构 / 文件位置 / 符号定义 类问题,**模型没有最新代码**。
直接答 = 编。

## 你应该这样答

1. 先看下文 "## 检索到的代码 / 文档片段" 段(chat 后端会自动注入)
2. 完全基于片段里的内容答,引用文件路径 (`apps/.../File.kt:42-58`) 或文档名
3. 片段里没的,**说不在检索结果里**,**不要补**;鼓励用户问得更具体或 sync 知识库

## 关键:一定要标 source

每条事实后面跟 `(<source>:<file>:<line>)`,例:

> `SessionManager` 定义在 `app/src/main/java/com/example/handyman/session/SessionManager.kt:18-95`,负责持有当前用户的 token 与 profile。

不带 source 的事实 ≈ 编,用户不会信。

## 当前注册的代码库

system prompt 顶部 "## 当前平台状态 → ### 已注册代码库" 一段是 live 的,直接报数即可。
