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
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Shield,
  Sparkles,
  Sun,
  Users,
  Wrench,
  X,
} from "lucide-react";
import "./styles.css";

const API_BASE = "/api/v1";
const TOKEN_KEY = "nudibranch_api_key";
const APPEARANCE_KEY = "nudibranch_appearance";

const navItems = [
  ["Library", Music],
  ["Import", HardDriveUpload],
  ["Wishlist", Sparkles],
  ["Task Queue", ListChecks],
  ["Downloads", Download],
  ["Playlists", FileAudio],
  ["Activity", Database],
  ["Tools", Wrench],
  ["Users", Users],
  ["Settings", Settings],
];

const pageDescriptions = {
  Library: "Browse artists, albums, and tracks in the managed library.",
  Import: "Scan new files and prepare them for review.",
  Wishlist: "Request artists, albums, and tracks for download.",
  "Task Queue": "Review requested changes before they run.",
  Downloads: "Review download searches, candidates, and completed transfers.",
  Playlists: "Create, import, and manage playlists.",
  Activity: "Track queued, running, completed, and failed work.",
  Tools: "Run maintenance checks and library actions.",
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
      setToast({ title: "Import review queued", body: "A review item was added to the task queue." });
      setPage("Task Queue");
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

  async function recheckImportTrack(file) {
    setLoading(true);
    setError("");
    try {
      const data = await api("/imports/acoustic-match", {
        method: "POST",
        body: JSON.stringify({ file }),
      });
      const candidate = data.candidates?.[0];
      if (!candidate) {
        setToast({ title: "No metadata match", body: "No acoustic match was found for this track." });
        return;
      }
      const metadataPatch = compactMetadata(candidate.metadata || {});
      setImportFiles((current) => patchImportFile(current, file.path, metadataPatch));
      setToast({ title: "Metadata updated", body: "The most likely acoustic match was applied." });
    } catch (lookupError) {
      setError(lookupError.message);
      setToast({ title: "Metadata lookup failed", body: lookupError.message });
    } finally {
      setLoading(false);
    }
  }

  async function lookupImportAlbum(artist, album) {
    setLoading(true);
    setError("");
    try {
      const data = await api("/imports/album-lookup", {
        method: "POST",
        body: JSON.stringify({ artist, album }),
      });
      setToast({ title: "Album checked", body: `${data.tracks?.length || 0} tracks found.` });
      return data;
    } catch (lookupError) {
      setError(lookupError.message);
      setToast({ title: "Album lookup failed", body: lookupError.message });
      return null;
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
      setToast({ title: "Tasks queued", body: `${batchIds.length} change groups were sent to the task queue.` });
      await refreshApprovals();
      window.setTimeout(refreshLibrary, 3500);
    } catch (approvalError) {
      setError(approvalError.message);
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
            <input placeholder="Search library, queue, activity" />
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
            {page === "Task Queue" && (
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
                library={library}
                onRecheckTrack={recheckImportTrack}
                onCheckAlbum={lookupImportAlbum}
                loading={loading}
                activeImportTask={activeImportTask}
              />
            )}
            {page === "Activity" && <TasksView tasks={tasks} />}
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
            {page === "Tools" && <ToolsView tasks={tasks} notifications={notifications} />}
            {!["Library", "Task Queue", "Import", "Activity", "Settings", "Tools"].includes(page) && <Placeholder page={page} />}
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
  const description = page === "Task Queue" ? `${selectedCount} selected changes are ready to run.` : pageDescriptions[page];

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
    return <EmptyState title="No library records" body="Import queued music to populate the managed library." />;
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
    return <EmptyState title="No queued changes" body="Import scans, download searches, and maintenance actions will add review items here." />;
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
            Run selected
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

function ImportWizard({ files, onScan, onPropose, onFilesChange, library, onRecheckTrack, onCheckAlbum, loading, activeImportTask }) {
  const [manualAlbums, setManualAlbums] = useState([]);
  const [albumRecords, setAlbumRecords] = useState({});
  const [albumSearchOpen, setAlbumSearchOpen] = useState(false);

  function addManualAlbum(album) {
    setManualAlbums((current) => [...current, album]);
    setAlbumRecords((current) => ({
      ...current,
      [albumRecordKey(album.artist, album.name)]: album.tracks,
    }));
    setAlbumSearchOpen(false);
  }

  async function checkAlbum(artist, album) {
    const record = await onCheckAlbum(artist, album);
    if (!record?.tracks?.length) return null;
    setAlbumRecords((current) => ({
      ...current,
      [albumRecordKey(record.artist || artist, record.album || album)]: record.tracks,
      [albumRecordKey(artist, album)]: record.tracks,
    }));
    return record;
  }

  return (
    <div className="import-view">
      <div className="action-bar">
        <button className="primary" onClick={onScan} disabled={loading}>
          <RefreshCw size={16} />
          Scan import folder
        </button>
        <button className="secondary" onClick={() => setAlbumSearchOpen((value) => !value)}>
          <Plus size={16} />
          Add album
        </button>
        <button className="secondary" onClick={onPropose} disabled={loading || activeImportTask || files.length === 0}>
          {activeImportTask ? "Import review running" : "Add to task queue"}
        </button>
      </div>
      {albumSearchOpen && <AlbumSearchPanel onAdd={addManualAlbum} onLookup={checkAlbum} />}
      {files.length === 0 && manualAlbums.length === 0 ? (
        <EmptyState title="No scanned files" body="Place audio files in /app/import, then scan the import folder." />
      ) : (
        <ImportTree
          files={files}
          onFilesChange={onFilesChange}
          library={library}
          manualAlbums={manualAlbums}
          albumRecords={albumRecords}
          onRecheckTrack={onRecheckTrack}
          onCheckAlbum={checkAlbum}
        />
      )}
    </div>
  );
}

function AlbumSearchPanel({ onAdd, onLookup }) {
  const [artist, setArtist] = useState("");
  const [album, setAlbum] = useState("");
  const [tracks, setTracks] = useState(10);

  async function submit(event) {
    event.preventDefault();
    if (!artist.trim() || !album.trim()) return;
    const record = await onLookup(artist.trim(), album.trim());
    if (record?.tracks?.length) {
      onAdd({
        id: record.musicbrainz_album_id || `manual:${Date.now()}`,
        name: record.album || album.trim(),
        artist: record.artist || artist.trim(),
        tracks: record.tracks,
      });
      return;
    }
    onAdd({
      id: `manual:${Date.now()}`,
      name: album.trim(),
      artist: artist.trim(),
      tracks: Array.from({ length: Number(tracks) || 10 }, (_, index) => ({
        track_number: index + 1,
        title: `Track ${index + 1}`,
      })),
    });
  }

  return (
    <form className="album-search-panel" onSubmit={submit}>
      <label>
        Artist
        <input value={artist} onChange={(event) => setArtist(event.target.value)} />
      </label>
      <label>
        Album
        <input value={album} onChange={(event) => setAlbum(event.target.value)} />
      </label>
      <label>
        Tracks
        <input type="number" min="1" max="80" value={tracks} onChange={(event) => setTracks(event.target.value)} />
      </label>
      <button className="primary">
        <Plus size={16} />
        Add
      </button>
    </form>
  );
}

function ImportTree({ files, onFilesChange, library, manualAlbums, albumRecords, onRecheckTrack, onCheckAlbum }) {
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [draggedAlbum, setDraggedAlbum] = useState(null);
  const [draggedTrack, setDraggedTrack] = useState(null);
  const [downloadSelections, setDownloadSelections] = useState(() => new Set());
  const [dismissedGhosts, setDismissedGhosts] = useState(() => new Set());
  const grouped = useMemo(() => groupImportFiles(files, library, manualAlbums, albumRecords), [files, library, manualAlbums, albumRecords]);

  useEffect(() => {
    setOpenArtists(new Set(grouped.map((artist) => artist.name)));
    setOpenAlbums(new Set(grouped.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
  }, [files.length, manualAlbums.length]);

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
                    <div className="tree-action-row">
                      <TreeRow
                        depth={1}
                        icon={Folder}
                        open={openAlbums.has(albumId)}
                        title={album.name}
                        meta={`${album.files.length}/${album.slots.length} matched · ${album.matchStatus}`}
                        warning={album.matchStatus === "partial"}
                        onToggle={() => toggleSet(setOpenAlbums, albumId)}
                      />
                      <button className="row-icon-button" onClick={() => onCheckAlbum(artist.name, album.name)} title="Check album records">
                        <Search size={15} />
                      </button>
                    </div>
                  </div>
                  {openAlbums.has(albumId) &&
                    album.slots.filter((slot) => slot.file || !dismissedGhosts.has(slot.id)).map((slot) =>
                      slot.file ? (
                        <ImportTrackRow
                          file={slot.file}
                          album={album}
                          onDragStart={() => setDraggedTrack(slot.file.path)}
                          onChange={(patch) => updateImportFile(files, onFilesChange, slot.file.path, patch)}
                          onRecheck={() => onRecheckTrack(slot.file)}
                          key={slot.file.path}
                        />
                      ) : (
                        <GhostTrackRow
                          key={`${albumId}:${slot.track_number}:${slot.title}`}
                          slot={slot}
                          checked={downloadSelections.has(slot.id)}
                          onChecked={(checked) => toggleDownloadSelection(setDownloadSelections, slot.id, checked)}
                          onDismiss={() => toggleDownloadSelection(setDismissedGhosts, slot.id, true)}
                          onDrop={() => {
                            if (draggedTrack) {
                              const draggedFile = files.find((file) => file.path === draggedTrack);
                              updateImportFile(files, onFilesChange, draggedTrack, {
                                artist: artist.name,
                                albumartist: artist.name,
                                album: album.name,
                                track_number: slot.track_number,
                                title: titleForDroppedSlot(slot, draggedFile),
                              });
                              setDraggedTrack(null);
                            }
                          }}
                        />
                      ),
                    )}
                </div>
              );
            })}
        </div>
      ))}
    </div>
  );
}

