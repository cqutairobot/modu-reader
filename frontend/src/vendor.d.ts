declare module 'markdown-it-texmath' {
  import type { MarkdownIt } from 'markdown-it';
  const plugin: (md: MarkdownIt, options: {
    engine: { renderToString(source: string, options: Record<string, unknown>): string };
    delimiters: string | string[];
    katexOptions?: Record<string, unknown>;
  }) => void;
  export default plugin;
}
