import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Bell,
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  Download,
  FileAudio,
  Folder,
  HardDriveUpload,
  ListChecks,
  LogOut,
  Moon,
  Music,
  RefreshCw,
  Search,
  Settings,
  Shield,
  Sparkles,
  Sun,
  Users,
} from "lucide-react";
import "./styles.css";

const API_BASE = "/api/v1";
const TOKEN_KEY = "nudibranch_api_key";

const navItems = [
  ["Library", Music],
  ["Import", HardDriveUpload],
  ["Wishlist", Sparkles],
  ["Approvals", ListChecks],
  ["Downloads", Download],
  ["Playlists", FileAudio],
  ["Tasks", Database],
  ["Users", Users],
  ["Settings", Settings],
];

const pageDescriptions = {
  Library: "Browse artists, albums, and tracks in the managed library.",
  Import: "Scan new files and prepare them for review.",
  Wishlist: "Request artists, albums, and tracks for download.",
  Approvals: "Review pending changes and apply selected items.",
  Downloads: "Review download searches, candidates, and completed transfers.",
  Playlists: "Create, import, and manage playlists.",
  Tasks: "Track queued, running, completed, and failed work.",
  Users: "Manage users, PINs, API access, and permissions.",
  Settings: "Manage appearance, integrations, and system status.",
};