function ImportTrackRow({ file, album, onChange, onDragStart, onRecheck }) {
  const metadata = file.metadata || {};
  const [editing, setEditing] = useState(false);
  return (
    <>
      <div className="import-edit-row" draggable onDragStart={onDragStart}>
        <span className="chevron" />
        <FileAudio size={17} />
        <DraftInput value={metadata.artist || ""} onCommit={(value) => onChange({ artist: value, albumartist: value })} />
        <DraftInput value={metadata.album || ""} onCommit={(value) => onChange({ album: value })} />
        <DraftInput
          value={metadata.track_number || ""}
          onCommit={(value) => onChange({ track_number: parseInt(value, 10) || null })}
        />
        <DraftInput value={metadata.title || ""} onCommit={(value) => onChange({ title: value })} />
        <small>{album?.matchStatus === "full" ? "In library" : formatBytes(file.size_bytes)}</small>
        <button className="row-icon-button" onClick={onRecheck} title="Scan and match metadata">
          <Search size={15} />
        </button>
        <button className="row-icon-button" onClick={() => setEditing((value) => !value)} title="Edit metadata">
          <Pencil size={15} />
        </button>
      </div>
      {editing && <MetadataEditor metadata={metadata} onChange={onChange} />}
    </>
  );
}

function DraftInput({ value, onCommit, type = "text" }) {
  const [draft, setDraft] = useState(value ?? "");

  useEffect(() => {
    setDraft(value ?? "");
  }, [value]);

  function commit() {
    if (String(draft) !== String(value ?? "")) {
      onCommit(draft);
    }
  }

  return (
    <input
      type={type}
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.currentTarget.blur();
        }
      }}
    />
  );
}

