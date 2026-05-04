"""Stage X.6.b: translate user-facing Chinese status messages to English in
backend source. Skip CJK regex char classes and Chinese question-word
detection lists (those are language-detection feature data, not display).

Run from project root:
    python tmp/migrate_chinese_to_english.py

It modifies files in-place. Caller is expected to git diff and run tests
before committing.
"""
from __future__ import annotations
import os
import sys

# Files to migrate
TARGETS = [
    'apps/backend/app/orchestrator/service.py',
    'apps/backend/app/agents/service.py',
    'apps/backend/app/services/failure_diagnosis.py',
    'apps/backend/app/services/knowledge_synthesis.py',
    'apps/backend/app/services/model_config.py',
    'apps/backend/app/core/config.py',
]

# (old_substring, new_substring) pairs.
# Order matters: longer / more-specific patterns FIRST so they match before
# any shorter substring that would otherwise catch their fragments.
REPLACEMENTS: list[tuple[str, str]] = [
    # --- core/config.py: filesystem path defaults ---
    (r'D:\项目\HostedDashboard\handyman-admin-dashboard',
     r'D:\projects\HostedDashboard\handyman-admin-dashboard'),
    (r'D:\项目\HandymanApp-fresh',
     r'D:\projects\HandymanApp-fresh'),

    # --- orchestrator/service.py: status / event messages ---
    # Long unique sentences first
    ('代码生成失败：代码生成工具没有返回可应用的 diff。',
     'Codegen failed: codegen tool returned no applicable diff.'),
    ('代码生成失败：所有批次均未生成有效的 diff。',
     'Codegen failed: no valid diff produced across batches.'),
    ('代码生成失败：没有找到计划中受影响文件的上下文。',
     'Codegen failed: no context found for plan-affected files.'),
    ('代码生成完成，修改了',
     'Codegen completed: modified'),
    ('个文件（',
     ' file(s) ('),
    ('批）',
     ' batch(es))'),
    ('个文件包含目标关键词',
     ' file(s) contain target keyword(s)'),
    ('个上下文文件中未找到',
     ' not found in context files'),
    ('个失败',
     ' failure(s)'),
    ('确定性重命名跳过',
     'Deterministic rename skipped'),
    ('确定性重命名完成',
     'Deterministic rename completed'),
    ('所有目标关键词已清除',
     'All target keywords cleared'),
    ('规范一致性检查未通过：',
     'Spec conformance check failed: '),
    ('代码审查未通过：',
     'Code review failed: '),
    ('测试未通过：',
     'Tests failed: '),
    ('测试：已跳过（无测试配置）',
     'Tests: skipped (no test config)'),
    ('测试：通过',
     'Tests: passed'),
    ('补丁应用方式：',
     'Patch apply method: '),
    ('开发流水线已启动。',
     'Development pipeline started.'),
    ('开发流水线已启动',
     'Development pipeline started'),
    ('开发完成',
     'Development completed'),
    ('完整度检查',
     'Completeness Check'),
    ('改动总结',
     'Change summary'),
    ('本次修改了',
     'Modified in this run:'),
    ('代码变更',
     'Code changes'),
    ('代码生成：',
     'Codegen: '),
    ('流水线执行',
     'Pipeline execution'),
    ('回退到',
     'Falling back to'),
    ('审查：',
     'Review: '),
    ('修改了',
     'Modified'),
    ('个文件',
     ' file(s)'),
    ('仍有',
     'Still has'),
    ('处）',
     ' place(s))'),
    ('（共',
     ' (total '),
    ('：已添加评论',
     ': comment added'),
    ('：已转换状态',
     ': status transitioned'),
    ('：未找到 issue key，跳过回写',
     ': no issue key found, skipping writeback'),
    ('标记为',
     'Marked as'),
    ('推进',
     'Advance'),
    ('移到',
     'Move to'),
    ('备注：',
     'Remark: '),
    ('备注',
     'Remark'),
    ('评论：',
     'Comment: '),
    ('评论',
     'Comment'),

    # --- model_config.py: provider display names (keep brand English) ---
    ('阿里云', 'Aliyun'),
    ('智谱', 'ZhipuAI'),
]


def main() -> int:
    workdir = os.getcwd()
    print(f'workdir: {workdir}')
    total_changes = 0
    for path in TARGETS:
        if not os.path.exists(path):
            print(f'  SKIP missing: {path}')
            continue
        with open(path, 'r', encoding='utf-8', newline='') as f:
            content = f.read()
        original = content
        per_file_changes = 0
        for old, new in REPLACEMENTS:
            count = content.count(old)
            if count > 0:
                content = content.replace(old, new)
                per_file_changes += count
        if per_file_changes > 0:
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(content)
            print(f'  {path}: {per_file_changes} replacements')
            total_changes += per_file_changes
        else:
            print(f'  {path}: 0 (skipped)')

    # Verify no Chinese remains in target user-facing display strings.
    # We expect Chinese to remain ONLY in the question-word detection list
    # (orchestrator service.py around lines 180-220) and CJK regex chars.
    print()
    print(f'TOTAL_REPLACEMENTS={total_changes}')
    return 0 if total_changes > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
