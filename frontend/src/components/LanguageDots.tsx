import { languageColor } from "../status";

export function LanguageDots({
  languages,
  max = 3,
}: {
  languages: string[];
  max?: number;
}) {
  const shown = languages.slice(0, max);
  const extra = languages.length - shown.length;
  if (shown.length === 0) return <span className="text-fg-subtle text-[12px]">—</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      {shown.map((lang) => (
        <span
          key={lang}
          title={lang}
          className="inline-flex items-center gap-1"
        >
          <span
            className="w-[10px] h-[10px] rounded-full shrink-0"
            style={{ backgroundColor: languageColor(lang) }}
          />
          <span className="text-[12px] text-fg-muted hidden lg:inline">
            {lang}
          </span>
        </span>
      ))}
      {extra > 0 && (
        <span className="text-[12px] text-fg-subtle">+{extra}</span>
      )}
    </span>
  );
}
