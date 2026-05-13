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
const APPEARANCE_KEY = "nudibranch_appearance";

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

const approvalTypeLabels = {
  import_files: "Imports",
  download: "Downloads",
  metadata: "Metadata",
  artwork: "Artwork",
  lyrics: "Lyrics",
  file_move: "File moves",
  delete: "Deletes",
  jellyfin_sync: "Jellyfin sync",
  playlist: "Playlists",
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
  const [appearanceReady, setAppearanceReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const trayRef = useRef(null);

  const theme = dark ? "app dark" : "app";
  const approvalSelectionCount = useMemo(
    () => approvals.reduce((total, batch) => total + batch.items.filter((item) => item.selected).length, 0),
    [approvals],
  );
  const activeImportTask = tasks.some((task) => task.type === "propose_import" && ["queued", "running"].includes(task.status));

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
      refreshLibrary();
      refreshTasks();
      refreshApprovals();
      refreshNotifications();
    }, 10000);
    return () => window.clearInterval(interval);
  }, [token]);

  useEffect(() => {
    if (!user?.id) return;
    setAppearanceReady(false);
    const saved = localStorage.getItem(`${APPEARANCE_KEY}_${user.id}`);
    if (!saved) {
      setAppearanceReady(true);
      return;
    }
    try {
      const appearance = JSON.parse(saved);
      setDark(Boolean(appearance.dark));
      setAccentColor(appearance.accentColor || "#356df3");
      setBackgroundTint(appearance.backgroundTint || "#356df3");
    } catch {
      localStorage.removeItem(`${APPEARANCE_KEY}_${user.id}`);
    }
    setAppearanceReady(true);
  }, [user?.id]);

  useEffect(() => {
    if (!user?.id) return;
    if (!appearanceReady) return;
    localStorage.setItem(
      `${APPEARANCE_KEY}_${user.id}`,
      JSON.stringify({ dark, accentColor, backgroundTint }),
    );
  }, [user?.id, appearanceReady, dark, accentColor, backgroundTint]);

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
      const taskData = await api("/tasks");
      setTasks(taskData);
    } catch {
      // Task polling should not disrupt the page the user is working in.
    }
  }

  async function refreshLibrary() {
    try {
      setLibrary(await api("/library/tree"));
    } catch {
      // Library polling is best-effort after approval execution.
    }
  }

  async function refreshNotifications() {
    try {
      setNotifications(await api("/notifications"));
    } catch {
      // Notification polling is best-effort.
    }
  }

  async function refreshApprovals() {
    try {
      setApprovals(await api("/approvals"));
    } catch {
      // Approval polling is best-effort.
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
        body: JSON.stringify({ path: null, files: importFiles }),
      });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Import review queued", body: "A proposal batch will appear in Approvals." });
      setPage("Approvals");
      window.setTimeout(() => {
        refreshApprovals();
        refreshTasks();
      }, 2500);
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

  async function approveBatch(batchId) {
    setLoading(true);
    try {
      const task = await api(`/approvals/${batchId}/approve`, { method: "POST" });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Approval queued", body: "Selected changes were sent to the task queue." });
      await refreshApprovals();
    } catch (approvalError) {
      setError(approvalError.message);
    } finally {
      setLoading(false);
    }
  }

  async function approveItems(items) {
    setLoading(true);
    try {
      const batchIds = [...new Set(items.map((item) => item.batch_id))];
      const createdTasks = [];
      const itemsByBatch = groupBy(items, (item) => item.batch_id);
      for (const [batchId, batchItems] of itemsByBatch) {
        createdTasks.push(
          await api(`/approvals/${batchId}/approve`, {
            method: "POST",
            body: JSON.stringify({ item_ids: batchItems.map((item) => item.id) }),
          }),
        );
      }
      setTasks((current) => createdTasks.reduce((next, task) => upsertTask(next, task), current));
      setToast({ title: "Approvals queued", body: `${batchIds.length} approval groups were sent to the task queue.` });
      await refreshApprovals();
      window.setTimeout(refreshLibrary, 3500);
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

  async function rejectItems(items) {
    setLoading(true);
    try {
      const itemsByBatch = groupBy(items, (item) => item.batch_id);
      for (const [batchId, batchItems] of itemsByBatch) {
        await api(`/approvals/${batchId}/reject`, {
          method: "POST",
          body: JSON.stringify({ item_ids: batchItems.map((item) => item.id), suppress_for: "week" }),
        });
      }
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
                onApprove={approveItems}
                onReject={rejectItems}
              />
            )}
            {page === "Import" && (
              <ImportWizard
                files={importFiles}
                onScan={scanImportFolder}
                onPropose={proposeImport}
                onFilesChange={setImportFiles}
                loading={loading}
                activeImportTask={activeImportTask}
              />
            )}
            {page === "Tasks" && <TasksView tasks={tasks} />}
            {page === "Settings" && (
              <SettingsPanel
                accentColor={accentColor}
                setAccentColor={setAccentColor}
                backgroundTint={backgroundTint}
                setBackgroundTint={setBackgroundTint}
                user={user}
                apiKey={token}
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
  const groups = useMemo(() => groupApprovalBatches(approvals), [approvals]);

  if (groups.length === 0) {
    return <EmptyState title="No pending approvals" body="Import scans and download searches will create review batches here." />;
  }

  return (
    <div className="approval-tree">
      {groups.map((group) => (
        <ApprovalBatch key={group.id} batch={group} onSelection={onSelection} onApprove={onApprove} onReject={onReject} />
      ))}
    </div>
  );
}

function ApprovalBatch({ batch, onSelection, onApprove, onReject }) {
  const [openItems, setOpenItems] = useState(() => new Set(batch.items.filter((item) => !item.parent_id).map((item) => item.id)));
  const tree = useMemo(() => buildItemTree(batch.items), [batch.items]);
  const selectedItems = batch.items.filter((item) => item.selected);
  const allSelected = selectedItems.length === batch.items.length && batch.items.length > 0;

  useEffect(() => {
    setOpenItems(new Set(batch.items.filter((item) => !item.parent_id).map((item) => item.id)));
  }, [batch.id, batch.items.length]);

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
          <button className="secondary" onClick={() => onReject(selectedItems)} disabled={selectedItems.length === 0}>
            Reject selected
          </button>
          <button className="primary" onClick={() => onApprove(selectedItems)} disabled={selectedItems.length === 0}>
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
            onChange={(event) => {
              for (const [batchId, items] of groupBy(batch.items, (item) => item.batch_id)) {
                onSelection(batchId, items.map((item) => item.id), event.target.checked);
              }
            }}
          />
          Select all visible
        </label>
        <span>{selectedItems.length} selected</span>
      </div>
      {tree.roots.map((item) => (
        <ApprovalNode
          item={item}
          childrenById={tree.childrenById}
          openItems={openItems}
          setOpenItems={setOpenItems}
          onSelection={onSelection}
          key={item.id}
        />
      ))}
    </section>
  );
}

function ApprovalNode({ item, childrenById, openItems, setOpenItems, onSelection, depth = 0 }) {
  const children = childrenById.get(item.id) || [];
  const hasChildren = children.length > 0;
  const open = openItems.has(item.id);
  const descendantIds = collectItemIds(item, childrenById);

  return (
    <>
      <div className="proposal-row" style={{ "--depth": depth }}>
        <input type="checkbox" checked={item.selected} onChange={(event) => onSelection(item.batch_id, descendantIds, event.target.checked)} />
        <button
          className="row-toggle"
          disabled={!hasChildren}
          onClick={() => toggleSet(setOpenItems, item.id)}
          title={hasChildren ? "Toggle branch" : ""}
        >
          {hasChildren ? (open ? <ChevronDown size={15} /> : <ChevronRight size={15} />) : null}
        </button>
        <span className="proposal-title">{item.title}</span>
        <small>{item.kind}</small>
      </div>
      {open &&
        children.map((child) => (
          <ApprovalNode
            item={child}
            childrenById={childrenById}
            openItems={openItems}
            setOpenItems={setOpenItems}
            onSelection={onSelection}
            depth={depth + 1}
            key={child.id}
          />
        ))}
    </>
  );
}

function ImportWizard({ files, onScan, onPropose, onFilesChange, loading, activeImportTask }) {
  return (
    <div className="import-view">
      <div className="action-bar">
        <button className="primary" onClick={onScan} disabled={loading}>
          <RefreshCw size={16} />
          Scan import folder
        </button>
        <button className="secondary" onClick={onPropose} disabled={loading || activeImportTask || files.length === 0}>
          {activeImportTask ? "Review batch running" : "Create review batch"}
        </button>
      </div>
      {files.length === 0 ? (
        <EmptyState title="No scanned files" body="Place audio files in /app/import, then scan the import folder." />
      ) : (
        <ImportTree files={files} onFilesChange={onFilesChange} />
      )}
    </div>
  );
}

function ImportTree({ files, onFilesChange }) {
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [draggedAlbum, setDraggedAlbum] = useState(null);
  const [draggedTrack, setDraggedTrack] = useState(null);
  const grouped = useMemo(() => groupImportFiles(files), [files]);

  useEffect(() => {
    setOpenArtists(new Set(grouped.map((artist) => artist.name)));
    setOpenAlbums(new Set(grouped.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
  }, [grouped]);

  return (
    <div className="tree">
      {grouped.map((artist) => (
        <div key={artist.name}>
          <div
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
              if (draggedAlbum) {
                updateImportAlbum(files, onFilesChange, draggedAlbum.artist, draggedAlbum.album, { artist: artist.name, albumartist: artist.name });
                setDraggedAlbum(null);
              }
            }}
          >
            <TreeRow
              icon={Folder}
              open={openArtists.has(artist.name)}
              title={artist.name}
              meta={`${artist.count} files`}
              onToggle={() => toggleSet(setOpenArtists, artist.name)}
            />
          </div>
          {openArtists.has(artist.name) &&
            artist.albums.map((album) => {
              const albumId = `${artist.name}/${album.name}`;
              return (
                <div key={albumId}>
                  <div
                    draggable
                    onDragStart={() => setDraggedAlbum({ artist: artist.name, album: album.name })}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={() => {
                      if (draggedTrack) {
                        updateImportFile(files, onFilesChange, draggedTrack, { artist: artist.name, albumartist: artist.name, album: album.name });
                        setDraggedTrack(null);
                      }
                    }}
                  >
                    <TreeRow
                      depth={1}
                      icon={Folder}
                      open={openAlbums.has(albumId)}
                      title={album.name}
                      meta={`${album.files.length} tracks`}
                      onToggle={() => toggleSet(setOpenAlbums, albumId)}
                    />
                  </div>
                  {openAlbums.has(albumId) &&
                    album.files.map((file) => (
                      <ImportTrackRow
                        file={file}
                        onDragStart={() => setDraggedTrack(file.path)}
                        onChange={(patch) => updateImportFile(files, onFilesChange, file.path, patch)}
                        key={file.path}
                      />
                    ))}
                </div>
              );
            })}
        </div>
      ))}
    </div>
  );
}

function ImportTrackRow({ file, onChange, onDragStart }) {
  const metadata = file.metadata || {};
  return (
    <div className="import-edit-row" draggable onDragStart={onDragStart}>
      <span className="chevron" />
      <FileAudio size={17} />
      <input value={metadata.artist || ""} onChange={(event) => onChange({ artist: event.target.value, albumartist: event.target.value })} />
      <input value={metadata.album || ""} onChange={(event) => onChange({ album: event.target.value })} />
      <input value={metadata.track_number || ""} onChange={(event) => onChange({ track_number: parseInt(event.target.value, 10) || null })} />
      <input value={metadata.title || ""} onChange={(event) => onChange({ title: event.target.value })} />
      <small>{formatBytes(file.size_bytes)}</small>
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

function SettingsPanel({ accentColor, setAccentColor, backgroundTint, setBackgroundTint, user, apiKey }) {
  const [showApiKey, setShowApiKey] = useState(false);
  const canViewApiKey =
    user?.is_admin || user?.permissions?.includes("settings:manage") || user?.permissions?.includes("users:manage");

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
      {canViewApiKey && (
        <section className="settings-section">
          <h2>API Access</h2>
          <div className="api-key-row">
            <input readOnly type={showApiKey ? "text" : "password"} value={apiKey} />
            <button className="secondary" onClick={() => setShowApiKey((value) => !value)}>
              {showApiKey ? "Hide" : "Show"}
            </button>
          </div>
        </section>
      )}
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

function buildItemTree(items) {
  const childrenById = new Map();
  const roots = [];
  items.forEach((item) => childrenById.set(item.id, []));
  items.forEach((item) => {
    if (item.parent_id && childrenById.has(item.parent_id)) {
      childrenById.get(item.parent_id).push(item);
    } else {
      roots.push(item);
    }
  });
  return { roots, childrenById };
}

function groupApprovalBatches(batches) {
  const groups = new Map();
  const seen = new Set();
  batches.forEach((batch) => {
    const batchGroupKind = batch.kind === "import_files" ? "import_files" : null;
    batch.items.forEach((item) => {
      if (!["pending", "approved", "executing"].includes(item.status)) return;
      const groupKind = batchGroupKind || item.kind;
      const key = `${groupKind}:${item.kind}:${item.title}:${item.old_value || ""}:${item.new_value || ""}`;
      if (seen.has(key)) return;
      seen.add(key);
      if (!groups.has(groupKind)) {
        groups.set(groupKind, {
          id: `type:${groupKind}`,
          title: approvalTypeLabels[groupKind] || groupKind,
          status: "pending",
          items: [],
        });
      }
      groups.get(groupKind).items.push(item);
    });
  });
  return [...groups.values()];
}

function collectItemIds(item, childrenById) {
  const children = childrenById.get(item.id) || [];
  return [item.id, ...children.flatMap((child) => collectItemIds(child, childrenById))];
}

function updateImportFile(files, onFilesChange, path, metadataPatch) {
  onFilesChange(
    files.map((file) => {
      if (file.path !== path) return file;
      const metadata = { ...(file.metadata || {}), ...metadataPatch };
      return {
        ...file,
        metadata,
        suggested_library_path: suggestImportPath(file, metadata),
      };
    }),
  );
}

function updateImportAlbum(files, onFilesChange, artistName, albumName, metadataPatch) {
  onFilesChange(
    files.map((file) => {
      const metadata = file.metadata || {};
      const currentArtist = metadata.albumartist || metadata.artist || "Unknown Artist";
      const currentAlbum = metadata.album || "Unknown Album";
      if (currentArtist !== artistName || currentAlbum !== albumName) return file;
      return {
        ...file,
        metadata: { ...metadata, ...metadataPatch },
        suggested_library_path: suggestImportPath(file, { ...metadata, ...metadataPatch }),
      };
    }),
  );
}

function suggestImportPath(file, metadata) {
  const artist = safePathPart(metadata.albumartist || metadata.artist || "Unknown Artist");
  const album = safePathPart(metadata.album || "Unknown Album");
  const title = safePathPart(metadata.title || "Unknown Title");
  const extension = file.extension || `.${file.path.split(".").pop()}`;
  const track = metadata.track_number ? String(metadata.track_number).padStart(2, "0") : "#";
  return `/app/library/${artist}/${album}/${track}-${title}${extension}`;
}

function safePathPart(value) {
  return String(value || "")
    .replace(/[/:*?"<>|]/g, "_")
    .replace(/\s+/g, " ")
    .replace(/^\.+|\.+$/g, "")
    .trim();
}

function groupImportFiles(files) {
  const artistMap = new Map();
  files.forEach((file) => {
    const artistName = file.metadata?.albumartist || file.metadata?.artist || "Unknown Artist";
    const albumName = file.metadata?.album || "Unknown Album";
    if (!artistMap.has(artistName)) {
      artistMap.set(artistName, { name: artistName, count: 0, albumMap: new Map() });
    }
    const artist = artistMap.get(artistName);
    artist.count += 1;
    if (!artist.albumMap.has(albumName)) {
      artist.albumMap.set(albumName, { name: albumName, files: [] });
    }
    artist.albumMap.get(albumName).files.push(file);
  });

  return [...artistMap.values()]
    .map((artist) => ({
      name: artist.name,
      count: artist.count,
      albums: [...artist.albumMap.values()].map((album) => ({
        ...album,
        files: album.files.sort((a, b) => (a.metadata?.track_number || 9999) - (b.metadata?.track_number || 9999)),
      })),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function upsertTask(tasks, task) {
  const withoutTask = tasks.filter((current) => current.id !== task.id);
  return [task, ...withoutTask];
}

function groupBy(items, getKey) {
  const groups = new Map();
  items.forEach((item) => {
    const key = getKey(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return groups;
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