function MetadataEditor({ metadata, onChange }) {
  const [extraKey, setExtraKey] = useState("");
  const visibleKeys = [
    "artist",
    "albumartist",
    "album",
    "title",
    "track_number",
    "disc_number",
    "genre",
    "date",
    "musicbrainz_artist_id",
    "musicbrainz_album_id",
    "musicbrainz_recording_id",
    "format",
    "bitrate",
    "duration_ms",
    "is_lossless",
  ];
  const keys = [...new Set([...visibleKeys, ...Object.keys(metadata || {})])];

  return (
    <div className="metadata-editor">
      {keys.map((key) => (
        <label key={key}>
          <span>{key}</span>
          {typeof metadata[key] === "boolean" ? (
            <input type="checkbox" checked={Boolean(metadata[key])} onChange={(event) => onChange({ [key]: event.target.checked })} />
          ) : (
            <DraftInput value={metadata[key] ?? ""} onCommit={(value) => onChange({ [key]: coerceMetadataValue(key, value) })} />
          )}
        </label>
      ))}
      <form
        className="metadata-add-row"
        onSubmit={(event) => {
          event.preventDefault();
          if (!extraKey.trim()) return;
          onChange({ [extraKey.trim()]: "" });
          setExtraKey("");
        }}
      >
        <input value={extraKey} placeholder="Add tag" onChange={(event) => setExtraKey(event.target.value)} />
        <button className="secondary">
          <Plus size={15} />
          Add tag
        </button>
      </form>
    </div>
  );
}