function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [user, setUser] = useState(null);
  const [page, setPage] = useState("Import");
  const [dark, setDark] = useState(false);
  const [trayOpen, setTrayOpen] = useState(false);
  const [toast, setToast] = useState(null);
  const [accentColor, setAccentColor] = useState("#356df3");
  const [backgroundTint, setBackgroundTint] = useState("#356df3");
  const [library, setLibrary] = useState([]);
  const [importFiles, setImportFiles] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [notifications, setNotifications] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const trayRef = useRef(null);

  const theme = dark ? "app dark" : "app";
  const approvalSelectionCount = useMemo(
    () => approvals.reduce((total, batch) => total + batch.items.filter((item) => item.selected).length, 0),
    [approvals],
  );

  useEffect(() => {
    const handlePointerDown = (event) => {
      if (trayRef.current && !trayRef.current.contains(event.target)) {
        setTrayOpen(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timeout = window.setTimeout(() => setToast(null), 5200);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useEffect(() => {
    if (!token) return;
    refreshAll();
    const interval = window.setInterval(() => {
      refreshTasks();
      refreshNotifications();
    }, 10000);
    return () => window.clearInterval(interval);
  }, [token]);

  async function api(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  async function login(pin) {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pin }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || "Invalid PIN");
      }
      const data = await response.json();
      localStorage.setItem(TOKEN_KEY, data.api_key);
      setToken(data.api_key);
      setUser(data);
      setToast({ title: "Signed in", body: `Welcome, ${data.display_name}.` });
    } catch (loginError) {
      setError(loginError.message);
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setUser(null);
  }

  async function refreshAll() {
    setLoading(true);
    setError("");
    try {
      const [me, libraryTree, approvalData, taskData, notificationData] = await Promise.all([
        api("/me"),
        api("/library/tree"),
        api("/approvals"),
        api("/tasks"),
        api("/notifications"),
      ]);
      setUser(me);
      setLibrary(libraryTree);
      setApprovals(approvalData);
      setTasks(taskData);
      setNotifications(notificationData);
    } catch (refreshError) {
      setError(refreshError.message);
      if (refreshError.message.includes("Invalid API key") || refreshError.message.includes("Missing API key")) {
        logout();
      }
    } finally {
      setLoading(false);
    }
  }

  async function refreshTasks() {
    try {
      setTasks(await api("/tasks"));
    } catch {
      // Task polling should not disrupt the page the user is working in.
    }
  }

  async function refreshNotifications() {
    try {
      setNotifications(await api("/notifications"));
    } catch {
      // Notification polling is best-effort.
    }
  }

  async function scanImportFolder() {
    setLoading(true);
    setError("");
    try {
      const data = await api("/imports/scan", {
        method: "POST",
        body: JSON.stringify({ path: null }),
      });
      setImportFiles(data.files);
      setToast({ title: "Import scan complete", body: `${data.count} audio files found.` });
    } catch (scanError) {
      setError(scanError.message);
    } finally {
      setLoading(false);
    }
  }

  async function proposeImport() {
    setLoading(true);
    setError("");
    try {
      const task = await api("/imports/propose", {
        method: "POST",
        body: JSON.stringify({ path: null }),
      });
      setTasks((current) => [task, ...current]);
      setToast({ title: "Import review queued", body: "A proposal batch will appear in Approvals." });
      setPage("Tasks");
    } catch (proposeError) {
      setError(proposeError.message);
    } finally {
      setLoading(false);
    }
  }

  async function setApprovalSelection(batchId, itemIds, selected) {
    await api(`/approvals/${batchId}/selection`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds, selected }),
    });
    await refreshApprovals();
  }

  async function refreshApprovals() {
    setApprovals(await api("/approvals"));
  }

  async function approveBatch(batchId) {
    setLoading(true);
    try {
      const task = await api(`/approvals/${batchId}/approve`, { method: "POST" });
      setTasks((current) => [task, ...current]);
      setToast({ title: "Approval queued", body: "Selected changes were sent to the task queue." });
      await refreshApprovals();
    } catch (approvalError) {
      setError(approvalError.message);
    } finally {
      setLoading(false);
    }
  }

  async function rejectSelected(batchId, itemIds) {
    setLoading(true);
    try {
      await api(`/approvals/${batchId}/reject`, {
        method: "POST",
        body: JSON.stringify({ item_ids: itemIds, suppress_for: "week" }),
      });
      setToast({ title: "Changes rejected", body: "Selected items were suppressed for one week." });
      await refreshApprovals();
    } catch (rejectError) {
      setError(rejectError.message);
    } finally {
      setLoading(false);
    }
  }

  if (!token) {
    return <LoginScreen loading={loading} error={error} onLogin={login} />;
  }

  return (
    <main className={theme} style={{ "--accent-color": accentColor, "--background-tint": backgroundTint }}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">N</div>
          <div>
            <strong>Nudibranch</strong>
          </div>
        </div>
        <nav>
          {navItems.map(([label, Icon]) => (
            <button className={page === label ? "active" : ""} key={label} onClick={() => setPage(label)}>
              <Icon size={17} />
              {label}
            </button>
          ))}
        </nav>
        <button className="theme-toggle" onClick={() => setDark((value) => !value)} title="Toggle theme">
          {dark ? <Sun size={17} /> : <Moon size={17} />}
          {dark ? "Light" : "Dark"}
        </button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="search">
            <Search size={16} />
            <input placeholder="Search library, proposals, tasks" />
          </div>
          <button className="icon-button" onClick={refreshAll} title="Refresh">
            <RefreshCw size={18} />
          </button>
          <div className="notification-anchor" ref={trayRef}>
            <button className="icon-button" onClick={() => setTrayOpen((value) => !value)} title="Notifications">
              <Bell size={18} />
              {notifications.length > 0 && <span className="badge">{notifications.length}</span>}
            </button>
            {trayOpen && <NotificationTray notifications={notifications} />}
          </div>
          <button className="icon-button" onClick={logout} title="Sign out">
            <LogOut size={18} />
          </button>
        </header>

        <div className="content-grid">
          <section className="panel main-panel">
            <PanelHeader page={page} selectedCount={approvalSelectionCount} />
            {error && <div className="error-banner">{error}</div>}
            {loading && <div className="loading-line">Working...</div>}
            {page === "Library" && <LibraryTree artists={library} />}
            {page === "Approvals" && (
              <Approvals
                approvals={approvals}
                onSelection={setApprovalSelection}
                onApprove={approveBatch}
                onReject={rejectSelected}
              />
            )}
            {page === "Import" && (
              <ImportWizard files={importFiles} onScan={scanImportFolder} onPropose={proposeImport} loading={loading} />
            )}
            {page === "Tasks" && <TasksView tasks={tasks} />}
            {page === "Settings" && (
              <SettingsPanel
                accentColor={accentColor}
                setAccentColor={setAccentColor}
                backgroundTint={backgroundTint}
                setBackgroundTint={setBackgroundTint}
                user={user}
              />
            )}
            {!["Library", "Approvals", "Import", "Tasks", "Settings"].includes(page) && <Placeholder page={page} />}
          </section>

          <Inspector page={page} importFiles={importFiles} approvals={approvals} tasks={tasks} />
        </div>
        {toast && <Toast title={toast.title} body={toast.body} onClose={() => setToast(null)} />}
      </section>
    </main>
  );
}

