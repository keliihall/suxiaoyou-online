"use client";

import { useMemo, useCallback, useState, memo, createContext, useContext } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { useTranslation } from "react-i18next";
import { Copy, Check, PanelRight, FileText } from "lucide-react";
import Image from "next/image";
import { cn } from "@/lib/utils";
import { useArtifactStore } from "@/stores/artifact-store";
import { useWorkspaceStore } from "@/stores/workspace-store";
import {
  classifyCodeBlock,
  artifactTypeFromExtension,
  languageFromExtension,
} from "@/lib/artifacts";
import {
  createFilePreviewReferenceResolver,
  isLocalFileReference,
  type FilePreviewReference,
  type FilePreviewReferenceResolver,
  type ResolvedFilePreviewReference,
} from "@/lib/file-preview-references";
import type { TextPart as TextPartType } from "@/types/message";
import type { Source } from "@/lib/sources";
import { MermaidBlock } from "./mermaid-block";

interface TextPartProps {
  data: TextPartType;
  isStreaming?: boolean;
  sources?: Source[];
  /** Paths recorded by completed tools in this conversation turn. */
  fileReferences?: FilePreviewReference[];
}

const NO_FILE_REFERENCES: FilePreviewReference[] = [];
const FilePreviewAllowedContext = createContext(true);

// Extract plain text from React children (handles nested elements)
function extractText(node: React.ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (!node) return "";
  if (Array.isArray(node)) return node.map(extractText).join("");
  if (typeof node === "object" && "props" in node) {
    const el = node as { props: { children?: React.ReactNode } };
    return extractText(el.props.children);
  }
  return "";
}

// Detect key-value pattern: colon/fullwidth-colon within first 30 chars
function isKeyValue(text: string): boolean {
  const head = text.slice(0, 30);
  return /[:：]/.test(head) && text.length < 80;
}

function ProseParagraph({ children, ...props }: React.HTMLAttributes<HTMLParagraphElement> & { children?: React.ReactNode }) {
  const text = extractText(children);
  const len = text.length;
  const compact = len > 0 && len <= 60;
  const kv = compact && isKeyValue(text);

  const cls = cn(
    kv ? "prose-kv" : compact ? "prose-compact" : undefined,
  );

  return (
    <p className={cls || undefined} {...props}>
      {children}
    </p>
  );
}

function FilePreviewLink({ file }: { file: ResolvedFilePreviewReference }) {
  const { t } = useTranslation("chat");
  const openArtifact = useArtifactStore((s) => s.openArtifact);
  return (
    <button
      type="button"
      data-testid="file-preview-link"
      aria-label={`${t("previewFile")} ${file.name}`}
      onClick={() => openArtifact({
        id: `file-preview-${file.path}`,
        title: file.name,
        type: artifactTypeFromExtension(file.path) || "file-preview",
        content: "",
        language: languageFromExtension(file.path),
        filePath: file.path,
      })}
      className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-[var(--border-subtle)] bg-[var(--surface-secondary)] px-2 py-1 align-middle text-left text-[0.9em] font-medium text-[var(--text-primary)] no-underline transition-colors hover:bg-[var(--surface-tertiary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-primary)]"
    >
      <FileText className="h-3.5 w-3.5 shrink-0 text-[var(--text-tertiary)]" aria-hidden="true" />
      <span className="truncate">{file.name}</span>
    </button>
  );
}

function CodeBlock({ className, children, fileResolver, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode; fileResolver: FilePreviewReferenceResolver }) {
  const filePreviewAllowed = useContext(FilePreviewAllowedContext);
  const { t } = useTranslation("chat");
  const [copied, setCopied] = useState(false);
  const match = /language-(\w+)/.exec(className || "");
  const lang = match?.[1] ?? "";
  const rawCode = extractText(children);
  const code = rawCode.replace(/\n$/, "");
  const openArtifact = useArtifactStore((s) => s.openArtifact);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [code]);

  // Classify code block as potential artifact
  const artifactType = classifyCodeBlock(lang, code);

  const handleOpenInPanel = useCallback(() => {
    if (!artifactType) return;
    openArtifact({
      id: `code-${code.length}-${code.slice(0, 32)}`,
      title: lang ? `${lang.charAt(0).toUpperCase() + lang.slice(1)} snippet` : "Code",
      type: artifactType,
      content: code,
      language: lang || undefined,
    });
  }, [artifactType, code, lang, openArtifact]);

  if (!match && !rawCode.endsWith("\n")) {
    const file = filePreviewAllowed ? fileResolver.resolve(rawCode.trim()) : null;
    if (file) return <FilePreviewLink file={file} />;

    // Regular inline code
    return (
      <code
        className="rounded-md bg-[var(--surface-tertiary)] px-1.5 py-0.5 text-[0.85em] font-mono border border-[var(--border-default)]"
        {...props}
      >
        {children}
      </code>
    );
  }

  // Detect mermaid code blocks
  if (lang === "mermaid") {
    return <MermaidBlock code={code} />;
  }

  // Code block
  const langDisplay = lang
    ? lang.charAt(0).toUpperCase() + lang.slice(1).toLowerCase()
    : "Code";

  return (
    <div className="group relative rounded-2xl overflow-hidden my-6 bg-[var(--code-block-bg)] border border-[var(--code-block-border)]">
      <div className="flex items-center justify-between px-4 py-2">
        <div className="flex items-center gap-2">
          <svg className="w-3.5 h-3.5 text-[var(--code-block-text)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          <span className="text-xs font-sans text-[var(--code-block-text)] select-none">{langDisplay}</span>
        </div>
        <div className="flex items-center gap-1 opacity-100 transition-opacity lg:opacity-0 lg:group-hover:opacity-100 lg:group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">
          {artifactType && (
            <button
              onClick={handleOpenInPanel}
              className="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-black/10 dark:hover:bg-white/10 text-[var(--code-block-text)] hover:text-[var(--code-block-text-hover)] transition-colors lg:h-9 lg:w-9 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11"
              aria-label={t("previewFile")}
              title={t("previewFile")}
            >
              <PanelRight className="h-4 w-4" />
            </button>
          )}
          <button
            onClick={handleCopy}
            className="flex h-11 w-11 items-center justify-center rounded-lg hover:bg-black/10 dark:hover:bg-white/10 text-[var(--code-block-text)] hover:text-[var(--code-block-text-hover)] transition-colors lg:h-9 lg:w-9 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11"
            aria-label={copied ? t("copied") : t("copy")}
            title={copied ? t("copied") : t("copy")}
          >
            {copied ? (
              <Check className="h-4 w-4 text-[var(--code-block-success)]" />
            ) : (
              <Copy className="h-4 w-4" />
            )}
          </button>
        </div>
      </div>
      <div className="px-4 pb-4">
        <pre className="overflow-x-auto text-[0.8125rem] leading-relaxed">
          <code className={`font-mono ${className}`} {...props}>
            {children}
          </code>
        </pre>
      </div>
    </div>
  );
}