function GhostTrackRow({ slot, checked, onChecked, onDismiss, onDrop }) {
  return (
    <div className="ghost-track-row" onDragOver={(event) => event.preventDefault()} onDrop={onDrop}>
      <span className="chevron" />
      <FileAudio size={17} />
      <label>
        <input type="checkbox" checked={checked} onChange={(event) => onChecked(event.target.checked)} />
        Download
      </label>
      <span className="ghost-title">
        {slot.track_number ? String(slot.track_number).padStart(2, "0") : "#"}-{slot.title}
      </span>
      <small>{slot.reason}</small>
      <button className="row-icon-button" onClick={onDismiss} title="Dismiss slot">
        <X size={15} />
      </button>
    </div>
  );
}

function TasksView({ tasks }) {
  const [openTasks, setOpenTasks] = useState(() => new Set());
  if (tasks.length === 0) {
    return <EmptyState title="No activity" body="Scans, queued changes, downloads, and notifications will appear here." />;
  }

  return (
    <div className="task-list">
      {tasks.map((task) => (
        <section className="task-entry" key={task.id}>
          <button className="task-row" onClick={() => toggleSet(setOpenTasks, task.id)}>
            <strong>{task.type}</strong>
            <span>{task.status}</span>
            <small>{taskSummary(task)}</small>
          </button>
          {openTasks.has(task.id) && (
            <pre className="task-detail">{JSON.stringify({ payload: task.payload, result: task.result, error: task.error }, null, 2)}</pre>
          )}
        </section>
      ))}
    </div>
  );
}

