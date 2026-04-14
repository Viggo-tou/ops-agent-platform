"""Repair UTF-8/GBK double-encoded mojibake in files Codex wrote.

The transform: read file as UTF-8, encode the resulting string as GBK,
decode the resulting bytes as UTF-8. For ASCII-only content this is a
no-op; for mojibake CJK it recovers the original characters.
"""
import sys
from pathlib import Path

ROOT = Path(r"d:/项目/Ops_agent_platform")
TARGETS = [
    "apps/web/src/components/chat/ChatInput.tsx",
    "apps/web/src/components/chat/MessageList.tsx",
    "apps/web/src/components/knowledge/KnowledgeSourceList.tsx",
    "apps/web/src/components/knowledge/KnowledgeUploadPanel.tsx",
    "apps/web/src/components/layout/AppShell.tsx",
    "apps/web/src/components/layout/ConversationList.tsx",
    "apps/web/src/components/memory/MemoryPanel.tsx",
    "apps/web/src/components/settings/ModelSelector.tsx",
    "apps/web/src/pages/chat/ChatPage.tsx",
    "apps/web/src/pages/home/HomePage.tsx",
    "apps/web/src/pages/knowledge/KnowledgePage.tsx",
    "apps/web/src/pages/memory/MemoryPage.tsx",
    "apps/web/src/pages/settings/SettingsPage.tsx",
    "apps/web/src/styles.css",
]

report = []
for rel in TARGETS:
    p = ROOT / rel
    raw = p.read_text(encoding="utf-8")
    try:
        fixed = raw.encode("gbk").decode("utf-8")
    except UnicodeEncodeError as e:
        report.append((rel, "SKIP-encode", str(e)[:60]))
        continue
    except UnicodeDecodeError as e:
        report.append((rel, "SKIP-decode", str(e)[:60]))
        continue
    if fixed == raw:
        report.append((rel, "UNCHANGED", "ascii-only"))
        continue
    p.write_text(fixed, encoding="utf-8", newline="\n")
    report.append((rel, "FIXED", f"{len(raw)} -> {len(fixed)} chars"))

for rel, status, note in report:
    print(f"{status:10} {rel}  -- {note}")
