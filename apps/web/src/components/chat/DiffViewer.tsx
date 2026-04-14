interface DiffViewerProps {
  diff: string;
}

type DiffLineKind = "add" | "remove" | "context" | "meta";

interface DiffLine {
  kind: DiffLineKind;
  oldLine: number | null;
  newLine: number | null;
  content: string;
}

interface DiffHunk {
  header: string;
  oldStart: number;
  newStart: number;
  lines: DiffLine[];
}

interface DiffFile {
  path: string;
  oldPath: string | null;
  newPath: string | null;
  hunks: DiffHunk[];
}

const HUNK_HEADER_PATTERN = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

function cleanDiffPath(path: string): string {
  const [rawPath] = path.trim().split(/\s+/);
  if (!rawPath || rawPath === "/dev/null") {
    return rawPath || "";
  }
  return rawPath.replace(/^[ab]\//, "");
}

function readGitDiffPath(line: string): string {
  const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
  return match?.[2] ?? line.replace(/^diff --git\s+/, "").trim();
}

function ensureFile(files: DiffFile[]): DiffFile {
  const lastFile = files[files.length - 1];
  if (lastFile) {
    return lastFile;
  }

  const file = { path: "Changes", oldPath: null, newPath: null, hunks: [] };
  files.push(file);
  return file;
}

function parseUnifiedDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = [];
  const lines = diff.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  if (lines[lines.length - 1] === "") {
    lines.pop();
  }
  let currentFile: DiffFile | null = null;
  let currentHunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      currentFile = {
        path: cleanDiffPath(readGitDiffPath(line)),
        oldPath: null,
        newPath: null,
        hunks: [],
      };
      files.push(currentFile);
      currentHunk = null;
      continue;
    }

    if (line.startsWith("--- ")) {
      currentFile = ensureFile(files);
      currentFile.oldPath = cleanDiffPath(line.slice(4));
      currentFile.path = currentFile.oldPath || currentFile.path;
      continue;
    }

    if (line.startsWith("+++ ")) {
      currentFile = ensureFile(files);
      currentFile.newPath = cleanDiffPath(line.slice(4));
      currentFile.path = currentFile.newPath || currentFile.oldPath || currentFile.path;
      continue;
    }

    if (line.startsWith("@@")) {
      currentFile = ensureFile(files);
      const match = HUNK_HEADER_PATTERN.exec(line);
      oldLine = match ? Number(match[1]) : 0;
      newLine = match ? Number(match[2]) : 0;
      currentHunk = {
        header: line,
        oldStart: oldLine,
        newStart: newLine,
        lines: [],
      };
      currentFile.hunks.push(currentHunk);
      continue;
    }

    if (!currentHunk) {
      continue;
    }

    if (line.startsWith("+")) {
      currentHunk.lines.push({
        kind: "add",
        oldLine: null,
        newLine,
        content: line.slice(1),
      });
      newLine += 1;
      continue;
    }

    if (line.startsWith("-")) {
      currentHunk.lines.push({
        kind: "remove",
        oldLine,
        newLine: null,
        content: line.slice(1),
      });
      oldLine += 1;
      continue;
    }

    if (line.startsWith("\\")) {
      currentHunk.lines.push({
        kind: "meta",
        oldLine: null,
        newLine: null,
        content: line,
      });
      continue;
    }

    currentHunk.lines.push({
      kind: "context",
      oldLine,
      newLine,
      content: line.startsWith(" ") ? line.slice(1) : line,
    });
    oldLine += 1;
    newLine += 1;
  }

  return files.filter((file) => file.hunks.length > 0);
}

export function countDiffFiles(diff: string): number {
  return parseUnifiedDiff(diff).length;
}

export function DiffViewer({ diff }: DiffViewerProps) {
  const files = parseUnifiedDiff(diff);

  if (files.length === 0) {
    return <pre className="diff-viewer diff-viewer-fallback">{diff}</pre>;
  }

  return (
    <div className="diff-viewer">
      {files.map((file, fileIndex) => (
        <section className="diff-file" key={`${file.path}-${fileIndex}`}>
          <div className="diff-file-header">{file.path}</div>
          {file.hunks.map((hunk, hunkIndex) => (
            <div className="diff-hunk" key={`${hunk.header}-${hunkIndex}`}>
              <div className="diff-hunk-header">{hunk.header}</div>
              {hunk.lines.map((line, lineIndex) => (
                <div className={`diff-line diff-line-${line.kind}`} key={`${hunkIndex}-${lineIndex}`}>
                  <span className="diff-line-number">{line.oldLine ?? ""}</span>
                  <span className="diff-line-number">{line.newLine ?? ""}</span>
                  <span className="diff-line-content">
                    {line.kind === "add" ? "+" : line.kind === "remove" ? "-" : line.kind === "context" ? " " : ""}
                    {line.content}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