export const TextPart = memo(function TextPart({ data, isStreaming, sources = [], fileReferences = NO_FILE_REFERENCES }: TextPartProps) {
  const workspaceFiles = useWorkspaceStore((s) => s.workspaceFiles);
  const workspace = useWorkspaceStore((s) => s.activeWorkspacePath);
  const fileResolver = useMemo(
    () => createFilePreviewReferenceResolver([...fileReferences, ...workspaceFiles], workspace),
    [fileReferences, workspaceFiles, workspace],
  );
  // Build a URL→Source lookup for inline citation matching
  const sourceMap = useMemo(() => {
    const map = new Map<string, Source>();
    for (const s of sources) {
      map.set(s.url, s);
    }
    return map;
  }, [sources]);

  const components = useMemo(
    () => ({
      code: (props: React.HTMLAttributes<HTMLElement>) => <CodeBlock {...props} fileResolver={fileResolver} />,
      p: ProseParagraph,
      // Wrap tables in a rounded, scrollable container
      table: ({ children }: { children?: React.ReactNode }) => (
        <div className="prose-table-wrapper my-6 rounded-lg border border-[var(--border-default)] overflow-x-auto">
          <table>{children}</table>
        </div>
      ),
      img: ({ src, alt, ...props }: React.ImgHTMLAttributes<HTMLImageElement>) => {
        const file = typeof src === "string" ? fileResolver.resolve(src) : null;
        if (file) return <FilePreviewLink file={file} />;
        if (!src || (typeof src === "string" && isLocalFileReference(src, workspace))) return <span>{alt}</span>;
        // Keep ordinary remote Markdown images at their natural aspect ratio.
        // eslint-disable-next-line @next/next/no-img-element
        return <img src={src} alt={alt || ""} {...props} />;
      },
      // Render matched links as citation badges, otherwise open in new tab
      a: ({ children, href, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { children?: React.ReactNode }) => {
        const file = href ? fileResolver.resolve(href) : null;
        if (file) return <FilePreviewLink file={file} />;
        if (isLocalFileReference(href, workspace) || !href) return <span><FilePreviewAllowedContext.Provider value={false}>{children}</FilePreviewAllowedContext.Provider></span>;
        const source = href ? sourceMap.get(href) : undefined;
        if (source) {
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="no-underline"
              {...props}
            >
              <span className="inline-flex items-center gap-1 rounded-full bg-[var(--surface-tertiary)] px-2 py-0.5 text-[11px] text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)] transition-colors cursor-pointer align-middle mx-0.5">
                {source.favicon && (
                  <Image
                    src={source.favicon}
                    alt=""
                    width={14}
                    height={14}
                    unoptimized
                    className="h-3.5 w-3.5 rounded-sm"
                    loading="lazy"
                  />
                )}
                <span className="truncate max-w-[150px]">{source.domain}</span>
              </span>
            </a>
          );
        }
        return (
          <a target="_blank" rel="noopener noreferrer" href={href} {...props}>
            <FilePreviewAllowedContext.Provider value={false}>{children}</FilePreviewAllowedContext.Provider>
          </a>
        );
      },
    }),
    [fileResolver, sourceMap, workspace],
  );

  // Skip expensive syntax highlighting during streaming — code blocks are
  // still growing so highlighting is wasted work. Full highlighting runs
  // once streaming finishes (isStreaming becomes false).
  const rehypePlugins = useMemo(
    () => (isStreaming ? [] : [rehypeHighlight]),
    [isStreaming],
  );

  if (!data.text) return null;

  return (
    <div className={cn(
      "prose max-w-none",
      isStreaming && "streaming-cursor",
    )}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, fileResolver.remarkFilePreviews]}
        urlTransform={(url) => fileResolver.resolve(url) ? url : defaultUrlTransform(url)}
        rehypePlugins={rehypePlugins}
        components={components}
      >
        {data.text}
      </ReactMarkdown>
    </div>
  );
});
