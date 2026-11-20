/** Resolve display references only against files already recorded for this session.
 * Rendering a mention never changes a file's delivery classification.
 */
export interface FilePreviewReference {
  path: string;
  name?: string;
}

export interface ResolvedFilePreviewReference {
  path: string;
  name: string;
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
}

function localPath(value: string): string | null {
  let path = value.trim();
  if (path.startsWith("sandbox:/")) path = path.slice("sandbox:".length);
  if (!path || /[\u0000-\u001f]/.test(path)) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(path) && !/^[a-z]:[\\/]/i.test(path)) return null;
  if (path.startsWith("//")) return null;
  path = path.replaceAll("\\", "/");
  if (path.split("/").includes("..")) return null;
  const absolute = path.startsWith("/");
  const normalized = path.split("/").filter((part) => part && part !== ".").join("/");
  return normalized ? `${absolute ? "/" : ""}${normalized}` : null;
}

function absolutePath(path: string): boolean {
  return path.startsWith("/") || /^[a-z]:\//i.test(path);
}

function basename(path: string): string {
  return path.split("/").pop() || path;
}

/** Local links with no known session file remain inert rather than navigating
 * the browser to a server filesystem path. Web links and page anchors retain
 * ordinary Markdown behavior.
 */
export function isLocalFileReference(value: string | undefined, workspace: string | null = null): boolean {
  if (!value || value.startsWith("#") || value.startsWith("//")) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
    return /^(sandbox:|file:|[a-z]:[\\/])/i.test(value);
  }
  const path = decodedPath(value);
  if (!path) return false;
  const root = workspace ? localPath(workspace) : null;
  return Boolean(
    (root && (path === root || path.startsWith(`${root}/`))) ||
    /^\/(?:tenant|home|srv|var|tmp|Users|opt|etc|mnt|root|workspace|workspaces)(?:\/|$)/.test(path) ||
    /\.[a-z0-9]{1,10}(?:[?#].*)?$/i.test(path),
  );
}

function decodedPath(value: string): string | null {
  try {
    return localPath(decodeURIComponent(value));
  } catch {
    return null;
  }
}

export function createFilePreviewReferenceResolver(
  files: readonly FilePreviewReference[],
  workspace: string | null = null,
) {
  const root = workspace ? localPath(workspace) : null;
  const entries = new Map<string, ResolvedFilePreviewReference>();
  for (const file of files) {
    const path = localPath(file.path);
    if (!path) continue;
    const canonical = !absolutePath(path) && root ? `${root}/${path}` : path;
    // Keep the persisted path for the authenticated content API; aliases are
    // display-only, never guesses based on a filename suffix.
    if (!entries.has(canonical)) entries.set(canonical, { path: file.path, name: basename(path) });
  }

  const aliases = new Map<string, ResolvedFilePreviewReference | null>();
  const addAlias = (alias: string, entry: ResolvedFilePreviewReference) => {
    const previous = aliases.get(alias);
    if (previous && previous.path !== entry.path) aliases.set(alias, null);
    else if (!aliases.has(alias)) aliases.set(alias, entry);
  };
  for (const [canonical, entry] of entries) {
    addAlias(canonical, entry);
    const path = localPath(entry.path)!;
    addAlias(path, entry);
    if (root && canonical.startsWith(`${root}/`)) addAlias(canonical.slice(root.length + 1), entry);
    addAlias(entry.name, entry);
  }

  const resolve = (reference: string): ResolvedFilePreviewReference | null => {
    if (reference.startsWith("#") || reference.startsWith("//")) return null;
    // A literal percent sign is a valid filename character. Prefer exact
    // spelling before decoding Markdown's URL-encoded spaces and Unicode.
    const path = localPath(reference) ?? decodedPath(reference);
    if (path && aliases.get(path)) return aliases.get(path)!;
    const decoded = decodedPath(reference);
    return decoded ? aliases.get(decoded) ?? null : null;
  };

  const mentions = [...aliases]
    .filter((entry): entry is [string, ResolvedFilePreviewReference] => Boolean(entry[1]))
    .flatMap(([alias, entry]) => [
      { text: alias, file: entry },
      ...(absolutePath(alias) ? [{ text: `sandbox:${alias}`, file: entry }] : []),
    ])
    .sort((a, b) => b.text.length - a.text.length);
  const mentionsByFirstCharacter = new Map<string, typeof mentions>();
  for (const mention of mentions) {
    const first = mention.text[0];
    const bucket = mentionsByFirstCharacter.get(first) || [];
    bucket.push(mention);
    mentionsByFirstCharacter.set(first, bucket);
  }

  /** Transform only prose text leaves. Fenced/inline code, URLs, citations and
   * image labels must retain their original semantics.
   */
  const remarkFilePreviews = () => (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (["code", "inlineCode", "link", "linkReference", "image", "imageReference", "html"].includes(node.type)) return;
      if (!node.children) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text" || !child.value) {
          visit(child);
          return [child];
        }
        const value = child.value;
        const nodes: MarkdownNode[] = [];
        let start = 0;
        let index = 0;
        while (index < value.length) {
          const mention = mentionsByFirstCharacter.get(value[index])?.find((candidate) => {
            if (!value.startsWith(candidate.text, index)) return false;
            const before = value[index - 1] || "";
            const remainder = value.slice(index + candidate.text.length);
            const after = remainder[0] || "";
            const sentencePeriod = after === "." && !/[\p{L}\p{N}_/\\.%-]/u.test(remainder[1] || "");
            // Do not match within URLs, longer paths or a similarly named file.
            return !/[\p{L}\p{N}_/\\.@:%=+-]/u.test(before) &&
              (sentencePeriod || !/[\p{L}\p{N}_/\\.%+-]/u.test(after));
          });
          if (!mention) { index += 1; continue; }
          if (index > start) nodes.push({ type: "text", value: value.slice(start, index) });
          nodes.push({ type: "link", url: mention.file.path, children: [{ type: "text", value: mention.file.name }] });
          index += mention.text.length;
          start = index;
        }
        if (!nodes.length) return [child];
        if (start < value.length) nodes.push({ type: "text", value: value.slice(start) });
        return nodes;
      });
    };
    visit(tree);
  };

  return { resolve, remarkFilePreviews };
}

export type FilePreviewReferenceResolver = ReturnType<typeof createFilePreviewReferenceResolver>;
