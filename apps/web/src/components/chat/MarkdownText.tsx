import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Props {
  text: string;
}

/**
 * Renders an assistant message as markdown.
 * Used for streaming chat answers so **bold** / numbered lists / code
 * blocks / tables / horizontal rules display as proper formatting instead
 * of literal `##`, `**`, `---` characters.
 *
 * The text grows char-by-char (driven by ChatPage.submitMessage's pending
 * buffer). react-markdown re-parses on each update; for short chat replies
 * (~hundreds of chars) this is cheap.
 */
export function MarkdownText({ text }: Props) {
  return (
    <div className="md-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Open links in a new tab for safety + better UX inside the chat.
          a: ({ children, href, ...rest }) => (
            <a href={href ?? undefined} target="_blank" rel="noreferrer noopener" {...rest}>
              {children}
            </a>
          ),
          // Limit heading levels: ## → h4 inside a chat bubble (no giant text).
          h1: ({ children }) => <h4 className="md-h">{children}</h4>,
          h2: ({ children }) => <h4 className="md-h">{children}</h4>,
          h3: ({ children }) => <h4 className="md-h">{children}</h4>,
          h4: ({ children }) => <h4 className="md-h">{children}</h4>,
          h5: ({ children }) => <h5 className="md-h md-h-sm">{children}</h5>,
          h6: ({ children }) => <h6 className="md-h md-h-sm">{children}</h6>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