function ToolsView({ tasks, notifications }) {
  const [query, setQuery] = useState("");
  const tools = [
    ["Scan Jellyfin", "Request Jellyfin to refresh the managed library."],
    ["Find missing album tracks", "Compare known albums against library records and queue download searches."],
    ["Check files against database", "Find library files missing from the database and records with missing files."],
    ["Backup now", "Create a manual backup when no file operations are running."],
  ];

  const logs = buildLiveLog(tasks, notifications).filter((entry) => entry.text.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="tools-view">
      <div className="tool-grid">
        {tools.map(([title, body]) => (
          <button className="tool-card" key={title} disabled>
            <Wrench size={18} />
            <span>
              <strong>{title}</strong>
              <small>{body}</small>
            </span>
          </button>
        ))}
      </div>
      <section className="log-panel">
        <div className="log-header">
          <h2>Live Log</h2>
          <input placeholder="Search log" value={query} onChange={(event) => setQuery(event.target.value)} />
        </div>
        <div className="log-list">
          {logs.map((entry) => (
            <pre className={entry.level === "error" ? "log-row error" : "log-row"} key={entry.id}>{entry.text}</pre>
          ))}
        </div>
      </section>
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
        <label>Queue</label>
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
      if (!["pending", "approved", "executing", "failed"].includes(item.status)) return;
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
  onFilesChange(patchImportFile(files, path, metadataPatch));
}

function patchImportFile(files, path, metadataPatch) {
  return files.map((file) => {
    if (file.path !== path) return file;
    const metadata = { ...(file.metadata || {}), ...metadataPatch };
    return {
      ...file,
      metadata,
      suggested_library_path: suggestImportPath(file, metadata),
    };
  });
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

function groupImportFiles(files, library = [], manualAlbums = [], albumRecords = {}) {
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

  manualAlbums.forEach((album) => {
    if (!artistMap.has(album.artist)) {
      artistMap.set(album.artist, { name: album.artist, count: 0, albumMap: new Map() });
    }
    const artist = artistMap.get(album.artist);
    if (!artist.albumMap.has(album.name)) {
      artist.albumMap.set(album.name, { name: album.name, files: [], expectedTracks: album.tracks, manual: true });
    }
  });

  return [...artistMap.values()]
    .map((artist) => ({
      name: artist.name,
      count: artist.count,
      albums: [...artist.albumMap.values()].map((album) => buildImportAlbum(album, artist.name, library, albumRecords)),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function buildImportAlbum(album, artistName, library, albumRecords) {
  const files = album.files.sort((a, b) => (a.metadata?.track_number || 9999) - (b.metadata?.track_number || 9999));
  const libraryAlbum = findLibraryAlbum(library, artistName, album.name);
  const recordTracks = albumRecords[albumRecordKey(artistName, album.name)];
  const expectedTracks = recordTracks || album.expectedTracks || libraryAlbum?.tracks || inferExpectedTracks(files);
  const trackMap = new Map();
  files.forEach((file) => {
    const trackNumber = file.metadata?.track_number;
    if (trackNumber) trackMap.set(trackNumber, file);
  });
  const slots = expectedTracks.map((track, index) => {
    const trackNumber = track.track_number || index + 1;
    const file = trackMap.get(trackNumber);
    return file
      ? { id: file.path, track_number: trackNumber, title: file.metadata?.title || track.title, file }
      : {
          id: `${artistName}:${album.name}:${trackNumber}:${track.title}`,
          track_number: trackNumber,
          title: track.title || `Track ${trackNumber}`,
          reason: recordTracks ? "Missing from album record" : libraryAlbum ? "Missing from import" : "Album slot",
        };
  });
  const matchedCount = slots.filter((slot) => slot.file).length;
  const matchStatus = libraryAlbum ? (matchedCount >= expectedTracks.length ? "full" : "partial") : "new";
  return {
    ...album,
    files,
    slots,
    matchStatus,
    libraryAlbum,
  };
}

function albumRecordKey(artist, album) {
  return `${normalizeName(artist)}::${normalizeName(album)}`;
}

function titleForDroppedSlot(slot, file) {
  if (isGenericTrackTitle(slot.title)) {
    return file?.metadata?.title || slot.title;
  }
  return slot.title;
}

function isGenericTrackTitle(title) {
  return /^track\s+#?\d+$/i.test(String(title || "").trim());
}

function compactMetadata(metadata) {
  return Object.fromEntries(Object.entries(metadata).filter(([, value]) => value !== null && value !== undefined && value !== ""));
}

function findLibraryAlbum(library, artistName, albumName) {
  const normalizedArtist = normalizeName(artistName);
  const normalizedAlbum = normalizeName(albumName);
  const artist =
    library.find((entry) => normalizeName(entry.name) === normalizedArtist) ||
    library.find((entry) => normalizeName(entry.name).includes(normalizedArtist) || normalizedArtist.includes(normalizeName(entry.name)));
  if (!artist) return null;
  return (
    artist.albums.find((album) => normalizeName(album.title) === normalizedAlbum) ||
    artist.albums.find((album) => normalizeName(album.title).includes(normalizedAlbum) || normalizedAlbum.includes(normalizeName(album.title)))
  );
}

function inferExpectedTracks(files) {
  const numberedTracks = files
    .map((file) => file.metadata?.track_number)
    .filter((trackNumber) => Number.isInteger(trackNumber) && trackNumber > 0);
  const maxTrack = Math.max(files.length, numberedTracks.length ? Math.max(...numberedTracks) : 0);
  return Array.from({ length: maxTrack }, (_, index) => ({
    track_number: index + 1,
    title: files.find((file) => file.metadata?.track_number === index + 1)?.metadata?.title || `Track ${index + 1}`,
  }));
}

function normalizeName(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\([^)]*\)|\[[^\]]*\]/g, "")
    .replace(/deluxe|expanded|remaster(?:ed)?|edition|explicit/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function toggleDownloadSelection(setter, id, checked) {
  setter((current) => {
    const next = new Set(current);
    if (checked) next.add(id);
    else next.delete(id);
    return next;
  });
}

function coerceMetadataValue(key, value) {
  if (["track_number", "disc_number", "bitrate", "duration_ms"].includes(key)) {
    return parseInt(value, 10) || null;
  }
  return value;
}

function upsertTask(tasks, task) {
  const withoutTask = tasks.filter((current) => current.id !== task.id);
  return [task, ...withoutTask];
}

function taskSummary(task) {
  if (task.error) return task.error;
  if (task.result?.errors?.length) return task.result.errors.join("; ");
  if (task.result?.imported !== undefined) return `${task.result.imported} imported, ${task.result.skipped || 0} skipped`;
  return new Date(task.created_at).toLocaleString();
}

function buildLiveLog(tasks, notifications) {
  const taskEntries = tasks.map((task) => ({
    id: `task:${task.id}`,
    level: task.status === "failed" || task.error || task.result?.errors?.length ? "error" : "info",
    createdAt: task.updated_at || task.created_at,
    text: `[${new Date(task.updated_at || task.created_at).toLocaleString()}] ${task.type} ${task.status}: ${taskSummary(task)}`,
  }));
  const notificationEntries = notifications.map((notification) => ({
    id: `notification:${notification.id}`,
    level: notification.event_type?.includes("failed") || notification.title?.toLowerCase().includes("failed") ? "error" : "info",
    createdAt: notification.created_at,
    text: `[${new Date(notification.created_at).toLocaleString()}] ${notification.title}: ${notification.body}`,
  }));
  return [...taskEntries, ...notificationEntries].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
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
