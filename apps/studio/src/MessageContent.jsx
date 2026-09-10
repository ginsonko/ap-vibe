import { useMemo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { prepareMessage } from "./message-model.mjs";

const components = {
  a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
  img: ({ alt }) => <span className="message-image-note">🖼 {alt || "消息中的图片"}（请在原任务查看）</span>,
  table: ({ children }) => <div className="markdown-table-scroll"><table>{children}</table></div>,
};

export function MessageContent({ text, annotations = true }) {
  const value = useMemo(() => prepareMessage(text), [text]);
  return <div className="message-content markdown-content"><Markdown remarkPlugins={[remarkGfm]} skipHtml components={components}>{value.body || "（没有可展示的正文）"}</Markdown>{annotations && value.notes.length > 0 && <details className="message-annotations"><summary>查看引用与附注 · {value.notes.length} 项</summary>{value.notes.map((note, i) => <pre key={i}>{note}</pre>)}</details>}</div>;
}
