import { useMemo } from "react";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import css from "highlight.js/lib/languages/css";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import "highlight.js/styles/github.css";

hljs.registerLanguage("bash", bash);
hljs.registerLanguage("css", css);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("python", python);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("xml", xml);

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

interface HighlightedDiffLine extends DiffLine {
  highlightedHtml: string | null;
}

interface HighlightedDiffHunk extends Omit<DiffHunk, "lines"> {
  lines: HighlightedDiffLine[];
}

interface HighlightedDiffFile extends Omit<DiffFile, "hunks"> {
  hunks: HighlightedDiffHunk[];
  language: string | null;
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

export function detectLanguage(path: string): string | null {
  const normalizedPath = path.toLowerCase();

  if (normalizedPath.endsWith(".ts") || normalizedPath.endsWith(".tsx")) {
    return "typescript";
  }

  if (
    normalizedPath.endsWith(".js") ||
    normalizedPath.endsWith(".jsx") ||
    normalizedPath.endsWith(".mjs") ||
    normalizedPath.endsWith(".cjs")
  ) {
    return "javascript";
  }

  if (normalizedPath.endsWith(".py")) {
    return "python";
  }

  if (normalizedPath.endsWith(".json")) {
    return "json";
  }

  if (
    normalizedPath.endsWith(".css") ||
    normalizedPath.endsWith(".scss") ||
    normalizedPath.endsWith(".less")
  ) {
    return "css";
  }

  if (normalizedPath.endsWith(".sh") || normalizedPath.endsWith(".bash")) {
    return "bash";
  }

  if (
    normalizedPath.endsWith(".html") ||
    normalizedPath.endsWith(".xml") ||
    normalizedPath.endsWith(".vue") ||
    normalizedPath.endsWith(".svg")
  ) {
    return "xml";
  }

  return null;
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

function canHighlightLine(kind: DiffLineKind): boolean {
  return kind === "add" || kind === "remove" || kind === "context";
}

function buildHighlightedFiles(files: DiffFile[]): HighlightedDiffFile[] {
  const highlightedHtmlByPair = new Map<string, string | null>();

  return files.map((file) => {
    const language = detectLanguage(file.newPath ?? file.oldPath ?? file.path);

    return {
      ...file,
      language,
      hunks: file.hunks.map((hunk) => ({
        ...hunk,
        lines: hunk.lines.map((line) => {
          let highlightedHtml: string | null = null;

          if (language && canHighlightLine(line.kind)) {
            const cacheKey = `${language}\0${line.content}`;

            if (!highlightedHtmlByPair.has(cacheKey)) {
              try {
                highlightedHtmlByPair.set(
                  cacheKey,
                  hljs.highlight(line.content, { language, ignoreIllegals: true }).value,
                );
              } catch {
                highlightedHtmlByPair.set(cacheKey, null);
              }
            }

            highlightedHtml = highlightedHtmlByPair.get(cacheKey) ?? null;
          }

          return { ...line, highlightedHtml };
        }),
      })),
    };
  });
}

export function DiffViewer({ diff }: DiffViewerProps) {
  const files = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const highlightedFiles = useMemo(() => buildHighlightedFiles(files), [files]);

  if (files.length === 0) {
    return <pre className="diff-viewer diff-viewer-fallback">{diff}</pre>;
  }

  return (
    <div className="diff-viewer">
      {highlightedFiles.map((file, fileIndex) => (
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
                    {line.highlightedHtml ? (
                      <span dangerouslySetInnerHTML={{ __html: line.highlightedHtml }} />
                    ) : (
                      line.content
                    )}
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
