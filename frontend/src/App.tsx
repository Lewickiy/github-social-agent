import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { BarChart3, BrainCircuit, Settings, Users } from "lucide-react";
import { RefreshProvider } from "./refresh";
import OverviewPage from "./pages/OverviewPage";
import UsersPage from "./pages/UsersPage";
import MLPage from "./pages/MLPage";
import ManagementPage from "./pages/ManagementPage";

const navItems = [
  { to: "/", label: "Overview", icon: BarChart3, end: true },
  { to: "/users", label: "Users", icon: Users, end: false },
  { to: "/ml", label: "ML", icon: BrainCircuit, end: false },
  { to: "/manage", label: "Management", icon: Settings, end: false },
];

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      {/* GitHub-style top header */}
      <header className="bg-header text-header-fg h-[60px] flex items-center px-4 md:px-6 shrink-0">
        <div className="max-w-[1280px] w-full mx-auto flex items-center gap-6">
          <div className="flex items-center gap-2">
            <svg viewBox="0 0 16 16" width="24" height="24" fill="currentColor" aria-hidden="true">
              <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
            </svg>
            <span className="font-semibold text-[16px] tracking-tight">
              GitHub Social Dashboard
            </span>
          </div>

          <nav className="hidden sm:flex items-center gap-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium transition-colors duration-100 ${
                    isActive
                      ? "bg-white/15 text-white"
                      : "text-header-fg/80 hover:text-white hover:bg-white/10"
                  }`
                }
              >
                <item.icon size={14} />
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto hidden md:flex items-center gap-3">
            <span className="text-[12px] text-header-fg/70">
              Growing GitHub reach, one follow at a time
            </span>
          </div>
        </div>
      </header>

      <main className="flex-1 bg-canvas-subtle">
        <RefreshProvider>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/ml" element={<MLPage />} />
            <Route path="/manage" element={<ManagementPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </RefreshProvider>
      </main>
    </div>
  );
}
