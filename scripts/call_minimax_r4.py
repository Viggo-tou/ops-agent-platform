"""Call MiniMax to generate T-R4 code: language detection + localized summary."""
import httpx
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "backend"))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "apps", "backend", ".env"))

api_key = os.getenv("OPS_AGENT_MINIMAX_API_KEY")
if not api_key:
    print("ERROR: No MiniMax API key found")
    sys.exit(1)

url = "https://api.minimaxi.com/v1/text/chatcompletion_v2"

prompt = r"""You are a Python code generator. Output ONLY valid Python code. No explanations, no markdown fences, no text before or after.

TASK: Write two Python functions.

FUNCTION 1: detect_user_language(text: str) -> str
- Uses re module to count CJK characters (Unicode range \u4e00-\u9fff and \u3400-\u4dbf)
- If more than 10% of non-space characters are CJK, return "zh"
- Otherwise return "en"
- Handle empty string (return "en")

FUNCTION 2: _build_develop_summary(self, pipeline_state: dict[str, object]) -> str
- This is a method on a class (takes self)
- Read user_lang from pipeline_state.get("user_lang", "en")
- Extract issue_key same as before: try pipeline_state["issue_key"], fallback to jira_writeback comment/transition
- Build a markdown summary with these sections:

If user_lang == "zh":
  Header: "## {issue_key} 开发完成\n"
  Files: "**修改了 {count} 个文件：**" then "- `{path}`" for each
  Diff: "**代码变更：**" then ```diff block
  Pipeline header: "**流水线执行：**"
  Code gen: "- 代码生成：{provider}"
  Patch: "- 补丁应用方式：{method}"
  Test skipped: "- 测试：已跳过（无测试配置）"
  Test passed: "- 测试：通过"
  Review: "- 审查：{verdict}"
  Jira transitioned: "- Jira：已添加评论并转换状态"
  Jira commented only: "- Jira：已添加评论"
  Jira skipped: "- Jira：未找到 issue key，跳过回写"

If user_lang == "en" (default):
  Header: "## {issue_key} Development Complete\n"
  Files: "**Modified {count} file(s):**" then "- `{path}`" for each
  Diff: "**Changes:**" then ```diff block
  Pipeline header: "**Pipeline:**"
  Code gen: "- Code generation: {provider}"
  Patch: "- Patch applied via: {method}"
  Test skipped: "- Tests: skipped (no test config)"
  Test passed: "- Tests: passed"
  Review: "- Review: {verdict}"
  Jira transitioned: "- Jira: commented and transitioned"
  Jira commented only: "- Jira: commented"
  Jira skipped: "- Jira: no issue key found, writeback skipped"

Use pipeline_state.get() for all field access. Check isinstance for lists and dicts.
files_changed is a list, diff is a string, codegen_provider/patch_method/review_verdict are strings.
jira_writeback is a dict with optional "comment" and "transition" sub-dicts that may have "issue_key".

Output ONLY the two function definitions. Start with "import re" then the functions."""

body = {
    "model": "MiniMax-M2.7-highspeed",
    "messages": [
        {"role": "system", "content": "You are a Python code generator. Output ONLY valid Python code. No markdown fences."},
        {"role": "user", "content": prompt},
    ],
    "temperature": 0.1,
    "max_tokens": 4096,
}

resp = httpx.post(
    url,
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json=body,
    timeout=60,
)
data = resp.json()
if "choices" not in data:
    print("API Error:", json.dumps(data, indent=2))
    sys.exit(1)

content = data["choices"][0]["message"]["content"]
# Strip markdown fences if present
import re as _re
content = _re.sub(r"^```(?:python)?\s*", "", content.strip())
content = _re.sub(r"\s*```$", "", content).strip()

print(content)

# Also write to file for review
output_path = os.path.join(os.path.dirname(__file__), "minimax_r4_output.py")
with open(output_path, "w", encoding="utf-8") as f:
    f.write(content + "\n")
print(f"\n--- Written to {output_path} ---")