function LoginScreen({ loading, error, onLogin }) {
  const [pin, setPin] = useState("");

  return (
    <main className="login-page">
      <form
        className="login-panel"
        onSubmit={(event) => {
          event.preventDefault();
          onLogin(pin);
        }}
      >
        <div className="brand login-brand">
          <div className="brand-mark">N</div>
          <strong>Nudibranch</strong>
        </div>
        <label>
          PIN
          <input autoFocus value={pin} onChange={(event) => setPin(event.target.value)} type="password" />
        </label>
        {error && <div className="error-banner">{error}</div>}
        <button className="primary" disabled={loading || pin.length < 4}>
          {loading ? "Signing in" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

function NotificationTray({ notifications }) {
  return (
    <div className="notification-tray">
      <h2>Notifications</h2>
      {notifications.length === 0 ? (
        <p className="empty-state">No notifications yet.</p>
      ) : (
        notifications.map((notification) => (
          <TrayItem
            key={notification.id}
            tone={notification.status === "unread" ? "urgent" : "normal"}
            title={notification.title}
            body={notification.body}
          />
        ))
      )}
    </div>
  );
}

function TrayItem({ title, body, tone = "normal" }) {
  return (
    <button className={`tray-item ${tone}`}>
      <span>{title}</span>
      <small>{body}</small>
    </button>
  );
}

function PanelHeader({ page, selectedCount }) {
  const description = page === "Approvals" ? `${selectedCount} selected changes are ready for review.` : pageDescriptions[page];

  return (
    <div className="panel-header">
      <div>
        <h1>{page}</h1>
        <p>{description ?? "Manage this section of Nudibranch."}</p>
      </div>
    </div>
  );
}

function LibraryTree({ artists }) {
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());

  if (artists.length === 0) {
    return <EmptyState title="No library records" body="Import approved music to populate the managed library." />;
  }

  return (
    <div className="tree">
      {artists.map((artist) => (
        <div key={artist.id}>
          <TreeRow
            icon={Folder}
            open={openArtists.has(artist.id)}
            title={artist.name}
            meta={`${artist.albums.length} albums`}
            onToggle={() => toggleSet(setOpenArtists, artist.id)}
          />
          {openArtists.has(artist.id) &&
            artist.albums.map((album) => (
              <div key={album.id}>
                <TreeRow
                  depth={1}
                  icon={Folder}
                  open={openAlbums.has(album.id)}
                  title={album.title}
                  meta={`${album.tracks.length} tracks`}
                  onToggle={() => toggleSet(setOpenAlbums, album.id)}
                />
                {openAlbums.has(album.id) &&
                  album.tracks.map((track) => (
                    <TreeRow
                      key={track.id}
                      depth={2}
                      icon={FileAudio}
                      title={`${track.track_number ? String(track.track_number).padStart(2, "0") : "#"}-${track.title}`}
                      meta={track.format || "audio"}
                      warning={!track.is_lossless}
                    />
                  ))}
              </div>
            ))}
        </div>
      ))}
    </div>
  );
}

function Approvals({ approvals, onSelection, onApprove, onReject }) {
  if (approvals.length === 0) {
    return <EmptyState title="No pending approvals" body="Import scans and download searches will create review batches here." />;
  }

  return (
    <div className="approval-tree">
      {approvals.map((batch) => (
        <ApprovalBatch key={batch.id} batch={batch} onSelection={onSelection} onApprove={onApprove} onReject={onReject} />
      ))}
    </div>
  );
}

function ApprovalBatch({ batch, onSelection, onApprove, onReject }) {
  const depths = useMemo(() => calculateDepths(batch.items), [batch.items]);
  const selectedItems = batch.items.filter((item) => item.selected);
  const allSelected = selectedItems.length === batch.items.length && batch.items.length > 0;

  return (
    <section className="batch">
      <div className="batch-header">
        <div>
          <h2>{batch.title}</h2>
          <p>
            {batch.status} · {selectedItems.length} of {batch.items.length} selected
          </p>
        </div>
        <div className="approval-actions">
          <button className="secondary" onClick={() => onReject(batch.id, selectedItems.map((item) => item.id))} disabled={selectedItems.length === 0}>
            Reject selected
          </button>
          <button className="primary" onClick={() => onApprove(batch.id)} disabled={selectedItems.length === 0}>
            <Check size={16} />
            Approve selected
          </button>
        </div>
      </div>
      <div className="bulk-row">
        <label>
          <input
            type="checkbox"
            checked={allSelected}
            onChange={(event) => onSelection(batch.id, batch.items.map((item) => item.id), event.target.checked)}
          />
          Select all visible
        </label>
        <span>{selectedItems.length} selected</span>
      </div>
      {batch.items.map((item) => (
        <label className="proposal-row" style={{ "--depth": depths.get(item.id) || 0 }} key={item.id}>
          <input
            type="checkbox"
            checked={item.selected}
            onChange={(event) => onSelection(batch.id, [item.id], event.target.checked)}
          />
          <span className="proposal-title">{item.title}</span>
          <small>{item.kind}</small>
        </label>
      ))}
    </section>
  );
}

function ImportWizard({ files, onScan, onPropose, loading }) {
  return (
    <div className="import-view">
      <div className="action-bar">
        <button className="primary" onClick={onScan} disabled={loading}>
          <RefreshCw size={16} />
          Scan import folder
        </button>
        <button className="secondary" onClick={onPropose} disabled={loading || files.length === 0}>
          Create review batch
        </button>
      </div>
      {files.length === 0 ? (
        <EmptyState title="No scanned files" body="Place audio files in /app/import, then scan the import folder." />
      ) : (
        <div className="file-list">
          {files.map((file) => (
            <div className="file-row" key={file.path}>
              <FileAudio size={17} />
              <div>
                <strong>{file.metadata?.title || file.relative_path}</strong>
                <span>
                  {file.metadata?.artist || "Unknown Artist"} · {file.metadata?.album || "Unknown Album"}
                </span>
              </div>
              <small>{formatBytes(file.size_bytes)}</small>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TasksView({ tasks }) {
  if (tasks.length === 0) {
    return <EmptyState title="No tasks" body="Scans, approvals, downloads, and notifications will appear here." />;
  }

  return (
    <div className="task-list">
      {tasks.map((task) => (
        <div className="task-row" key={task.id}>
          <strong>{task.type}</strong>
          <span>{task.status}</span>
          <small>{task.error || new Date(task.created_at).toLocaleString()}</small>
        </div>
      ))}
    </div>
  );
}

function Placeholder({ page }) {
  return (
    <div className="placeholder">
      <Shield size={28} />
      <h2>{page}</h2>
      <p>{pageDescriptions[page] ?? "Manage this section of Nudibranch."}</p>
    </div>
  );
}

function SettingsPanel({ accentColor, setAccentColor, backgroundTint, setBackgroundTint, user }) {
  return (
    <div className="settings-grid">
      <section className="settings-section">
        <h2>Appearance</h2>
        <label className="setting-row">
          <span>
            Accent color
            <small>Interactive highlights and hover states.</small>
          </span>
          <input type="color" value={accentColor} onChange={(event) => setAccentColor(event.target.value)} />
        </label>
        <label className="setting-row">
          <span>
            Background tint
            <small>Mixed into the grey interface in light and dark mode.</small>
          </span>
          <input type="color" value={backgroundTint} onChange={(event) => setBackgroundTint(event.target.value)} />
        </label>
      </section>
      <section className="settings-section">
        <h2>Status</h2>
        <div className="status-list">
          <span>User</span>
          <strong>{user?.display_name || "Signed in"}</strong>
          <span>Role</span>
          <strong>{user?.is_admin ? "Admin" : "User"}</strong>
          <span>API</span>
          <strong>Connected</strong>
        </div>
      </section>
    </div>
  );
}

function Inspector({ page, importFiles, approvals, tasks }) {
  const selectedApprovalCount = approvals.reduce((total, batch) => total + batch.items.filter((item) => item.selected).length, 0);

  return (
    <aside className="panel inspector">
      <h2>Inspector</h2>
      <div className="metadata-grid">
        <label>Page</label>
        <strong>{page}</strong>
        <label>Imports</label>
        <strong>{importFiles.length}</strong>
        <label>Approvals</label>
        <strong>{selectedApprovalCount} selected</strong>
        <label>Tasks</label>
        <strong>{tasks.length}</strong>
      </div>
    </aside>
  );
}

function Toast({ title, body, onClose }) {
  return (
    <button className="toast" onClick={onClose}>
      <strong>{title}</strong>
      <span>{body}</span>
    </button>
  );
}

function TreeRow({ depth = 0, icon: Icon, open, title, meta, warning = false, onToggle }) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <button className="tree-row" style={{ "--depth": depth }} onClick={onToggle}>
      <span className="chevron">{onToggle ? <Chevron size={15} /> : null}</span>
      <Icon size={17} />
      <span className="tree-title">{title}</span>
      <small className={warning ? "warning" : ""}>{meta}</small>
    </button>
  );
}

function EmptyState({ title, body }) {
  return (
    <div className="empty-panel">
      <h2>{title}</h2>
      <p>{body}</p>
    </div>
  );
}

function calculateDepths(items) {
  const byId = new Map(items.map((item) => [item.id, item]));
  const depths = new Map();
  const depthOf = (item) => {
    if (!item.parent_id) return 0;
    if (depths.has(item.id)) return depths.get(item.id);
    const parent = byId.get(item.parent_id);
    const depth = parent ? depthOf(parent) + 1 : 0;
    depths.set(item.id, depth);
    return depth;
  };
  items.forEach((item) => depths.set(item.id, depthOf(item)));
  return depths;
}

function toggleSet(setter, value) {
  setter((current) => {
    const next = new Set(current);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  });
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

createRoot(document.getElementById("root")).render(<App />);
