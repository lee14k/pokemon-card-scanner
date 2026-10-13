import { useEffect, useRef, useState } from "react";
import type { PixelIconName } from "@shared/pixelIcons";
import type { Trainer } from "../api";
import PixelIcon from "../ui/PixelIcon";

export type View = "home" | "scan" | "pulls" | "collection" | "dashboard" | "dex" | "battles";

/** Views that need a logged-in trainer. */
export const PROTECTED: ReadonlySet<View> = new Set(["pulls", "collection", "dashboard", "dex", "battles"]);

interface NavItem {
  view: View;
  icon: PixelIconName;
  label: string; // top nav (desktop)
  short: string; // tab bar (phone)
}

export const NAV_ITEMS: NavItem[] = [
  { view: "scan", icon: "camera", label: "Scan", short: "SCAN" },
  { view: "pulls", icon: "pack", label: "My Pulls", short: "PULLS" },
  { view: "dex", icon: "dex", label: "Pokédex", short: "DEX" },
  { view: "collection", icon: "chest", label: "Collection", short: "CARDS" },
  { view: "battles", icon: "swords", label: "Battles", short: "BATTLE" },
];

interface TopbarProps {
  view: View;
  trainer: Trainer | null;
  loading: boolean;
  canViewStats: boolean;
  onNavigate: (v: View) => void;
  onLogin: () => void;
  onLogout: () => void;
}

export function Topbar({ view, trainer, loading, canViewStats, onNavigate, onLogin, onLogout }: TopbarProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setMenuOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [menuOpen]);

  return (
    <header className="topbar">
      <h1 style={{ margin: 0 }}>
        <button type="button" className="logo" onClick={() => onNavigate("home")}>
          <span className="logo-mark">
            <PixelIcon name="pack" size={18} />
          </span>
          <span className="logo-text">
            Pack Scanner
            <small>Gotta scan &rsquo;em all</small>
          </span>
        </button>
      </h1>

      <nav className="topnav" aria-label="Main">
        {NAV_ITEMS.map((item) => (
          <button
            key={item.view}
            type="button"
            aria-current={view === item.view ? "page" : undefined}
            onClick={() => onNavigate(item.view)}
          >
            {item.label}
          </button>
        ))}
        {canViewStats && (
          <button
            type="button"
            aria-current={view === "dashboard" ? "page" : undefined}
            onClick={() => onNavigate("dashboard")}
          >
            Stats
          </button>
        )}
      </nav>

      <div className="topbar-right" ref={menuRef}>
        {!loading &&
          (trainer ? (
            <>
              <button
                type="button"
                className="trainer-chip"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((o) => !o)}
              >
                <span className="avatar">{trainer.handle.slice(0, 1).toUpperCase()}</span>
                <span className="handle">@{trainer.handle}</span>
              </button>
              {menuOpen && (
                <div className="panel menu" role="menu">
                  <p className="menu-email">{trainer.email}</p>
                  {canViewStats && (
                    <button
                      type="button"
                      role="menuitem"
                      className="ghost"
                      onClick={() => {
                        setMenuOpen(false);
                        onNavigate("dashboard");
                      }}
                    >
                      <PixelIcon name="chart" size={16} /> Pull-rate dashboard
                    </button>
                  )}
                  <button
                    type="button"
                    role="menuitem"
                    className="ghost"
                    onClick={() => {
                      setMenuOpen(false);
                      onLogout();
                    }}
                  >
                    <PixelIcon name="close" size={16} /> Log out
                  </button>
                </div>
              )}
            </>
          ) : (
            <button type="button" className="gold small" onClick={onLogin}>
              <PixelIcon name="trainer" size={14} /> Log in
            </button>
          ))}
      </div>
    </header>
  );
}

export function TabBar({ view, onNavigate }: { view: View; onNavigate: (v: View) => void }) {
  return (
    <nav className="tabbar" aria-label="Main">
      {NAV_ITEMS.map((item) => (
        <button
          key={item.view}
          type="button"
          aria-current={view === item.view ? "page" : undefined}
          onClick={() => onNavigate(item.view)}
        >
          <PixelIcon name={item.icon} size={24} />
          {item.short}
        </button>
      ))}
    </nav>
  );
}
