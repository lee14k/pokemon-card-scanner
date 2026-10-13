// Small presentational pieces of the "Pixel Pop" theme. Styles live in App.css.
import { useEffect, useRef, type ReactNode } from "react";
import type { PixelIconName } from "@shared/pixelIcons";
import PixelIcon from "./PixelIcon";

export function ScreenTitle({
  kicker,
  title,
  sub,
  children,
}: {
  kicker?: string;
  title: string;
  sub?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="screen-title">
      {kicker && <span className="kicker">{kicker}</span>}
      <h2>{title}</h2>
      {sub && <p className="sub">{sub}</p>}
      {children}
    </header>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h3 className="section-title">{children}</h3>;
}

export function Loader({ label, small }: { label?: string; small?: boolean }) {
  return (
    <span className={`pixel-loader${small ? " small" : ""}`} role={label ? "status" : undefined}>
      <i />
      <i />
      <i />
      {label && <span className="sr-only">{label}</span>}
    </span>
  );
}

export function LoadingScreen({ label }: { label: string }) {
  return (
    <div className="status">
      <Loader />
      <p className="pixel small-text">{label}</p>
    </div>
  );
}

export function HpBar({
  value,
  max,
  label = "HP",
  tone,
  showValue = true,
}: {
  value: number;
  max: number;
  label?: string;
  tone?: "auto" | "cyan" | "green";
  showValue?: boolean;
}) {
  const pct = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  const auto = pct > 0.5 ? "" : pct > 0.2 ? " mid" : " low";
  const cls = tone === "cyan" ? " cyan" : tone === "green" ? "" : tone === "auto" ? auto : "";
  return (
    <div className="hpbar" role="progressbar" aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
      {label && <span className="hpbar-label">{label}</span>}
      <div className="hpbar-track">
        <div className={`hpbar-fill${cls}`} style={{ width: `${pct * 100}%` }} />
      </div>
      {showValue && (
        <span className="hpbar-value">
          {value}/{max}
        </span>
      )}
    </div>
  );
}

export function StatTile({
  value,
  label,
  tone,
}: {
  value: ReactNode;
  label: string;
  tone?: "green" | "red" | "cyan";
}) {
  return (
    <div className="panel stat-tile">
      <span className={`stat-value${tone ? ` ${tone}` : ""}`}>{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  children,
}: {
  icon: PixelIconName;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="panel empty-state">
      <PixelIcon name={icon} size={48} />
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="panel flagged stack" role="alert">
      <div className="row">
        <PixelIcon name="warn" size={24} className="danger-text" />
        <p className="error-box grow">{message}</p>
      </div>
      {onRetry && (
        <div className="row row-end">
          <button type="button" className="small" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Callers pass inline closures; read the latest through a ref so a parent
  // re-render (e.g. a background poll) neither re-binds the key handler nor
  // yanks focus back to the first field mid-typing.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey, true);
    // Focus the first field (or the dialog itself) so keyboard users land inside.
    const first = ref.current?.querySelector<HTMLElement>("input, select, textarea, button:not(.modal-close)");
    (first ?? ref.current)?.focus();
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="panel modal" role="dialog" aria-modal="true" aria-label={title} ref={ref} tabIndex={-1}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button type="button" className="ghost small icon-only modal-close" aria-label="Close" onClick={onClose}>
            <PixelIcon name="close" size={14} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

/** RPG-style text box toast. */
export function Toast({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="textbox toast" role="status">
      {message}
    </div>
  );
}

export function CardArt({
  src,
  alt,
  className = "art",
  fallback,
}: {
  src: string | null | undefined;
  alt: string;
  className?: string;
  fallback?: ReactNode;
}) {
  return src ? (
    <img src={src} alt={alt} className={className} loading="lazy" />
  ) : (
    <div className={`${className} placeholder`} aria-label={alt}>
      {fallback ?? "?"}
    </div>
  );
}

const CONFETTI_COLORS = ["var(--gold)", "var(--cyan)", "var(--primary)", "var(--green)", "#a78bfa"];

export function Confetti({ count = 18 }: { count?: number }) {
  return (
    <div className="confetti" aria-hidden="true">
      {Array.from({ length: count }).map((_, i) => (
        <i
          key={i}
          style={
            {
              left: `${(i * 97) % 100}%`,
              "--c": CONFETTI_COLORS[i % CONFETTI_COLORS.length],
              "--d": `${1.8 + ((i * 37) % 10) / 10}s`,
              "--delay": `${((i * 53) % 12) / 10}s`,
            } as React.CSSProperties
          }
        />
      ))}
    </div>
  );
}
