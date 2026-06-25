import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { createRoot } from "react-dom/client";
import {
  ArrowLeft,
  Bell,
  Info,
  Check,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Compass,
  Database,
  FileAudio,
  Folder,
  GripVertical,
  HardDriveUpload,
  House,
  Heart,
  ListChecks,
  ListMusic,
  ListPlus,
  LogOut,
  Maximize2,
  Menu,
  Ban,
  Mic2,
  Minimize2,
  Moon,
  Music,
  Pencil,
  Pause,
  Pin,
  PinOff,
  PictureInPicture2,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Shield,
  Sparkles,
  SkipBack,
  SkipForward,
  Shuffle,
  Repeat,
  Repeat1,
  Sun,
  Trash2,
  Upload,
  Users,
  Wrench,
  X,
  Zap,
} from "lucide-react";
import "./styles.css";

const API_BASE = "/api/v1";
const TOKEN_KEY = "nudibranch_api_key";
const APPEARANCE_LAST_KEY = "nudibranch_appearance_last";
const DEVICE_LABEL_KEY = "nudibranch_device_label";

// Stable per-browser device label so re-logins reuse one session instead of
// piling up a fresh "Web" session every time (backend dedupes by device_label).
function getDeviceLabel() {
  let label = localStorage.getItem(DEVICE_LABEL_KEY);
  if (!label) {
    label = `Web · ${Math.random().toString(36).slice(2, 8)}`;
    localStorage.setItem(DEVICE_LABEL_KEY, label);
  }
  return label;
}
const DEFAULT_APPEARANCE = { dark: false, accentColor: "#356df3", backgroundTint: "#356df3" };

const navItems = [
  ["Home", House],
  ["Library", Music],
  ["Discover", Compass],
  ["Import/Add", HardDriveUpload],
  ["Wishlist", Sparkles],
  ["Task Queue", ListChecks],
  ["Playlists", FileAudio],
  ["Activity", Database],
  ["Tools", Wrench],
  ["Automations", Zap],
  ["Users", Users],
  ["Settings", Settings],
];

const pageDescriptions = {
  Home: "Your library at a glance.",
  Library: "Browse artists, albums, and tracks in the library.",
  Discover: "Search for artists, albums, and tracks to request or download.",
  "Import/Add": "Scan new files, add album records, and prepare them for review.",
  Wishlist: "Request music for dowload.",
  "Wishlist Approvals": "Review user wishlist requests.",
  "Task Queue": "Review requested changes before they run.",
  Playlists: "Create, import, and manage playlists.",
  Activity: "Track queued, running, completed, and failed work.",
  Tools: "Run tools to manage your library.",
  Automations: "Run tools or other actions automatically when triggered or on a schedule.",
  Users: "Manage users, passwords, API keys, and permissions.",
  Settings: "Manage settings.",
};

// Pages that render full-width with no Inspector aside.
const NO_INSPECTOR_PAGES = new Set(["Home", "Settings", "Discover"]);

const approvalTypeLabels = {
  import_files: "Imports",
  download: "Download candidates",
  metadata: "Metadata",
  artwork: "Artwork",
  lyrics: "Lyrics",
  file_move: "File moves",
  delete: "Deletes",
  jellyfin_sync: "Jellyfin sync",
  playlist: "Playlists",
};

function App() {
  const initialAppearance = readInitialAppearance();
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [user, setUser] = useState(null);
  const [playerDiagnostics, setPlayerDiagnostics] = useState(() => {
    try { return localStorage.getItem("nudibranch:playerDiagnostics") === "1"; } catch { return false; }
  });
  const togglePlayerDiagnostics = (next) => {
    setPlayerDiagnostics(next);
    try { localStorage.setItem("nudibranch:playerDiagnostics", next ? "1" : "0"); } catch { /* ignore */ }
  };
  const [page, setPage] = useState("Library");
  const [albumDetail, setAlbumDetail] = useState(null);
  const [artistDetail, setArtistDetail] = useState(null);
  const [homeVersion, setHomeVersion] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [importUploadProgress, setImportUploadProgress] = useState(null);
  const [pinnedAlbumIds, setPinnedAlbumIds] = useState(() => new Set());
  const [pinnedArtistIds, setPinnedArtistIds] = useState(() => new Set());
  const [dark, setDark] = useState(initialAppearance.dark);
  const [trayOpen, setTrayOpen] = useState(false);
  const [toast, setToast] = useState(null);
  const [accentColor, setAccentColor] = useState(initialAppearance.accentColor);
  const [backgroundTint, setBackgroundTint] = useState(initialAppearance.backgroundTint);
  const [crossfadeDuration, setCrossfadeDuration] = useState(0.5);
  const [library, setLibrary] = useState([]);
  const [importFiles, setImportFiles] = useState([]);
  const [importSeedDownloads, setImportSeedDownloads] = useState([]);
  const addImportAlbumsRef = useRef(null);
  const playbackControlRef = useRef(null);
  const importUploadXhrRef = useRef(null); // in-flight import upload, so it can be canceled
  const unshuffledQueueRef = useRef(null); // snapshot of queue order before shuffle, to revert
  const currentSessionIdRef = useRef(null);
  const remoteExecRef = useRef(null);
  const lastRecordedPlayRef = useRef(null);
  const commandPollingRef = useRef(false);
  const [approvals, setApprovals] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [appLogs, setAppLogs] = useState([]);
  const [notifications, setNotifications] = useState([]);
  const [wishlist, setWishlist] = useState([]);
  const [wishlistApprovals, setWishlistApprovals] = useState([]);
  const [playlists, setPlaylists] = useState([]);
  const [users, setUsers] = useState([]);
  const [jellyfinUsers, setJellyfinUsers] = useState(null);
  const [jellyfinUsersLoading, setJellyfinUsersLoading] = useState(false);
  const [userPlayback, setUserPlayback] = useState({ app: [], jellyfin: [] });
  const [permissionCatalog, setPermissionCatalog] = useState([]);
  const [favoriteTrackIds, setFavoriteTrackIds] = useState(() => new Set());
  const [integrationSettings, setIntegrationSettings] = useState(null);
  const [backups, setBackups] = useState([]);
  const [importAlbumSearchOpen, setImportAlbumSearchOpen] = useState(false);
  const [importDownloadRequests, setImportDownloadRequests] = useState([]);
  const [wishlistInspectorActions, setWishlistInspectorActions] = useState(null);
  const [playlistInspectorActions, setPlaylistInspectorActions] = useState(null);
  const [mappingSyncStats, setMappingSyncStats] = useState(null);
  const [playlistImportOpen, setPlaylistImportOpen] = useState(false);
  const [playlistImportUrl, setPlaylistImportUrl] = useState("");
  const [playlistImportMode, setPlaylistImportMode] = useState("songs");
  const [playlistImportLoading, setPlaylistImportLoading] = useState(false);
    const [pendingPlaylistName, setPendingPlaylistName] = useState(null);
  const [pendingPlaylistOriginalTracks, setPendingPlaylistOriginalTracks] = useState(null);
  // Source playlist URL — the "origin" so re-importing the same playlist updates the
  // existing Nudibranch/Jellyfin playlist instead of creating a duplicate.
  const [pendingPlaylistOrigin, setPendingPlaylistOrigin] = useState(null);
  const [playerQueue, setPlayerQueue] = useState([]);
  const [currentTrack, setCurrentTrack] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [shuffle, setShuffle] = useState(false);
  const [repeat, setRepeat] = useState("off"); // off | all | one
  const [playerOpen, setPlayerOpen] = useState(false);
  const [playerPopped, setPlayerPopped] = useState(false);
  const [playerDockHeight, setPlayerDockHeight] = useState(0);
  const [playerToastHeight, setPlayerToastHeight] = useState(0);
  const [queueOpen, setQueueOpen] = useState(false);
  const [appearanceReady, setAppearanceReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const trayRef = useRef(null);
  const syncToastTaskIds = useRef(new Set());
  const checkFileTaskIds = useRef(new Set());
  const localNotificationCounter = useRef(0);
  const onlineStateRef = useRef(typeof navigator === "undefined" ? true : navigator.onLine);
  const appearanceHydratedUserId = useRef(null);
  const appearanceSaveVersion = useRef(0);

  const theme = dark ? "app dark" : "app";
  const queueGroups = useMemo(() => groupApprovalBatches(approvals), [approvals]);
  const queueSelectionCount = useMemo(
    () => queueGroups.reduce((total, group) => total + group.items.filter((item) => item.selected).length, 0),
    [queueGroups],
  );
  const queueItemCount = useMemo(
    () => queueGroups.reduce((total, group) => total + group.items.length, 0),
    [queueGroups],
  );
  const queueGroupCount = queueGroups.length;
  const queueSummary = useMemo(
    () =>
      queueItemCount === 0
        ? "No queued changes."
        : `${queueSelectionCount} of ${queueItemCount} visible changes selected across ${queueGroupCount} group${queueGroupCount === 1 ? "" : "s"}.`,
    [queueGroupCount, queueItemCount, queueSelectionCount],
  );
  const visibleNavItems = useMemo(() => navItems.filter(([label]) => canViewPage(user, label)), [user]);
  const activeImportTask = tasks.some((task) => task.type === "propose_import" && ["queued", "running"].includes(task.status));
  const activeWork = tasks.some((task) => ["queued", "running"].includes(task.status)) || approvals.some((batch) => batch.status === "executing");
  const unreadNotifications = useMemo(() => notifications.filter((notification) => notification.status === "unread"), [notifications]);
  const activeSeverity = useMemo(
    () => unreadNotifications.reduce((highest, notification) => maxSeverity(highest, notificationSeverity(notification)), "info"),
    [unreadNotifications],
  );
  const currentTrackIndex = playerQueue.findIndex((track) => track.id === currentTrack?.id);
  const playerDocked = playerOpen && !playerPopped;
  const appearanceVars = useMemo(() => buildAppearanceVars(dark, accentColor, backgroundTint), [dark, accentColor, backgroundTint]);
  const nextAudioUrl = useMemo(() => {
    const next = playerQueue[currentTrackIndex + 1];
    if (!next?.id || !token) return null;
    return `${API_BASE}/library/tracks/${next.id}/stream?api_key=${encodeURIComponent(token)}`;
  }, [playerQueue, currentTrackIndex, token]);

  const lyricsUrl = useMemo(() => {
    const track = playerQueue[currentTrackIndex];
    if (!track?.id || !token) return null;
    return `${API_BASE}/library/tracks/${track.id}/lyrics?api_key=${encodeURIComponent(token)}`;
  }, [playerQueue, currentTrackIndex, token]);

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
    const handleOffline = () => {
      if (onlineStateRef.current === false) return;
      onlineStateRef.current = false;
      notify("Offline", "Connection lost.", "ui_warning");
    };
    const handleOnline = () => {
      if (onlineStateRef.current === true) return;
      onlineStateRef.current = true;
      notify("Back online", "Connection restored.", "ui_notice");
      if (token) refreshAll();
    };
    window.addEventListener("offline", handleOffline);
    window.addEventListener("online", handleOnline);
    if (typeof navigator !== "undefined" && navigator.onLine === false) handleOffline();
    return () => {
      window.removeEventListener("offline", handleOffline);
      window.removeEventListener("online", handleOnline);
    };
  }, [token]);

  useEffect(() => {
    if (!token) return;
    refreshAll();
    const interval = window.setInterval(() => {
      if (hasPermission(user, "library:view")) refreshLibrary();
      if (hasPermission(user, "activity:read")) refreshTasks();
      if (hasPermission(user, "activity:read")) refreshLogs();
      if (hasPermission(user, "approvals:manage")) refreshApprovals();
      refreshNotifications();
      if (hasPermission(user, "playlists:manage")) refreshPlaylists();
      if (hasPermission(user, "discover")) {
        refreshWishlist();
        refreshWishlistApprovals();
      }
      if (hasPermission(user, "activity:read")) refreshUserPlayback();
    }, activeWork ? 2500 : 10000);
    return () => window.clearInterval(interval);
  }, [token, user?.id, user?.is_admin, stablePermissionKey(user?.permissions || []), activeWork]);

  useEffect(() => {
    if (!user || visibleNavItems.length === 0) return;
    if (!canViewPage(user, page)) {
      setPage(visibleNavItems[0][0]);
    }
  }, [user, page, visibleNavItems]);

  useEffect(() => {
    if (!user?.id) {
      appearanceHydratedUserId.current = null;
      setAppearanceReady(false);
      return;
    }
    if (appearanceHydratedUserId.current === user.id) return;
    appearanceHydratedUserId.current = user.id;
    setAppearanceReady(false);
    setDark(user.theme === "dark");
    setAccentColor(user.accent_color || DEFAULT_APPEARANCE.accentColor);
    setBackgroundTint(user.background_tint || DEFAULT_APPEARANCE.backgroundTint);
    setCrossfadeDuration(user.crossfade_duration ?? 0.5);
    setAppearanceReady(true);
  }, [user?.id]);

  useEffect(() => {
    if (!user?.id) return;
    if (!appearanceReady) return;
    localStorage.setItem(APPEARANCE_LAST_KEY, JSON.stringify({ dark, accentColor, backgroundTint }));
  }, [user?.id, appearanceReady, dark, accentColor, backgroundTint]);

  useEffect(() => {
    if (!user?.id || !appearanceReady) return;
    const appearance = {
      theme: dark ? "dark" : "light",
      accent_color: accentColor,
      background_tint: backgroundTint,
      crossfade_duration: crossfadeDuration,
    };
    if (
      (user.theme || "light") === appearance.theme &&
      (user.accent_color || DEFAULT_APPEARANCE.accentColor) === appearance.accent_color &&
      (user.background_tint || DEFAULT_APPEARANCE.backgroundTint) === appearance.background_tint &&
      (user.crossfade_duration ?? 0.5) === appearance.crossfade_duration
    ) {
      return;
    }
    const timeout = window.setTimeout(() => saveOwnAppearance(appearance), 250);
    return () => window.clearTimeout(timeout);
  }, [user?.id, user?.theme, user?.accent_color, user?.background_tint, user?.crossfade_duration, appearanceReady, dark, accentColor, backgroundTint, crossfadeDuration]);

  useEffect(() => {
    if (!token || !user) return;
    let cancelled = false;
    let consecutiveErrors = 0;
    let intervalId = null;
    async function fetchMappingStats() {
      try {
        const data = await api("/playlists/sync/stats");
        if (!cancelled) { setMappingSyncStats(data); consecutiveErrors = 0; }
      } catch {
        consecutiveErrors++;
        if (consecutiveErrors >= 3 && intervalId !== null && !cancelled) {
          clearInterval(intervalId);
          intervalId = null;
        }
      }
    }
    fetchMappingStats();
    intervalId = setInterval(fetchMappingStats, 30000);
    return () => { cancelled = true; if (intervalId !== null) clearInterval(intervalId); };
  }, [token, user?.id]);

  const api = useCallback(async (path, options = {}) => {
    const isFormData = options.body instanceof FormData;
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: {
        ...(isFormData ? {} : { "Content-Type": "application/json" }),
        Authorization: `Bearer ${token}`,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }, [token]);

  async function login(username, password) {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, device_label: getDeviceLabel() }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || "Invalid username or password");
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
    try {
      const me = await api("/me");
      setUser(me);
      const [permissionData, libraryTree, taskData, logData, notificationData, wishlistData, wishlistApprovalData, approvalData, playlistData, backupData] = await Promise.all([
        api("/permissions"),
        hasPermission(me, "library:view") ? api("/library/tree") : Promise.resolve([]),
        hasPermission(me, "activity:read") ? api("/tasks") : Promise.resolve([]),
        hasPermission(me, "activity:read") ? api("/logs") : Promise.resolve([]),
        api("/notifications"),
        hasPermission(me, "discover") ? api("/wishlist") : Promise.resolve([]),
        hasPermission(me, "discover") ? api("/wishlist/approvals") : Promise.resolve([]),
        hasPermission(me, "approvals:manage") ? api("/approvals") : Promise.resolve([]),
        hasPermission(me, "playlists:manage") ? api("/playlists") : Promise.resolve([]),
        hasPermission(me, "tools:manage") ? api("/tools/backups") : Promise.resolve({ backups: [] }),
      ]);
      setPermissionCatalog(permissionData);
      setLibrary(libraryTree);
      setTasks(taskData);
      setAppLogs(logData);
      handleCompletedTaskEffects(taskData, { emit: false });
      setNotifications((current) => mergeTrayNotifications(notificationData, current));
      setWishlist(wishlistData);
      setWishlistApprovals(wishlistApprovalData);
      setApprovals(approvalData);
      setPlaylists(playlistData);
      setBackups(backupData.backups || []);
      setFavoriteTrackIds(new Set(favoritePlaylistFrom(playlistData)?.track_ids || []));
      setHomeVersion((v) => v + 1);
      setRefreshVersion((v) => v + 1);
      if (canManageSettings(me)) {
        refreshIntegrationSettings();
      }
      if (canManageUsers(me)) refreshUsers();
      if (hasPermission(me, "activity:read")) refreshUserPlayback();
    } catch (refreshError) {
      if (refreshError.message.includes("Invalid API key") || refreshError.message.includes("Missing API key")) {
        logout();
      } else {
        notify("Refresh failed", refreshError.message, "ui_error");
      }
    } finally {
      setLoading(false);
    }
  }

  async function refreshTasks() {
    try {
      const taskData = await api("/tasks");
      handleCompletedTaskEffects(taskData);
      setTasks(taskData);
      if (taskData.some((task) => ["queued", "running"].includes(task.status)) && hasPermission(user, "approvals:manage")) {
        refreshApprovals();
      }
    } catch {
      // Task polling should not disrupt the page the user is working in.
    }
  }

  async function refreshLogs() {
    try {
      const logData = await api("/logs");
      setAppLogs(logData);
    } catch {
      // Log polling should not interrupt active work.
    }
  }

  function handleCompletedTaskEffects(taskData, { emit = true } = {}) {
    taskData.forEach((task) => {
      if (task.status !== "completed") return;
      if (task.type === "sync_favorites_jellyfin") {
        if (syncToastTaskIds.current.has(task.id)) return;
        syncToastTaskIds.current.add(task.id);
        if (!emit) return;
        setToast({
          title: "Playlists synced",
          body: `${task.result?.synced || 0} tracks were sent to Jellyfin.`,
        });
      }
      if (task.type === "check_files") {
        if (checkFileTaskIds.current.has(task.id)) return;
        checkFileTaskIds.current.add(task.id);
        if (!emit) return;
        sendFileCheckToImport(task.result || {});
      }
    });
  }

  function sendFileCheckToImport(result) {
    const files = result.missing_records || [];
    const queuedDownloads = result.queued_missing_files || 0;
    const queuedRecords = result.queued_missing_records || 0;
    if (files.length === 0 && queuedDownloads === 0 && queuedRecords === 0) return;
    setImportFiles(files);
    setImportSeedDownloads([]);
    setPage("Task Queue");
    setToast({
      title: "File check ready",
      body: `${queuedDownloads + queuedRecords} fixes were added to the task queue.`,
    });
    refreshApprovals();
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
      const notificationData = await api("/notifications");
      setNotifications((current) => mergeTrayNotifications(notificationData, current));
    } catch {
      // Notification polling is best-effort.
    }
  }

  function notify(title, body, eventType = "ui_notice") {
    const notification = {
      id: `local:${Date.now()}:${localNotificationCounter.current++}`,
      user_id: user?.id || null,
      title,
      body,
      event_type: eventType,
      target_url: null,
      status: "unread",
      deliver_web: true,
      deliver_apns: false,
      created_at: new Date().toISOString(),
    };
    setToast({ title, body });
    setNotifications((current) => [notification, ...current]);
  }

  async function refreshIntegrationSettings() {
    try {
      setIntegrationSettings(await api("/settings/integrations"));
    } catch {
      // Users without settings permissions do not need integration fields.
    }
  }

  async function refreshUsers() {
    try {
      setUsers(await api("/users"));
    } catch {
      // Users without management permissions do not need this list.
    }
  }

  async function refreshUserPlayback() {
    try {
      setUserPlayback(await api("/users/playback"));
    } catch {
      // Playback visibility is only available to users with activity access.
    }
  }

  async function refreshPermissions() {
    try {
      setPermissionCatalog(await api("/permissions"));
    } catch {
      // Users without management permissions do not need this catalog.
    }
  }

  async function createUserAccount(payload) {
    setLoading(true);
    try {
      const created = await api("/users", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setUsers((current) => upsertUser(current, created));
      setToast({ title: "User created", body: created.display_name });
      return created;
    } catch (userError) {
      notify("User failed", userError.message, "ui_error");
      throw userError;
    } finally {
      setLoading(false);
    }
  }

  async function updateUserAccount(userId, payload) {
    setLoading(true);
    try {
      const updated = await api(`/users/${userId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      setUsers((current) => upsertUser(current, updated));
      setToast({ title: "User updated", body: updated.display_name });
      return updated;
    } catch (userError) {
      notify("User failed", userError.message, "ui_error");
      throw userError;
    } finally {
      setLoading(false);
    }
  }

  async function deleteUserAccount(userId) {
    setLoading(true);
    try {
      await api(`/users/${userId}`, { method: "DELETE" });
      setUsers((current) => current.filter((u) => u.id !== userId));
      setToast({ title: "User deleted", body: "Account and its data were removed." });
    } catch (userError) {
      notify("Delete failed", userError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function updateUserPin(userId, password) {
    setLoading(true);
    try {
      const updated = await api(`/users/${userId}/pin`, {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setUsers((current) => upsertUser(current, updated));
      setToast({ title: "Password updated", body: updated.display_name });
      return updated;
    } catch (userError) {
      notify("Password update failed", userError.message, "ui_error");
      throw userError;
    } finally {
      setLoading(false);
    }
  }

  async function updateOwnPin(password) {
    setLoading(true);
    try {
      const updated = await api("/me/pin", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setUser(updated);
      setUsers((current) => upsertUser(current, updated));
      setToast({ title: "Password updated", body: updated.display_name });
      return updated;
    } catch (userError) {
      notify("Password update failed", userError.message, "ui_error");
      throw userError;
    } finally {
      setLoading(false);
    }
  }

  async function saveOwnAppearance(appearance) {
    const version = ++appearanceSaveVersion.current;
    try {
      const updated = await api("/me/appearance", {
        method: "PUT",
        body: JSON.stringify(appearance),
      });
      if (version !== appearanceSaveVersion.current) return;
      setUser(updated);
      setUsers((current) => upsertUser(current, updated));
    } catch (appearanceError) {
      notify("Theme sync failed", appearanceError.message, "ui_error");
    }
  }

  async function loadJellyfinUsers() {
    setJellyfinUsersLoading(true);
    try {
      const data = await api("/settings/jellyfin-users");
      setJellyfinUsers(data);
    } catch {
      setJellyfinUsers([]);
    } finally {
      setJellyfinUsersLoading(false);
    }
  }

  async function updateUserJellyfinUser(userId, jellyfinUserId) {
    try {
      const updated = await api(`/users/${userId}/jellyfin-user`, {
        method: "PUT",
        body: JSON.stringify({ jellyfin_user_id: jellyfinUserId }),
      });
      setUsers((current) => upsertUser(current, updated));
      if (updated.id === user?.id) setUser(updated);
      return updated;
    } catch (err) {
      notify("Jellyfin link failed", err.message, "ui_error");
      throw err;
    }
  }

  async function saveIntegrationSettings(settings) {
    setLoading(true);
    try {
      const saved = await api("/settings/integrations", {
        method: "PUT",
        body: JSON.stringify(settings),
      });
      setIntegrationSettings(saved);
      refreshPlaylists();
      setToast({ title: "Settings saved", body: "Integration settings were updated." });
    } catch (settingsError) {
      notify("Settings failed", settingsError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function refreshPlaylists() {
    try {
      const playlistData = await api("/playlists");
      setPlaylists(playlistData);
      setFavoriteTrackIds(new Set(favoritePlaylistFrom(playlistData)?.track_ids || []));
    } catch {
      // Playlists are optional for users without playlist permissions.
    }
  }

  async function createPlaylist(name) {
    setLoading(true);
    try {
      const playlist = await api("/playlists", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setPlaylists((current) => upsertPlaylist(current, playlist));
      if (playlist.protected) {
        setFavoriteTrackIds(new Set(playlist.track_ids || []));
      }
      setToast({ title: "Playlist created", body: playlist.name });
      return playlist;
    } catch (playlistError) {
      notify("Playlist failed", playlistError.message, "ui_error");
      throw playlistError;
    } finally {
      setLoading(false);
    }
  }

  async function addTracksToPlaylist(playlistId, trackIds) {
    if (!playlistId || trackIds.length === 0) return null;
    setLoading(true);
    try {
      const playlist = await api(`/playlists/${playlistId}/tracks`, {
        method: "POST",
        body: JSON.stringify({ track_ids: trackIds }),
      });
      setPlaylists((current) => upsertPlaylist(current, playlist));
      if (playlist.protected) {
        setFavoriteTrackIds(new Set(playlist.track_ids || []));
      }
      setToast({ title: "Playlist updated", body: `${trackIds.length} item${trackIds.length === 1 ? "" : "s"} added to ${playlist.name}.` });
      return playlist;
    } catch (playlistError) {
      notify("Playlist failed", playlistError.message, "ui_error");
      throw playlistError;
    } finally {
      setLoading(false);
    }
  }

  async function toggleFavoriteTrack(track) {
    if (!track?.id) return;
    const wasFavorite = favoriteTrackIds.has(track.id);
    try {
      let favorites = favoritePlaylistFrom(playlists);
      if (!favorites) {
        const playlistData = await api("/playlists");
        setPlaylists(playlistData);
        favorites = favoritePlaylistFrom(playlistData);
      }
      if (!favorites) throw new Error("Favorites playlist is not available");
      const updatedFavorites = wasFavorite
        ? await api(`/playlists/${favorites.id}/tracks/${track.id}`, { method: "DELETE" })
        : await api(`/playlists/${favorites.id}/tracks`, {
            method: "POST",
            body: JSON.stringify({ track_ids: [track.id] }),
          });
      setPlaylists((current) => upsertPlaylist(current, updatedFavorites));
      setFavoriteTrackIds(new Set(updatedFavorites.track_ids || []));
      setToast({
        title: wasFavorite ? "Removed from Favorites" : "Added to Favorites",
        body: track._artist ? `${track.title} by ${track._artist}` : track.title,
      });
    } catch (favoriteError) {
      notify("Favorite failed", favoriteError.message, "ui_error");
    }
  }

  async function openNotificationTray() {
    const nextOpen = !trayOpen;
    setTrayOpen(nextOpen);
    if (!nextOpen || unreadNotifications.length === 0) return;
    setNotifications((current) => current.map((notification) => ({ ...notification, status: "read" })));
    try {
      await api("/notifications/read", { method: "POST" });
      await refreshNotifications();
    } catch {
      // The tray can still behave locally if marking read fails.
    }
  }

  async function clearNotifications() {
    setNotifications([]);
    try {
      await api("/notifications", { method: "DELETE" });
    } catch (clearError) {
      notify("Notifications failed", clearError.message, "ui_error");
    }
  }

  async function refreshApprovals() {
    try {
      setApprovals(await api("/approvals"));
    } catch {
      // Approval polling is best-effort.
    }
  }

  async function refreshWishlistApprovals() {
    try {
      setWishlistApprovals(await api("/wishlist/approvals"));
    } catch {
      // Wishlist approval polling is best-effort.
    }
  }

  async function refreshWishlist() {
    try {
      setWishlist(await api("/wishlist"));
    } catch {
      // Wishlist status polling is best-effort.
    }
  }

  async function createWishlistItem(item) {
    setLoading(true);
    try {
      const created = await api("/wishlist", {
        method: "POST",
        body: JSON.stringify(item),
      });
      setWishlist((current) => [created, ...current.filter((wishlistItem) => wishlistItem.id !== created.id)]);
      setToast({ title: "Wishlist updated", body: "The item was added to the wishlist." });
      return created;
    } catch (wishlistError) {
      notify("Wishlist failed", wishlistError.message, "ui_error");
      throw wishlistError;
    } finally {
      setLoading(false);
    }
  }

  async function removeWishlistItem(itemId) {
    return removeWishlistItems([itemId]);
  }

  async function removeWishlistItems(itemIds) {
    setLoading(true);
    try {
      const updatedItems = [];
      for (const itemId of itemIds) {
        updatedItems.push(await api(`/wishlist/${itemId}`, { method: "DELETE" }));
      }
      const updatedIds = new Set(updatedItems.map((item) => item.id));
      setWishlist((current) => current.filter((item) => !updatedIds.has(item.id)));
      setToast({ title: "Wishlist updated", body: `${updatedItems.length} item${updatedItems.length === 1 ? "" : "s"} removed.` });
      return updatedItems;
    } catch (wishlistError) {
      notify("Wishlist failed", wishlistError.message, "ui_error");
      throw wishlistError;
    } finally {
      setLoading(false);
    }
  }

  async function submitWishlistApprovals(itemIds = null, options = {}) {
    setLoading(true);
    try {
      const wantedItems = itemIds?.length ? wishlist.filter((item) => itemIds.includes(item.id)) : wishlist.filter((item) => item.status === "wanted");
      const batch = await api("/wishlist/approvals", {
        method: "POST",
        body: JSON.stringify({ item_ids: itemIds?.length ? itemIds : null, deny_unselected: Boolean(options.denyUnselected) }),
      });
      setWishlistApprovals((current) => [batch, ...current.filter((item) => item.id !== batch.id)]);
      await refreshApprovals();
      const wishlistData = await api("/wishlist");
      setWishlist(wishlistData);
      setToast({ title: "Wishlist review queued", body: `${wantedItems.length} wishlist items were submitted.` });
      return batch;
    } catch (wishlistError) {
      notify("Wishlist review failed", wishlistError.message, "ui_error");
      throw wishlistError;
    } finally {
      setLoading(false);
    }
  }

  async function searchDiscover(query) {
    return api(`/discover/search?q=${encodeURIComponent(query)}`);
  }

  async function fetchDiscoverAlbumTracks(albumId) {
    return api(`/discover/album-tracks/${encodeURIComponent(albumId)}`);
  }

  async function queueDiscoverDownloads(downloadRequests) {
    setLoading(true);
    try {
      const task = await api("/discover/task-queue", {
        method: "POST",
        body: JSON.stringify({ download_requests: downloadRequests }),
      });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Added to task queue", body: `${downloadRequests.length} download request${downloadRequests.length === 1 ? "" : "s"} queued.` });
      window.setTimeout(() => {
        refreshApprovals();
        refreshTasks();
      }, 2500);
      return task;
    } catch (discoverError) {
      notify("Discover failed", discoverError.message, "ui_error");
      throw discoverError;
    } finally {
      setLoading(false);
    }
  }

  async function scanImportFolder() {
    setLoading(true);
    try {
      setImportDownloadRequests([]);
      setImportSeedDownloads([]);
      const data = await api("/imports/scan", {
        method: "POST",
        body: JSON.stringify({ path: null }),
      });
      setImportFiles(data.files);
      setToast({ title: "Import scan complete", body: `${data.count} audio files found.` });
    } catch (scanError) {
      notify("Import scan failed", scanError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function proposeImport(downloadRequests = []) {
    setLoading(true);
    try {
      const task = await api("/imports/propose", {
        method: "POST",
        body: JSON.stringify({
          path: null,
          files: importFiles,
          download_requests: downloadRequests,
          playlist_name: pendingPlaylistName || null,
          playlist_original_tracks: pendingPlaylistOriginalTracks || null,
          playlist_origin: pendingPlaylistOrigin || null,
        }),
      });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Import review queued", body: "A review item was added to the task queue." });
      setImportFiles([]);
      setImportDownloadRequests([]);
      setImportSeedDownloads([]);
      setPendingPlaylistName(null);
      setPendingPlaylistOriginalTracks(null);
      setPendingPlaylistOrigin(null);
      setPage("Task Queue");
      window.setTimeout(() => {
        refreshApprovals();
        refreshTasks();
      }, 2500);
    } catch (proposeError) {
      notify("Import review failed", proposeError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function recheckImportTrack(file) {
    setLoading(true);
    try {
      const data = await api("/imports/musicbrainz-match", {
        method: "POST",
        body: JSON.stringify({ file }),
      });
      const candidate = data.candidates?.[0];
      if (!candidate) {
        setToast({ title: "No metadata match", body: "No MusicBrainz match was found for this track." });
        return;
      }
      const metadataPatch = compactMetadata(candidate.metadata || {});
      setImportFiles((current) => patchImportFile(current, file.path, metadataPatch));
      setToast({ title: "Metadata updated", body: "The most likely MusicBrainz match was applied." });
    } catch (lookupError) {
      notify("Metadata lookup failed", lookupError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function recheckImportAlbum(album) {
    const albumFiles = album.files || [];
    if (albumFiles.length === 0) return;
    setLoading(true);
    let nextFiles = importFiles;
    let matched = 0;
    let changed = 0;
    let missing = 0;
    let failed = 0;
    try {
      for (const file of albumFiles) {
        try {
          const data = await api("/imports/musicbrainz-match", {
            method: "POST",
            body: JSON.stringify({ file }),
          });
          const candidate = data.candidates?.[0];
          if (!candidate) {
            missing += 1;
            nextFiles = patchImportFile(nextFiles, file.path, { musicbrainz_match: "no match" });
            continue;
          }
          const metadataPatch = compactMetadata(candidate.metadata || {});
          const oldTitle = file.metadata?.title || "";
          const nextTitle = metadataPatch.title || oldTitle;
          const matchStatus = normalizeName(oldTitle) === normalizeName(nextTitle) ? "matched" : "changed";
          if (matchStatus === "matched") matched += 1;
          else changed += 1;
          nextFiles = patchImportFile(nextFiles, file.path, {
            ...metadataPatch,
            musicbrainz_match: matchStatus,
            musicbrainz_score: Math.round((candidate.score || 0) * 100),
          });
        } catch {
          failed += 1;
        }
      }
      setImportFiles(nextFiles);
      setToast({
        title: "Album MusicBrainz check complete",
        body: `${matched} matched. ${changed} updated. ${missing} unmatched. ${failed} failed.`,
      });
    } finally {
      setLoading(false);
    }
  }

  async function checkLibraryTrackAudio(track) {
    try {
      const result = await api(`/library/tracks/${track.id}/verify-audio`, { method: "POST" });
      // Result goes to the Activity log + notifications (created server-side); show a
      // transient toast for immediate feedback — never rendered inline in the tree.
      if (result) setToast({ title: "Audio check complete", body: result.message });
      return null;
    } catch (verifyError) {
      notify("Audio verification failed", verifyError.message, "ui_error");
      return null;
    }
  }

  // Manually queue a replacement download for a track/album (e.g. swap a clean version for
  // the explicit one). Kicks off a candidate search; the actual swap happens after the user
  // approves a candidate in the Task Queue (replace_track_id → replace_library_track_file).
  async function requeueTrackReplacement(track) {
    try {
      const task = await api(`/library/tracks/${track.id}/replace`, { method: "POST" });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Replacement queued", body: `Searching for a replacement of "${track.title}" — review candidates in the Task Queue.` });
      window.setTimeout(() => { refreshApprovals(); refreshTasks(); }, 2500);
    } catch (replaceError) {
      notify("Replacement failed", replaceError.message, "ui_error");
    }
  }

  async function requeueAlbumReplacement(album) {
    try {
      const task = await api(`/library/albums/${album.id}/replace`, { method: "POST" });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Replacement queued", body: `Searching for replacements for "${album.title}" — review candidates in the Task Queue.` });
      window.setTimeout(() => { refreshApprovals(); refreshTasks(); }, 2500);
    } catch (replaceError) {
      notify("Replacement failed", replaceError.message, "ui_error");
    }
  }

  async function lookupImportAlbum(artist, album, releaseId = null) {
    setLoading(true);
    try {
      const data = await api("/imports/album-lookup", {
        method: "POST",
        body: JSON.stringify({ artist, album, release_id: releaseId }),
      });
      setToast({ title: "Album checked", body: `${data.tracks?.length || 0} tracks found.` });
      return data;
    } catch (lookupError) {
      notify("Album lookup failed", lookupError.message, "ui_error");
      return null;
    } finally {
      setLoading(false);
    }
  }

  async function searchImportAlbums(artist, album) {
    setLoading(true);
    try {
      const data = await api("/imports/album-search", {
        method: "POST",
        body: JSON.stringify({ artist, album }),
      });
      return data.results || [];
    } catch (lookupError) {
      notify("Album search failed", lookupError.message, "ui_error");
      return [];
    } finally {
      setLoading(false);
    }
  }

  async function searchAlbumCover(albumId) {
    setLoading(true);
    try {
      const data = await api(`/library/albums/${albumId}/cover-candidates`);
      if (!data.cover_path) {
        notify("No cover art found", "No album art source matched this album.", "ui_error");
      }
      return data;
    } catch (coverError) {
      notify("Cover search failed", coverError.message, "ui_error");
      return null;
    } finally {
      setLoading(false);
    }
  }

  async function searchArtistCover(artistId) {
    setLoading(true);
    try {
      const data = await api(`/library/artists/${artistId}/cover-candidates`);
      if (!data.cover_path) {
        notify("No artist art found", "No artist image source matched.", "ui_error");
      }
      return data;
    } catch (coverError) {
      notify("Cover search failed", coverError.message, "ui_error");
      return null;
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!token) return;
    api("/me/pinned-albums").then((rows) => setPinnedAlbumIds(new Set((rows || []).map((r) => r.album_id)))).catch(() => {});
    api("/me/pinned-artists").then((rows) => setPinnedArtistIds(new Set((rows || []).map((r) => r.artist_id)))).catch(() => {});
  }, [token]);

  async function toggleAlbumPin(album) {
    const pinned = pinnedAlbumIds.has(album.id);
    try {
      const rows = pinned
        ? await api(`/me/pinned-albums/${encodeURIComponent(album.id)}`, { method: "DELETE" })
        : await api("/me/pinned-albums", { method: "POST", body: JSON.stringify({ album_id: album.id }) });
      setPinnedAlbumIds(new Set((rows || []).map((r) => r.album_id)));
      setHomeVersion((v) => v + 1);
    } catch (pinError) {
      notify("Pin failed", pinError.message, "ui_error");
    }
  }

  async function toggleArtistPin(artist) {
    const pinned = pinnedArtistIds.has(artist.id);
    try {
      const rows = pinned
        ? await api(`/me/pinned-artists/${encodeURIComponent(artist.id)}`, { method: "DELETE" })
        : await api("/me/pinned-artists", { method: "POST", body: JSON.stringify({ artist_id: artist.id }) });
      setPinnedArtistIds(new Set((rows || []).map((r) => r.artist_id)));
      setHomeVersion((v) => v + 1);
    } catch (pinError) {
      notify("Pin failed", pinError.message, "ui_error");
    }
  }

  async function unpinPlaylist(playlistId) {
    try {
      await api(`/me/pinned-playlists/${encodeURIComponent(playlistId)}`, { method: "DELETE" });
      setHomeVersion((v) => v + 1);
    } catch (pinError) {
      notify("Unpin failed", pinError.message, "ui_error");
    }
  }

  async function applyLibraryMetadata(targetType, targetId, changes) {
    // Library metadata edits apply directly on field blur (no review queue).
    setLoading(true);
    try {
      const result = await api("/library/metadata/apply", {
        method: "POST",
        body: JSON.stringify({ target_type: targetType, target_id: targetId, changes }),
      });
      await refreshLibrary();
      return result;
    } catch (metadataError) {
      notify("Metadata change failed", metadataError.message, "ui_error");
      throw metadataError;
    } finally {
      setLoading(false);
    }
  }

  async function proposeLibraryRemove(targetType, targetId, action) {
    setLoading(true);
    try {
      const batch = await api("/library/remove", {
        method: "POST",
        body: JSON.stringify({ target_type: targetType, target_id: targetId, action }),
      });
      setApprovals((current) => [batch, ...current.filter((entry) => entry.id !== batch.id)]);
      setToast({ title: "Library change queued", body: "The removal request was added to the task queue." });
      return batch;
    } catch (removeError) {
      notify("Queue request failed", removeError.message, "ui_error");
      throw removeError;
    } finally {
      setLoading(false);
    }
  }

  async function searchLibrary(q, minConfidence) {
    const params = new URLSearchParams({ q });
    if (minConfidence != null) params.set("min_confidence", String(minConfidence));
    const data = await api(`/library/search?${params.toString()}`);
    return data?.results || [];
  }

  async function saveSearchThreshold(value) {
    try {
      const updated = await api("/me/search-settings", { method: "PUT", body: JSON.stringify({ min_confidence: value }) });
      setUser((prev) => (prev ? { ...prev, search_min_confidence: updated.search_min_confidence } : updated));
    } catch (e) {
      notify("Could not save search threshold", e.message, "ui_error");
    }
  }

  async function saveLibraryPageSize(value) {
    try {
      const updated = await api("/me/search-settings", { method: "PUT", body: JSON.stringify({ page_size: value }) });
      setUser((prev) => (prev ? { ...prev, library_page_size: updated.library_page_size } : updated));
    } catch (e) {
      notify("Could not save page size", e.message, "ui_error");
    }
  }

  async function proposePlaylistPosition(entryId, position) {
    setLoading(true);
    try {
      const batch = await api(`/playlists/entries/${entryId}/position`, {
        method: "POST",
        body: JSON.stringify({ position }),
      });
      setApprovals((current) => [batch, ...current.filter((entry) => entry.id !== batch.id)]);
      setToast({ title: "Playlist change queued", body: "The order change was added to the task queue." });
      return batch;
    } catch (playlistError) {
      notify("Playlist queue failed", playlistError.message, "ui_error");
      throw playlistError;
    } finally {
      setLoading(false);
    }
  }

  async function renamePlaylist(playlistId, name) {
    setLoading(true);
    try {
      const playlist = await api(`/playlists/${playlistId}`, { method: "PATCH", body: JSON.stringify({ name }) });
      await refreshPlaylists();
      setToast({ title: "Playlist renamed", body: playlist.name });
      return playlist;
    } catch (playlistError) {
      notify("Rename failed", playlistError.message, "ui_error");
      throw playlistError;
    } finally {
      setLoading(false);
    }
  }

  async function deletePlaylist(playlistId) {
    setLoading(true);
    try {
      await api(`/playlists/${playlistId}`, { method: "DELETE" });
      await refreshPlaylists();
      setToast({ title: "Playlist deleted" });
    } catch (playlistError) {
      notify("Delete failed", playlistError.message, "ui_error");
      throw playlistError;
    } finally {
      setLoading(false);
    }
  }


  async function importPlaylist(url, mode) {
    setPlaylistImportLoading(true);
    try {
      const data = await api("/imports/playlist-url", { method: "POST", body: JSON.stringify({ url }) });
      const { tracks, name: playlistName } = data;
      const originalTracks = tracks.map((t) => ({ artist: t.artist, title: t.title }));

      function dedup(incoming, prev) {
        const existing = new Set(
          prev.map((r) => `${(r.artist || "").toLowerCase()}::${(r.album || "").toLowerCase()}::${(r.track || r.title || "").toLowerCase()}`)
        );
        return incoming.filter((r) => !existing.has(
          `${(r.artist || "").toLowerCase()}::${(r.album || "").toLowerCase()}::${(r.track || r.title || "").toLowerCase()}`
        ));
      }

      function addToTree(incoming) {
        const albums = manualAlbumsFromDownloadRequests(incoming);
        if (addImportAlbumsRef.current) {
          addImportAlbumsRef.current(albums);
        } else {
          // Fallback: wizard not mounted yet, use seed state
          setImportSeedDownloads((prev) => { const next = dedup(incoming, prev); return [...prev, ...next]; });
        }
      }

      if (mode === "songs") {
        const incoming = tracks.map((t) => {
          // When Spotify returns the track's own name as the album (single release),
          // strip the album so it groups under "Singles" instead of creating a
          // redundant artist → "Track Name" album → "Track Name" track hierarchy.
          const album = t.album && normalizeName(t.album) !== normalizeName(t.title) ? t.album : "";
          return { artist: t.artist, album, track: t.title, playlist_name: playlistName };
        });
        addToTree(incoming);
        setPendingPlaylistName(playlistName);
        setPendingPlaylistOriginalTracks(originalTracks);
        setPendingPlaylistOrigin(url);
        setPlaylistImportUrl("");
        setToast({ title: "Added to import", body: `${tracks.length} track${tracks.length === 1 ? "" : "s"} added to the import tree.` });
      } else {
        // Albums mode: group by unique artist+album, look up full tracklist from MusicBrainz for each.
        // Tracks with no album and fallback tracks (lookup failure or missing from album) get
        // added with album "" so the backend uses a track-level search instead of album-folder search.
        const seen = new Map();
        const singleTracks = []; // playlist tracks that have no album info
        for (const t of tracks) {
          if (!t.album) {
            singleTracks.push(t);
            continue;
          }
          const key = `${(t.artist || "").toLowerCase()}::${t.album.toLowerCase()}`;
          if (!seen.has(key)) seen.set(key, { artist: t.artist, albumHint: t.album, playlistTracks: [] });
          seen.get(key).playlistTracks.push(t);
        }
        const allIncoming = [];
        // No-album tracks → individual search
        for (const t of singleTracks) {
          allIncoming.push({ artist: t.artist, album: "", track: t.title, playlist_name: playlistName });
        }
        let albumsResolved = 0;
        for (const { artist, albumHint, playlistTracks } of seen.values()) {
          const albumData = await lookupImportAlbum(artist, albumHint);
          if (!albumData || !(albumData.tracks || []).length) {
            // Lookup failed → fall back to individual track searches
            for (const t of playlistTracks) {
              allIncoming.push({ artist: t.artist, album: "", track: t.title, playlist_name: playlistName });
            }
            continue;
          }
          albumsResolved++;
          // Add every track from the looked-up album
          const albumTrackTitles = new Set(albumData.tracks.map((t) => normalizeName(t.title)));
          albumData.tracks.forEach((track) => {
            allIncoming.push({ artist: albumData.artist || artist, album: albumData.album || albumHint, track: track.title, track_number: track.track_number, disc_number: track.disc_number, playlist_name: playlistName });
          });
          // Playlist tracks not found in the album → individual fallback search
          for (const pt of playlistTracks) {
            if (!albumTrackTitles.has(normalizeName(pt.title))) {
              allIncoming.push({ artist: pt.artist, album: "", track: pt.title, playlist_name: playlistName });
            }
          }
        }
        addToTree(allIncoming);
        setPendingPlaylistName(playlistName);
        setPendingPlaylistOriginalTracks(originalTracks);
        setPendingPlaylistOrigin(url);
        setPlaylistImportUrl("");
        setToast({ title: "Added to import", body: `${allIncoming.length} track${allIncoming.length === 1 ? "" : "s"} from ${albumsResolved} album${albumsResolved === 1 ? "" : "s"} added to the import tree.` });
      }
    } catch (err) {
      notify("Playlist import failed", err.message || "Failed to fetch playlist.", "ui_error");
    } finally {
      setPlaylistImportLoading(false);
    }
  }

  async function runTool(action, payload = null) {
    setLoading(true);
    try {
      const task = await api(`/tools/${action}`, {
        method: "POST",
        ...(payload ? { body: JSON.stringify(payload) } : {}),
      });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Tool queued", body: task.type });
      if (action === "backup") {
        window.setTimeout(() => api("/tools/backups").then((data) => setBackups(data.backups || [])).catch(() => {}), 2500);
      }
      return task;
    } catch (toolError) {
      notify("Tool failed", toolError.message, "ui_error");
      throw toolError;
    } finally {
      setLoading(false);
    }
  }

  async function proposeCheckFileFix(fix) {
    setLoading(true);
    try {
      const batch = await api("/tools/check-files/fix", {
        method: "POST",
        body: JSON.stringify(fix),
      });
      setApprovals((current) => [batch, ...current.filter((entry) => entry.id !== batch.id)]);
      setToast({ title: "File fix queued", body: "The fix was added to the task queue." });
      return batch;
    } catch (fixError) {
      notify("File fix failed", fixError.message, "ui_error");
      throw fixError;
    } finally {
      setLoading(false);
    }
  }

  async function uploadYoutubeCookies(browser, file) {
    if (!file) return null;
    setLoading(true);
    const body = new FormData();
    body.append("file", file);
    try {
      const saved = await api(`/settings/youtube-cookies?browser=${encodeURIComponent(browser || "")}`, {
        method: "POST",
        body,
      });
      setIntegrationSettings(saved);
      setToast({ title: "Cookies uploaded", body: file.name });
      return saved;
    } catch (uploadError) {
      notify("Cookie upload failed", uploadError.message, "ui_error");
      throw uploadError;
    } finally {
      setLoading(false);
    }
  }

  async function uploadArtistCover(artistId, file) {
    if (!file) return;
    setLoading(true);
    const body = new FormData();
    body.append("file", file);
    try {
      await api(`/library/artists/${artistId}/cover`, { method: "POST", body });
      coverCacheBust = Date.now();
      await refreshLibrary();
      setToast({ title: "Artist art updated", body: file.name });
    } catch (uploadError) {
      notify("Artist art upload failed", uploadError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function uploadAlbumCover(albumId, file) {
    if (!file) return;
    setLoading(true);
    const body = new FormData();
    body.append("file", file);
    try {
      await api(`/library/albums/${albumId}/cover`, { method: "POST", body });
      coverCacheBust = Date.now();
      await refreshLibrary();
      setToast({ title: "Cover art updated", body: file.name });
    } catch (uploadError) {
      notify("Cover art upload failed", uploadError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function uploadImportFiles(items) {
    // Accept plain File objects (file picker) or { file, path } entries (folder
    // picker / drag-drop). A relative path keeps a dropped folder's structure.
    const relPath = (it) => (it.path || it.file.webkitRelativePath || it.file.name || "").replace(/\\/g, "/").replace(/^\/+/, "");
    let list = Array.from(items || [])
      .map((it) => (it instanceof File ? { file: it, path: it.webkitRelativePath || it.name } : it))
      .filter((it) => it && it.file);
    if (!list.length) return;
    // Skip files that already exist in the import folder — don't re-send them.
    let skipped = 0;
    try {
      const existing = await api("/imports/existing");
      const have = new Set((existing?.names || []).map((n) => n.toLowerCase()));
      const before = list.length;
      list = list.filter((it) => !have.has(relPath(it).toLowerCase()));
      skipped = before - list.length;
    } catch {
      /* couldn't list the folder — fall back to uploading all (server de-dupes names too) */
    }
    if (!list.length) {
      setToast({ title: "Nothing to upload", body: skipped ? `${skipped} file${skipped === 1 ? "" : "s"} already in the import folder.` : "No files selected." });
      return;
    }
    setLoading(true);
    setImportUploadProgress(0);
    // Split into multiple POSTs so no single request exceeds the upstream proxy's
    // body cap (Cloudflare's free tier hard-limits requests to 100 MB — a whole
    // album folder bundled into one request 413s; a few hand-picked files don't).
    // The endpoint de-dupes by name, so sequential batches are safe.
    const MAX_UPLOAD_BYTES = 90 * 1024 * 1024;
    const batches = [];
    let current = [];
    let currentBytes = 0;
    for (const it of list) {
      const size = it.file.size || 0;
      if (current.length && currentBytes + size > MAX_UPLOAD_BYTES) {
        batches.push(current);
        current = [];
        currentBytes = 0;
      }
      current.push(it);
      currentBytes += size;
    }
    if (current.length) batches.push(current);
    const totalBytes = list.reduce((sum, it) => sum + (it.file.size || 0), 0) || 1;
    let uploadedBytes = 0;
    let uploadedCount = 0;
    let rejectedCount = 0;
    try {
      for (const batch of batches) {
        const body = new FormData();
        batch.forEach((it) => { body.append("files", it.file); body.append("paths", relPath(it)); });
        const batchBytes = batch.reduce((sum, it) => sum + (it.file.size || 0), 0);
        // XHR (not fetch) so we get real upload-progress events per batch.
        const res = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          importUploadXhrRef.current = xhr;
          xhr.open("POST", `${API_BASE}/imports/upload`);
          xhr.setRequestHeader("Authorization", `Bearer ${token}`);
          xhr.upload.onprogress = (event) => {
            if (event.lengthComputable) setImportUploadProgress((uploadedBytes + event.loaded) / totalBytes);
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({}); }
            } else {
              let detail = `${xhr.status} ${xhr.statusText}`;
              try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* keep status */ }
              reject(new Error(detail));
            }
          };
          xhr.onerror = () => reject(new Error("Upload failed"));
          xhr.onabort = () => reject(new Error("__canceled__"));
          xhr.send(body);
        });
        uploadedBytes += batchBytes;
        uploadedCount += res.count ?? 0;
        rejectedCount += res.rejected?.length ?? 0;
        setImportUploadProgress(uploadedBytes / totalBytes);
      }
      const parts = [`${uploadedCount} uploaded`];
      if (skipped) parts.push(`${skipped} already present`);
      if (rejectedCount) parts.push(`${rejectedCount} rejected`);
      setToast({ title: "Upload complete", body: parts.join(", ") });
      await scanImportFolder();
    } catch (uploadError) {
      if (uploadError.message === "__canceled__") {
        setToast({ title: "Upload canceled", body: uploadedCount ? `${uploadedCount} uploaded before cancel.` : "" });
        if (uploadedCount) await scanImportFolder();
      } else {
        notify("Import upload failed", uploadError.message, "ui_error");
      }
    } finally {
      importUploadXhrRef.current = null;
      setImportUploadProgress(null);
      setLoading(false);
    }
  }

  function cancelImportUpload() {
    importUploadXhrRef.current?.abort();
  }

  async function clearImportFolder() {
    setLoading(true);
    try {
      const res = await api("/imports/files", { method: "DELETE" });
      const removed = res?.removed ?? 0;
      setToast({ title: "Import folder cleared", body: `${removed} file${removed === 1 ? "" : "s"} removed.` });
      await scanImportFolder();
    } catch (clearError) {
      notify("Clear import folder failed", clearError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function playTracks(tracks, opts = {}) {
    const playable = tracks.filter((track) => track?.id);
    if (playable.length === 0) return;
    // Honor shuffle when starting a new queue: an explicit opts.shuffle (remote
    // command) wins, otherwise the current shuffle toggle decides. A shuffled
    // start reorders the new queue and remembers the original order so toggling
    // shuffle back off can revert it — identical to the in-place toggle path.
    const wantShuffle = opts.shuffle != null ? Boolean(opts.shuffle) : shuffle;
    // keepLead (default true): keep the first track playing first — correct when the
    // user clicked a specific song. Whole-collection plays (Shuffle all / play an
    // album/artist/playlist) pass keepLead:false so the entire list is shuffled and
    // each start picks a fresh random order, including a random first track.
    const keepLead = opts.keepLead !== false;
    let queue = playable;
    if (wantShuffle && playable.length > 1) {
      unshuffledQueueRef.current = [...playable];
      const head = keepLead ? [playable[0]] : [];
      const rest = keepLead ? playable.slice(1) : [...playable];
      for (let i = rest.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [rest[i], rest[j]] = [rest[j], rest[i]];
      }
      queue = [...head, ...rest];
    } else {
      unshuffledQueueRef.current = null;
    }
    if (opts.shuffle != null) setShuffle(Boolean(opts.shuffle));
    setPlayerQueue(queue);
    setPlayerOpen(true);
    setQueueOpen(false);
    await loadPlayerTrack(queue[0]);
  }

  function resolvePlayableFromLibrary(targetType, targetId) {
    const out = [];
    for (const artist of library || []) {
      for (const album of artist.albums || []) {
        for (const track of album.tracks || []) {
          const match =
            (targetType === "track" && track.id === targetId) ||
            (targetType === "album" && album.id === targetId) ||
            (targetType === "artist" && artist.id === targetId);
          if (match) out.push({ id: track.id, title: track.title, album_id: album.id, _artist: artist.name, _album: album.title });
        }
      }
    }
    return out;
  }

  async function resolvePlaylistTracks(playlistId) {
    try {
      const pl = playlistId === "favorites" ? await api("/playlists/favorites") : (await api("/playlists")).find((p) => p.id === playlistId);
      return (pl?.tracks || []).map((t) => ({ id: t.track_id, title: t.title, album_id: t.album_id, _artist: t.artist, _album: t.album }));
    } catch {
      return [];
    }
  }

  // Build playable track objects for an album, hydrating each with the album's
  // cover URL so the player art shows regardless of which tab launched playback.
  async function loadAlbumPlayables(album) {
    const albumId = typeof album === "string" ? album : album?.id;
    if (!albumId) return [];
    const data = await api(`/library/tracks?album_id=${encodeURIComponent(albumId)}&page_size=500`);
    const coverUrl =
      typeof album === "object" && album
        ? albumCoverUrl(album, token)
        : `${API_BASE}/library/albums/${encodeURIComponent(albumId)}/cover?api_key=${encodeURIComponent(token)}`;
    return (data?.items || []).map((t) => ({
      id: t.id,
      title: t.title,
      _artist: t.artist_name,
      _album: t.album_title,
      album_id: t.album_id,
      _coverUrl: coverUrl || undefined,
    }));
  }

  async function playAlbumFromHome(album) {
    try {
      const tracks = await loadAlbumPlayables(album);
      if (tracks.length === 0) { notify("Playback", "No tracks found for this album.", "ui_error"); return; }
      await playTracks(tracks, { keepLead: false });
    } catch (error) {
      notify("Playback failed", error.message, "ui_error");
    }
  }

  async function queueAlbumFromHome(album) {
    try {
      const tracks = await loadAlbumPlayables(album);
      if (tracks.length === 0) { notify("Queue", "No tracks found for this album.", "ui_error"); return; }
      addTracksToPlayerQueue(tracks);
    } catch (error) {
      notify("Queue failed", error.message, "ui_error");
    }
  }

  function openAlbumDetail(album, origin) {
    if (!album?.id) return;
    setArtistDetail(null);
    setAlbumDetail({
      id: album.id,
      title: album.title,
      artist_name: album.artist || album.artist_name || "",
      artist_id: album.artist_id,
      cover_path: album.cover_path,
      origin: origin || page,
    });
  }

  function closeAlbumDetail() {
    if (albumDetail?.origin) setPage(albumDetail.origin);
    setAlbumDetail(null);
  }

  function openArtistDetail(artist, origin) {
    if (!artist?.id) return;
    setAlbumDetail(null);
    setArtistDetail({
      id: artist.id,
      name: artist.name,
      cover_path: artist.cover_path,
      origin: origin || page,
    });
  }

  function closeArtistDetail() {
    if (artistDetail?.origin) setPage(artistDetail.origin);
    setArtistDetail(null);
  }

  async function playArtistFromHome(artist) {
    try {
      const data = await api(`/library/albums?artist_id=${encodeURIComponent(artist.id)}&page_size=500`);
      let tracks = [];
      for (const al of data?.items || []) {
        tracks = tracks.concat(await loadAlbumPlayables(al));
      }
      if (tracks.length === 0) { notify("Playback", "No tracks found for this artist.", "ui_error"); return; }
      await playTracks(tracks, { keepLead: false });
    } catch (error) {
      notify("Playback failed", error.message, "ui_error");
    }
  }

  async function queueArtistFromHome(artist) {
    try {
      const data = await api(`/library/albums?artist_id=${encodeURIComponent(artist.id)}&page_size=500`);
      let tracks = [];
      for (const al of data?.items || []) tracks = tracks.concat(await loadAlbumPlayables(al));
      if (tracks.length === 0) { notify("Queue", "No tracks found for this artist.", "ui_error"); return; }
      addTracksToPlayerQueue(tracks);
    } catch (error) {
      notify("Queue failed", error.message, "ui_error");
    }
  }

  async function playPlaylistFromHome(playlistId) {
    try {
      const tracks = await resolvePlaylistTracks(playlistId);
      if (tracks.length === 0) { notify("Playback", "This playlist has no tracks.", "ui_error"); return; }
      await playTracks(tracks, { keepLead: false });
    } catch (error) {
      notify("Playback failed", error.message, "ui_error");
    }
  }

  async function playAllLibrary(shuffleAll = false) {
    try {
      const tracks = [];
      const pageSize = 500;
      let page = 1;
      let total = Infinity;
      while (tracks.length < total) {
        const data = await api(`/library/tracks?bucket=all&page=${page}&page_size=${pageSize}`);
        total = data.total ?? 0;
        for (const t of data.items || []) {
          tracks.push({ id: t.id, title: t.title, album_id: t.album_id, _artist: t.artist_name, _album: t.album_title });
        }
        if (!data.items || data.items.length === 0) break;
        page += 1;
      }
      if (tracks.length === 0) { notify("Playback", "Your library has no tracks.", "ui_error"); return; }
      await playTracks(tracks, { shuffle: shuffleAll, keepLead: false });
    } catch (error) {
      notify("Playback failed", error.message, "ui_error");
    }
  }

  async function executeRemoteCommand(cmd) {
    const action = (cmd.action || "").toLowerCase();
    const ctl = playbackControlRef.current;
    // Automations/remote commands share the player's shuffle/repeat state.
    if (cmd.loop != null) {
      setRepeat(cmd.loop === true || cmd.loop === "all" ? "all" : cmd.loop === "one" ? "one" : "off");
    }
    const isPlay = action === "play" || (action === "resume" && cmd.target_id);
    // Reconcile shuffle the same way the UI toggle does. For play/resume-with-target
    // the new queue is (re)built, so playTracks owns the shuffle decision; for every
    // other action we reorder the *existing* queue in place via setShuffleState.
    if (cmd.shuffle != null && !isPlay) setShuffleState(Boolean(cmd.shuffle));
    if (action === "pause") return ctl?.pause?.();
    if (action === "stop") return ctl?.stop?.();
    if (action === "next") return playNextTrack();
    if (action === "previous") return playPreviousTrack();
    if (action === "resume" && !cmd.target_id) return ctl?.resume?.();
    if (isPlay) {
      let tracks = resolvePlayableFromLibrary(cmd.target_type, cmd.target_id);
      if (tracks.length === 0 && cmd.target_type === "playlist") tracks = await resolvePlaylistTracks(cmd.target_id);
      if (tracks.length === 0) {
        notify("Remote playback", `Could not find ${cmd.target_label || "the requested item"} in the library.`, "ui_error");
        return undefined;
      }
      return playTracks(tracks, { shuffle: cmd.shuffle != null ? Boolean(cmd.shuffle) : undefined });
    }
    return undefined;
  }
  remoteExecRef.current = executeRemoteCommand;

  // Identify this client's session so commands can target it specifically.
  useEffect(() => {
    if (!token) return;
    api("/me/sessions")
      .then((rows) => {
        const current = (rows || []).find((s) => s.current);
        if (current) currentSessionIdRef.current = current.id;
      })
      .catch(() => {});
  }, [token]);

  // Consume remote playback commands for this client (broadcast or targeted to this session).
  useEffect(() => {
    if (!token || !user?.id) return undefined;
    let stopped = false;
    async function poll() {
      if (commandPollingRef.current) return; // never let two polls overlap → no double-execution
      commandPollingRef.current = true;
      try {
        const dev = currentSessionIdRef.current;
        const cmds = await api(`/player/commands${dev ? `?device_id=${encodeURIComponent(dev)}` : ""}`);
        for (const cmd of cmds || []) {
          try { await remoteExecRef.current?.(cmd); } catch { /* keep going */ }
          await api(`/player/commands/${cmd.id}/ack`, { method: "POST" }).catch(() => {});
        }
      } catch {
        /* offline / transient */
      } finally {
        commandPollingRef.current = false;
      }
    }
    poll();
    const timer = setInterval(() => { if (!stopped) poll(); }, 4000);
    return () => { stopped = true; clearInterval(timer); };
  }, [token, user?.id]);

  function addTracksToPlayerQueue(tracks) {
    const playable = tracks.filter((track) => track?.id);
    if (playable.length === 0) return;
    const nothingPlaying = !currentTrack;
    setPlayerQueue((current) => [...current, ...playable]);
    setPlayerOpen(true);
    if (nothingPlaying) {
      // Nothing is playing yet — start the first added track instead of sitting idle.
      setQueueOpen(false);
      loadPlayerTrack(playable[0]);
    } else {
      setToast({ title: "Queue updated", body: `${playable.length} track${playable.length === 1 ? "" : "s"} added locally.` });
    }
  }

  async function loadPlayerTrack(track) {
    if (!track?.id) return;
    try {
      setAudioUrl(`${API_BASE}/library/tracks/${track.id}/stream?api_key=${encodeURIComponent(token)}`);
      setCurrentTrack(track);
      recordPlay(track.id);
      reportPlayerStatus(track, "playing", { queue_length: playerQueue.length || 1, current_index: Math.max(0, playerQueue.findIndex((queuedTrack) => queuedTrack.id === track.id)) });
    } catch (playError) {
      notify("Playback failed", playError.message, "ui_error");
    }
  }

  function recordPlay(trackId) {
    if (!trackId) return;
    // De-dupe rapid re-loads of the same track (scrubbing prev/next) so play counts
    // aren't inflated; a genuine replay after 30s still records.
    const now = Date.now();
    const last = lastRecordedPlayRef.current;
    if (last && last.id === trackId && now - last.at < 30000) return;
    lastRecordedPlayRef.current = { id: trackId, at: now };
    api("/me/plays", { method: "POST", body: JSON.stringify({ track_id: trackId }) }).catch(() => {});
  }

  function reportPlayerStatus(track = currentTrack, status = "stopped", details = {}) {
    if (!user?.id) return;
    api("/player/status", {
      method: "POST",
      body: JSON.stringify({
        track_id: track?.id || null,
        title: track?.title || null,
        artist: track?._artist || null,
        album: track?._album || null,
        status,
        queue_length: details.queue_length ?? playerQueue.length,
        current_index: details.current_index ?? Math.max(0, currentTrackIndex),
        position_seconds: details.position_seconds ?? null,
        duration_seconds: details.duration_seconds ?? null,
        shuffle,
        repeat,
      }),
    }).catch(() => {});
  }

  function nextQueueIndex() {
    // Shuffle now physically reorders the queue, so "next" is always sequential.
    if (playerQueue.length === 0) return -1;
    const next = (currentTrackIndex < 0 ? -1 : currentTrackIndex) + 1;
    if (next >= playerQueue.length) return repeat === "all" ? 0 : -1;
    return next;
  }

  // Reconcile the queue to a desired shuffle state by reordering the queue itself
  // (not by jumping to random entries). Shared by the UI toggle and remote
  // commands so shuffle behaves identically regardless of client. Enabling
  // snapshots the current order and shuffles only the upcoming tracks (the played
  // ones + the current track stay put). Disabling restores the snapshot order with
  // already-played tracks dropped.
  function setShuffleState(desired) {
    setShuffle((current) => {
      if (current === desired) return current;
      if (desired) {
        setPlayerQueue((queue) => {
          unshuffledQueueRef.current = [...queue];
          const idx = queue.findIndex((track) => track.id === currentTrack?.id);
          const head = idx >= 0 ? queue.slice(0, idx + 1) : [];
          const tail = idx >= 0 ? queue.slice(idx + 1) : [...queue];
          for (let i = tail.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [tail[i], tail[j]] = [tail[j], tail[i]];
          }
          return [...head, ...tail];
        });
      } else {
        setPlayerQueue((queue) => {
          const snapshot = unshuffledQueueRef.current;
          unshuffledQueueRef.current = null;
          if (!snapshot) return queue;
          const idx = queue.findIndex((track) => track.id === currentTrack?.id);
          const playedIds = new Set(idx > 0 ? queue.slice(0, idx).map((track) => track.id) : []);
          const snapshotIds = new Set(snapshot.map((track) => track.id));
          // Original order minus what's already been played, then append anything
          // queued during shuffle that wasn't in the snapshot (and isn't played).
          const restored = snapshot.filter((track) => !playedIds.has(track.id));
          const added = queue.filter((track) => !snapshotIds.has(track.id) && !playedIds.has(track.id));
          return [...restored, ...added];
        });
      }
      return desired;
    });
  }

  function toggleShuffle() {
    setShuffleState(!shuffle);
  }

  async function playNextTrack() {
    const next = nextQueueIndex();
    if (next >= 0) await loadPlayerTrack(playerQueue[next]);
  }

  async function playPreviousTrack() {
    if (playerQueue.length === 0) return;
    const previousTrack = playerQueue[currentTrackIndex - 1] || playerQueue[0];
    if (previousTrack) await loadPlayerTrack(previousTrack);
  }

  function removeFromQueue(queueIndex) {
    const absoluteIndex = currentTrackIndex + 1 + queueIndex;
    setPlayerQueue((current) => current.filter((_, i) => i !== absoluteIndex));
  }

  function setApprovalSelection(batchId, itemIds, selected) {
    const selectedSet = new Set(itemIds);
    setApprovals((current) =>
      current.map((batch) =>
        batch.id !== batchId
          ? batch
          : { ...batch, items: batch.items.map((item) => (selectedSet.has(item.id) ? { ...item, selected } : item)) }
      )
    );
    api(`/approvals/${batchId}/selection`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds, selected }),
    }).catch(() => refreshApprovals());
  }

  function selectOnlyApprovalItem(batchId, siblingIds, itemId) {
    const siblingSet = new Set(siblingIds);
    setApprovals((current) =>
      current.map((batch) =>
        batch.id !== batchId
          ? batch
          : { ...batch, items: batch.items.map((item) => (siblingSet.has(item.id) ? { ...item, selected: item.id === itemId } : item)) }
      )
    );
    api(`/approvals/${batchId}/selection`, {
      method: "POST",
      body: JSON.stringify({ item_ids: siblingIds, selected: false }),
    }).then(() =>
      api(`/approvals/${batchId}/selection`, {
        method: "POST",
        body: JSON.stringify({ item_ids: [itemId], selected: true }),
      })
    ).catch(() => refreshApprovals());
  }

  async function cancelTask(taskId) {
    try {
      const task = await api(`/tasks/${taskId}/cancel`, { method: "POST" });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Task canceled", body: task.type });
      await refreshTasks();
      await refreshApprovals();
    } catch (cancelError) {
      notify("Cancel failed", cancelError.message, "ui_error");
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
      setToast({ title: "Tasks queued", body: `${batchIds.length} change groups were sent to the task queue.` });
      await refreshApprovals();
      window.setTimeout(refreshLibrary, 3500);
    } catch (approvalError) {
      notify("Task queue failed", approvalError.message, "ui_error");
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
          body: JSON.stringify({ item_ids: batchItems.map((item) => item.id), suppress_for: "none" }),
        });
      }
      setToast({ title: "Changes rejected", body: "Selected items were removed from the queue." });
      await refreshApprovals();
    } catch (rejectError) {
      notify("Reject failed", rejectError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  if (!token) {
    return <LoginScreen loading={loading} error={error} onLogin={login} />;
  }

  return (
    <main
      className={`${theme}${playerDocked ? " player-docked" : ""}`}
      style={{
        ...appearanceVars,
        "--player-dock-height": playerDocked ? `${playerDockHeight}px` : "0px",
        "--toast-bottom": playerDocked ? `${playerToastHeight + 32}px` : "18px",
      }}
    >
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">N</div>
          <div>
            <strong>Nudibranch</strong>
          </div>
        </div>
        <nav>
          {visibleNavItems.map(([label, Icon]) => (
            <button className={page === label ? "active" : ""} key={label} onClick={() => { setAlbumDetail(null); setArtistDetail(null); setPage(label); }}>
              <Icon size={17} />
              {label}
            </button>
          ))}
        </nav>
      </aside>

      <section className="workspace">
        <header className="topbar">
          {playerOpen && (
            <AudioPlayer
              controlRef={playbackControlRef}
              currentTrack={currentTrack}
              audioUrl={audioUrl}
              nextAudioUrl={nextAudioUrl}
              lyricsUrl={lyricsUrl}
              queue={playerQueue}
              currentIndex={currentTrackIndex}
              queueOpen={queueOpen}
              setQueueOpen={setQueueOpen}
              onPlayTrack={loadPlayerTrack}
              onEnded={playNextTrack}
              onSkipBack={playPreviousTrack}
              onSkipForward={playNextTrack}
              shuffle={shuffle}
              repeat={repeat}
              onToggleShuffle={toggleShuffle}
              onCycleRepeat={() => setRepeat((r) => (r === "off" ? "all" : r === "all" ? "one" : "off"))}
              onFavorite={toggleFavoriteTrack}
              favoriteTrackIds={favoriteTrackIds}
              onPlaybackState={(status, details) => reportPlayerStatus(currentTrack, status, details)}
              onDockChange={({ popped, compactHeight, fullHeight }) => {
                setPlayerPopped(popped);
                setPlayerDockHeight(compactHeight || 0);
                setPlayerToastHeight(fullHeight || compactHeight || 0);
              }}
              onRemoveFromQueue={removeFromQueue}
              crossfadeDuration={crossfadeDuration}
              apiKey={token}
              diagnostics={playerDiagnostics && !!user?.is_admin}
              onClose={() => {
                reportPlayerStatus(currentTrack, "stopped");
                setPlayerOpen(false);
              }}
            />
          )}
          <div className="topbar-side topbar-side-right">
            <button className="icon-button" onClick={refreshAll} title="Refresh">
              <RefreshCw size={18} />
            </button>
            {user && (
              <div className="notification-anchor" ref={trayRef}>
                <button className="icon-button" onClick={openNotificationTray} title="Notifications">
                  <Bell size={18} />
                  {unreadNotifications.length > 0 && <span className="badge">{unreadNotifications.length}</span>}
                </button>
                {trayOpen && <NotificationTray notifications={notifications} onClear={clearNotifications} />}
              </div>
            )}
            <button className="icon-button" onClick={logout} title="Sign out">
              <LogOut size={18} />
            </button>
          </div>
          {loading && <div className="working-indicator" aria-live="polite">Working…</div>}
        </header>

        <div className={`content-grid${NO_INSPECTOR_PAGES.has(page) ? " no-inspector" : ""}`}>
          <section className="panel main-panel">
            {albumDetail ? (
              <AlbumDetailPage
                detail={albumDetail}
                api={api}
                apiKey={token}
                onBack={closeAlbumDetail}
                onPlayAlbum={playAlbumFromHome}
                onQueueAlbum={queueAlbumFromHome}
                onPlayTracks={playTracks}
                onQueueTracks={addTracksToPlayerQueue}
                pinned={pinnedAlbumIds.has(albumDetail.id)}
                onTogglePin={toggleAlbumPin}
              />
            ) : artistDetail ? (
              <ArtistDetailPage
                detail={artistDetail}
                api={api}
                apiKey={token}
                onBack={closeArtistDetail}
                onPlayArtist={playArtistFromHome}
                onQueueArtist={queueArtistFromHome}
                onPlayTracks={playTracks}
                onQueueTracks={addTracksToPlayerQueue}
                onOpenAlbum={(al) => openAlbumDetail(al, artistDetail?.origin || page)}
                pinned={pinnedArtistIds.has(artistDetail.id)}
                onTogglePin={toggleArtistPin}
                library={library}
              />
            ) : (
            <>
            <PanelHeader page={page} queueSummary={queueSummary} displayName={user?.display_name} />
            {page === "Home" && (
              <HomeView api={api} apiKey={token} onPlayAlbum={playAlbumFromHome} onQueueAlbum={queueAlbumFromHome} onPlayPlaylist={playPlaylistFromHome} onOpenAlbum={(al) => openAlbumDetail(al, "Home")} onPlayArtist={playArtistFromHome} pinnedAlbumIds={pinnedAlbumIds} onTogglePinAlbum={toggleAlbumPin} pinnedArtistIds={pinnedArtistIds} onTogglePinArtist={toggleArtistPin} homeVersion={homeVersion} onUnpinPlaylist={unpinPlaylist} onOpenArtist={(ar) => openArtistDetail(ar, "Home")} onQueueArtist={queueArtistFromHome} onPlayTracks={playTracks} onQueueTracks={addTracksToPlayerQueue} onPlayAll={() => playAllLibrary(false)} onShuffleAll={() => playAllLibrary(true)} />
            )}
            {page === "Library" && (
              <LibraryTree
                artists={library}
                onCheckAlbum={lookupImportAlbum}
                onCoverSearch={searchAlbumCover}
                onCheckTrackAudio={checkLibraryTrackAudio}
                onRequeueTrack={requeueTrackReplacement}
                onRequeueAlbum={requeueAlbumReplacement}
                onSearchAlbums={searchImportAlbums}
                onQueueMetadata={applyLibraryMetadata}
                onQueueRemove={proposeLibraryRemove}
                playlists={playlists}
                onAddToPlaylist={addTracksToPlaylist}
                user={user}
                apiKey={token}
                api={api}
                onPlay={playTracks}
                onQueue={addTracksToPlayerQueue}
                onSearchLibrary={searchLibrary}
                onSavePageSize={saveLibraryPageSize}
                onPlayAlbum={playAlbumFromHome}
                onQueueAlbum={queueAlbumFromHome}
                onOpenAlbum={(al) => openAlbumDetail(al, "Library")}
                onTogglePinAlbum={toggleAlbumPin}
                pinnedAlbumIds={pinnedAlbumIds}
                onTogglePinArtist={toggleArtistPin}
                pinnedArtistIds={pinnedArtistIds}
                onArtistCoverSearch={searchArtistCover}
                onAlbumCoverUpload={uploadAlbumCover}
                onArtistCoverUpload={uploadArtistCover}
                refreshVersion={refreshVersion}
                onOpenArtist={(ar) => openArtistDetail(ar, "Library")}
                onPlayArtist={playArtistFromHome}
                onQueueArtist={queueArtistFromHome}
              />
            )}
            {page === "Discover" && (
              <DiscoverView
                user={user}
                onSearch={searchDiscover}
                onFetchTracks={fetchDiscoverAlbumTracks}
                onWishlist={createWishlistItem}
                onQueue={queueDiscoverDownloads}
                apiKey={token}
              />
            )}
            {page === "Task Queue" && (
              <Approvals
                approvals={approvals}
                onSelection={setApprovalSelection}
                onSelectOnly={selectOnlyApprovalItem}
                onApprove={approveItems}
                onReject={rejectItems}
              />
            )}
            {page === "Import/Add" && (
              <ImportWizard
                files={importFiles}
                onFilesChange={setImportFiles}
                library={library}
                onRecheckTrack={recheckImportTrack}
                onRecheckAlbum={recheckImportAlbum}
                onCheckAlbum={lookupImportAlbum}
                onSearchAlbums={searchImportAlbums}
                seedDownloadRequests={importSeedDownloads}
                albumSearchOpen={importAlbumSearchOpen}
                setAlbumSearchOpen={setImportAlbumSearchOpen}
                onDownloadRequestsChange={setImportDownloadRequests}
                addAlbumsRef={addImportAlbumsRef}
              />
            )}
            {page === "Activity" && (
              <>
                <TasksView tasks={tasks} playback={userPlayback} onCancel={cancelTask} />
                <PlayHistoryPanel api={api} />
              </>
            )}
            {page === "Settings" && (
              <SettingsPanel
                accentColor={accentColor}
                setAccentColor={setAccentColor}
                backgroundTint={backgroundTint}
                setBackgroundTint={setBackgroundTint}
                dark={dark}
                setDark={setDark}
                crossfadeDuration={crossfadeDuration}
                setCrossfadeDuration={setCrossfadeDuration}
                onSaveSearchThreshold={saveSearchThreshold}
                user={user}
                apiKey={token}
                playlists={playlists}
                integrationSettings={integrationSettings}
                onSaveIntegrations={saveIntegrationSettings}
                onUploadYoutubeCookies={uploadYoutubeCookies}
                api={api}
                notify={notify}
                playerDiagnostics={playerDiagnostics}
                onTogglePlayerDiagnostics={togglePlayerDiagnostics}
              />
            )}
            {page === "Tools" && (
              <ToolsView
                tasks={tasks}
                appLogs={appLogs}
                user={user}
                backups={backups}
                onRun={runTool}
                onFix={proposeCheckFileFix}
                api={api}
                notify={notify}
              />
            )}
            {page === "Wishlist" && (
              <WishlistView
                wishlist={wishlist}
                approvals={wishlistApprovals}
                user={user}
                onAdd={createWishlistItem}
                onRemove={removeWishlistItem}
                onRemoveMany={removeWishlistItems}
                onSubmit={submitWishlistApprovals}
                onSearchAlbums={searchImportAlbums}
                onLookupAlbum={lookupImportAlbum}
                onInspectorActionsChange={setWishlistInspectorActions}
              />
            )}
            {page === "Playlists" && (
              <PlaylistsView
                playlists={playlists}
                library={library}
                onCreatePlaylist={createPlaylist}
                onAddToPlaylist={addTracksToPlaylist}
                onRename={renamePlaylist}
                onDelete={deletePlaylist}
                onPlay={playTracks}
                onQueue={addTracksToPlayerQueue}
                onQueuePosition={proposePlaylistPosition}
                onInspectorActionsChange={setPlaylistInspectorActions}
                api={api}
              />
            )}
            {page === "Users" && (
              <UsersView
                users={users}
                permissions={permissionCatalog}
                currentUser={user}
                canManage={canManageUsers(user)}
                onCreate={createUserAccount}
                onUpdate={updateUserAccount}
                onDelete={deleteUserAccount}
                onUpdatePin={updateUserPin}
                onUpdateOwnPin={updateOwnPin}
                jellyfinUsers={jellyfinUsers}
                jellyfinUsersLoading={jellyfinUsersLoading}
                onLoadJellyfinUsers={loadJellyfinUsers}
                onUpdateJellyfinUser={updateUserJellyfinUser}
                api={api}
              />
            )}
            {page === "Automations" && <AutomationsView api={api} notify={notify} user={user} />}
            {!["Home", "Library", "Discover", "Task Queue", "Import/Add", "Activity", "Settings", "Tools", "Wishlist", "Playlists", "Users", "Automations"].includes(page) && <Placeholder page={page} />}
            </>
            )}
          </section>

          {!NO_INSPECTOR_PAGES.has(page) && (
          <Inspector
            page={page}
            api={api}
            library={library}
            importFiles={importFiles}
            importDownloadRequests={importDownloadRequests}
            approvals={approvals}
            wishlist={wishlist}
            playlists={playlists}
            queueItemCount={queueItemCount}
            queueSelectionCount={queueSelectionCount}
            tasks={tasks}
            downloadProgress={downloadProgressSummary(approvals)}
            importActions={{
              onScan: scanImportFolder,
              onToggleAlbumSearch: () => setImportAlbumSearchOpen((value) => !value),
              onPropose: () => proposeImport(importDownloadRequests),
              onUpload: uploadImportFiles,
              onCancelUpload: cancelImportUpload,
              onClearFolder: clearImportFolder,
              hasFiles: importFiles.length > 0,
              uploadProgress: importUploadProgress,
              loading,
              activeImportTask,
              downloadCount: importDownloadRequests.length,
              hasPendingPlaylist: !!(pendingPlaylistName && pendingPlaylistOriginalTracks && pendingPlaylistOriginalTracks.length > 0),
              // Allow submitting even with nothing to download/import when a playlist is pending —
              // the playlist still gets created/updated from the songs already in the library.
              disabled:
                loading ||
                activeImportTask ||
                (importFiles.length === 0 &&
                  importDownloadRequests.length === 0 &&
                  !(pendingPlaylistName && pendingPlaylistOriginalTracks && pendingPlaylistOriginalTracks.length > 0)),
            }}
            wishlistActions={wishlistInspectorActions}
            playlistActions={playlistInspectorActions}
            mappingSyncStats={mappingSyncStats}
            playlistImportActions={{
              open: playlistImportOpen,
              setOpen: setPlaylistImportOpen,
              url: playlistImportUrl,
              setUrl: setPlaylistImportUrl,
              mode: playlistImportMode,
              setMode: setPlaylistImportMode,
              loading: playlistImportLoading,
              onImport: importPlaylist,
            }}
          />
          )}
        </div>
        {toast && <Toast title={toast.title} body={toast.body} onClose={() => setToast(null)} />}
      </section>
    </main>
  );
}

function LoginScreen({ loading, error, onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  return (
    <main className="login-page">
      <form
        className="login-panel"
        onSubmit={(event) => {
          event.preventDefault();
          onLogin(username, password);
        }}
      >
        <div className="brand login-brand">
          <div className="brand-mark">N</div>
          <strong>Nudibranch</strong>
        </div>
        <label>
          Username
          <input autoFocus value={username} onChange={(event) => setUsername(event.target.value)} />
        </label>
        <label>
          Password
          <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" />
        </label>
        {error && <div className="error-banner">{error}</div>}
        <button className="primary" disabled={loading || !username.trim() || !password}>
          {loading ? "Signing in" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

function NotificationTray({ notifications, onClear }) {
  return (
    <div className="notification-tray">
      <div className="notification-header">
        <h2>Notifications</h2>
        <button className="secondary compact" onClick={onClear} disabled={notifications.length === 0}>
          Clear
        </button>
      </div>
      <div className="notification-list">
        {notifications.length === 0 ? (
          <p className="empty-state">No notifications yet.</p>
        ) : (
          notifications.map((notification) => (
            <TrayItem
              key={notification.id}
              tone={notificationSeverity(notification)}
              title={notification.title}
              body={notification.body}
            />
          ))
        )}
      </div>
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

function PanelHeader({ page, queueSummary, displayName }) {
  const description = page === "Task Queue" ? queueSummary : pageDescriptions[page];
  let heading = page;
  if (page === "Home") {
    const hour = new Date().getHours();
    const period = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
    heading = `Good ${period}${displayName ? `, ${displayName}` : ""}`;
  }

  return (
    <div className="panel-header">
      <div>
        <h1>{heading}</h1>
        <p>{description ?? "Manage this section of Nudibranch."}</p>
      </div>
    </div>
  );
}

function LibraryAlbumGrid({ api, apiKey, bucket, pageSize, onPageSizeChange, onPlayAlbum, onQueueAlbum, onOpenAlbum, onTogglePinAlbum, pinnedAlbumIds, refreshVersion }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [bucket, pageSize]);
  useEffect(() => {
    let active = true;
    setData(null);
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (bucket && bucket !== "all") params.set("bucket", bucket);
    api(`/library/albums?${params.toString()}`)
      .then((d) => { if (active) setData(d); })
      .catch(() => { if (active) setData({ items: [], total: 0 }); });
    return () => { active = false; };
  }, [api, bucket, page, pageSize, refreshVersion]);
  const items = data?.items || [];
  const total = data?.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="library-album-view">
      {data === null ? (
        <p className="muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="muted">No albums in this bucket.</p>
      ) : (
        <div className="home-album-grid">
          {items.map((al) => (
            <AlbumCard
              key={al.id}
              album={al}
              apiKey={apiKey}
              onPlay={onPlayAlbum}
              onQueue={onQueueAlbum}
              onOpen={onOpenAlbum}
              pinned={pinnedAlbumIds?.has(al.id)}
              onTogglePin={onTogglePinAlbum}
            />
          ))}
        </div>
      )}
      <div className="tree-toolbar library-page-size-row">
        <span className="muted">{total} albums</span>
        <div className="album-page-nav">
          <button type="button" className="secondary compact" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
          <span className="muted">Page {page} / {totalPages}</span>
          <button type="button" className="secondary compact" disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button>
        </div>
        <label className="library-page-size">
          <span>Per page</span>
          <select value={pageSize} onChange={(e) => onPageSizeChange(Number(e.target.value))}>
            {[20, 50, 100, 500, 1000].map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

function LibraryArtistGrid({ api, apiKey, bucket, pageSize, onPageSizeChange, onPlayArtist, onQueueArtist, onOpenArtist, onTogglePinArtist, pinnedArtistIds, refreshVersion }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  useEffect(() => { setPage(1); }, [bucket, pageSize]);
  useEffect(() => {
    let active = true;
    setData(null);
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (bucket && bucket !== "all") params.set("bucket", bucket);
    api(`/library/artists?${params.toString()}`)
      .then((d) => { if (active) setData(d); })
      .catch(() => { if (active) setData({ items: [], total: 0 }); });
    return () => { active = false; };
  }, [api, bucket, page, pageSize, refreshVersion]);
  const items = data?.items || [];
  const total = data?.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="library-album-view">
      {data === null ? (
        <p className="muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="muted">No artists in this bucket.</p>
      ) : (
        <div className="home-album-grid">
          {items.map((ar) => (
            <ArtistCard
              key={ar.id}
              artist={ar}
              apiKey={apiKey}
              onPlay={onPlayArtist}
              onQueue={onQueueArtist}
              onOpen={onOpenArtist}
              pinned={pinnedArtistIds?.has(ar.id)}
              onTogglePin={onTogglePinArtist}
            />
          ))}
        </div>
      )}
      <div className="tree-toolbar library-page-size-row">
        <span className="muted">{total} artists</span>
        <div className="album-page-nav">
          <button type="button" className="secondary compact" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
          <span className="muted">Page {page} / {totalPages}</span>
          <button type="button" className="secondary compact" disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button>
        </div>
        <label className="library-page-size">
          <span>Per page</span>
          <select value={pageSize} onChange={(e) => onPageSizeChange(Number(e.target.value))}>
            {[20, 50, 100, 500, 1000].map((n) => (<option key={n} value={n}>{n}</option>))}
          </select>
        </label>
      </div>
    </div>
  );
}


function LibraryTrackBranch({ ctx, artist, album, track, depth = 2 }) {
  // Clicking the row body plays this track and queues the rest of the album after it.
  const playFromHere = () => {
    if (!ctx.onPlay) return;
    const tracks = albumTracks(artist, album);
    const idx = tracks.findIndex((t) => t.id === track.id);
    ctx.onPlay(idx >= 0 ? tracks.slice(idx) : [hydrateTrack(track, artist, album)]);
  };
  return (
    <div>
      <div className="tree-action-row library-row-actions">
        <TreeRow
          depth={depth}
          icon={FileAudio}
          title={`${track.track_number ? String(track.track_number).padStart(2, "0") : "#"}-${track.title}`}
          meta={track.format || "audio"}
          warning={!track.is_lossless}
          onActivate={ctx.onPlay ? playFromHere : undefined}
        />
        <QuickLibraryActions
          onPlay={() => ctx.onPlay([hydrateTrack(track, artist, album)])}
          onQueue={() => ctx.onQueue([hydrateTrack(track, artist, album)])}
          onRemove={ctx.canRemoveLibrary ? () => ctx.setRemoveTarget(removeKey("track", track.id)) : null}
        />
        {(ctx.canEditMetadata || ctx.canUsePlaylists) && (
          <button className="row-icon-button" onClick={() => toggleSet(ctx.setOpenTrackDetails, track.id)} title="Edit song">
            <Pencil size={15} />
          </button>
        )}
      </div>
      {ctx.removeTarget === removeKey("track", track.id) && (
        <RemoveChoice
          title={track.title}
          onCancel={() => ctx.setRemoveTarget(null)}
          onChoose={(action) => { ctx.onQueueRemove("track", track.id, action); ctx.setRemoveTarget(null); }}
        />
      )}
      {ctx.openTrackDetails?.has(track.id) && (
        <LibraryMetadataEditor
          targetType="track"
          targetId={track.id}
          title={track.title}
          fields={trackFields(track)}
          details={{ artist: artist.name, album: album.title }}
          onAutoLookup={(field, draft) => trackAutoLookup(field, draft, artist.name, album.title, ctx.onCheckAlbum)}
          onSearchAlbums={ctx.onSearchAlbums}
          playlists={ctx.canUsePlaylists ? ctx.playlists : []}
          targetTrackIds={[track.id]}
          onAddToPlaylist={ctx.onAddToPlaylist}
          onVerifyAudio={ctx.canEditMetadata ? () => ctx.onCheckTrackAudio(track) : null}
          onRequeue={ctx.canRemoveLibrary && ctx.onRequeueTrack ? () => ctx.onRequeueTrack(track) : null}
          onQueue={ctx.onQueueMetadata}
          onClose={() => toggleSet(ctx.setOpenTrackDetails, track.id)}
        />
      )}
    </div>
  );
}

function LibraryAlbumBranch({ ctx, artist, album, depth = 1 }) {
  return (
    <div>
      <div className="tree-action-row library-row-actions">
        <TreeRow
          depth={depth}
          icon={Folder}
          open={ctx.openAlbums?.has(album.id)}
          title={album.title}
          meta={`${album.tracks.length} tracks`}
          onToggle={() => toggleSet(ctx.setOpenAlbums, album.id)}
        />
        <AlbumResultArt src={album._coverUrl} />
        <QuickLibraryActions
          onPlay={() => ctx.onPlay(albumTracks(artist, album), { keepLead: false })}
          onQueue={() => ctx.onQueue(albumTracks(artist, album))}
        />
        {ctx.onTogglePinAlbum && (
          <button
            className={`row-icon-button${ctx.pinnedAlbumIds?.has(album.id) ? " active" : ""}`}
            onClick={() => ctx.onTogglePinAlbum(album)}
            title={ctx.pinnedAlbumIds?.has(album.id) ? "Unpin from Home" : "Pin to Home"}
          >
            <Pin size={15} />
          </button>
        )}
        {ctx.onOpenAlbum && (
          <button className="row-icon-button" onClick={() => ctx.onOpenAlbum({ ...album, artist_name: artist.name, artist_id: artist.id })} title="Open album">
            <Compass size={15} />
          </button>
        )}
        {(ctx.canEditMetadata || ctx.canRemoveLibrary || ctx.canUsePlaylists) && (
          <button className="row-icon-button" onClick={() => toggleSet(ctx.setOpenAlbumDetails, album.id)} title="Edit album">
            <Pencil size={15} />
          </button>
        )}
      </div>
      {ctx.removeTarget === removeKey("album", album.id) && (
        <RemoveChoice
          title={album.title}
          onCancel={() => ctx.setRemoveTarget(null)}
          onChoose={(action) => { ctx.onQueueRemove("album", album.id, action); ctx.setRemoveTarget(null); }}
        />
      )}
      {ctx.openAlbumDetails?.has(album.id) && (
        <LibraryMetadataEditor
          targetType="album"
          targetId={album.id}
          title={album.title}
          coverUrl={album._coverUrl}
          fields={albumFields(album)}
          details={{ artist: artist.name, tracks: album.tracks.length }}
          onAutoLookup={(field, draft) => albumAutoLookup(field, draft, artist.name, ctx.onCheckAlbum, album.id, ctx.onCoverSearch)}
          onCoverUpload={ctx.onAlbumCoverUpload ? (file) => ctx.onAlbumCoverUpload(album.id, file) : undefined}
          onSearchAlbums={ctx.onSearchAlbums}
          playlists={ctx.canUsePlaylists ? ctx.playlists : []}
          targetTrackIds={albumTracks(artist, album).map((t) => t.id)}
          onAddToPlaylist={ctx.onAddToPlaylist}
          onRequeue={ctx.canRemoveLibrary && ctx.onRequeueAlbum ? () => ctx.onRequeueAlbum(album) : null}
          onRemove={ctx.canRemoveLibrary ? () => ctx.setRemoveTarget(removeKey("album", album.id)) : null}
          onQueue={ctx.onQueueMetadata}
          onClose={() => toggleSet(ctx.setOpenAlbumDetails, album.id)}
        />
      )}
      {ctx.openAlbums?.has(album.id) &&
        album.tracks.map((track) => (
          <LibraryTrackBranch key={track.id} ctx={ctx} artist={artist} album={album} track={track} depth={depth + 1} />
        ))}
    </div>
  );
}

function LibraryTree({ artists, onCheckAlbum, onCoverSearch, onCheckTrackAudio, onRequeueTrack, onRequeueAlbum, onSearchAlbums, onQueueMetadata, onQueueRemove, playlists, onAddToPlaylist, user, apiKey, api, onPlay, onQueue, onSearchLibrary, onSavePageSize, onPlayAlbum, onQueueAlbum, onOpenAlbum, onTogglePinAlbum, pinnedAlbumIds, onTogglePinArtist, pinnedArtistIds, onArtistCoverSearch, onAlbumCoverUpload, onArtistCoverUpload, refreshVersion, onOpenArtist, onPlayArtist, onQueueArtist }) {
  const [libraryEntity, setLibraryEntity] = useState("artist");
  const [libraryLayout, setLibraryLayout] = useState("tree");
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [openArtistDetails, setOpenArtistDetails] = useState(() => new Set());
  const [openAlbumDetails, setOpenAlbumDetails] = useState(() => new Set());
  const [openTrackDetails, setOpenTrackDetails] = useState(() => new Set());
  const [removeTarget, setRemoveTarget] = useState(null);
  const visibleArtists = useMemo(
    () =>
      artists
        .map((artist) => ({
          ...artist,
          albums: artist.albums
            .filter((album) => album.tracks.length > 0)
            .map((album) => ({ ...album, _coverUrl: albumCoverUrl(album, apiKey) })),
        }))
        .filter((artist) => artist.albums.length > 0),
    [artists, apiKey],
  );
  const [bucket, setBucket] = useState("all");
  const availableBuckets = useMemo(() => {
    const ordered = ["#"];
    for (let i = 65; i <= 90; i++) ordered.push(String.fromCharCode(i));
    return ordered;
  }, []);
  const bucketedArtists = useMemo(
    () => (bucket === "all" ? visibleArtists : visibleArtists.filter((a) => artistBucket(a) === bucket)),
    [visibleArtists, bucket],
  );
  const [pageSize, setPageSize] = useState(() => (user && user.library_page_size != null ? user.library_page_size : 100));
  // Resync if the user object loads/changes after mount (the workspace can render before /me resolves).
  useEffect(() => {
    if (user && user.library_page_size != null) setPageSize(user.library_page_size);
  }, [user?.library_page_size]);
  const pagedArtists = useMemo(() => bucketedArtists.slice(0, pageSize), [bucketedArtists, pageSize]);
  const changePageSize = (v) => { setPageSize(v); if (onSavePageSize) onSavePageSize(v); };
  const canEditMetadata = hasPermission(user, "library:edit");
  const canRemoveLibrary = hasPermission(user, "library:edit");
  const canUsePlaylists = hasPermission(user, "playlists:manage");
  const albumRows = useMemo(() => {
    const rows = [];
    for (const ar of visibleArtists) for (const al of ar.albums) rows.push({ album: al, artist: ar });
    rows.sort((a, b) => (a.album.title || "").localeCompare(b.album.title || ""));
    return rows;
  }, [visibleArtists]);
  const bucketedAlbums = useMemo(
    () => (bucket === "all" ? albumRows : albumRows.filter((r) => titleBucket(r.album.title) === bucket)),
    [albumRows, bucket],
  );
  const pagedAlbums = useMemo(() => bucketedAlbums.slice(0, pageSize), [bucketedAlbums, pageSize]);
  const treeCtx = {
    onPlay, onQueue,
    canEditMetadata, canRemoveLibrary, canUsePlaylists,
    playlists, onAddToPlaylist,
    removeTarget, setRemoveTarget, onQueueRemove,
    openAlbums, setOpenAlbums,
    openAlbumDetails, setOpenAlbumDetails,
    openTrackDetails, setOpenTrackDetails,
    onCheckAlbum, onCoverSearch, onAlbumCoverUpload,
    onSearchAlbums, onQueueMetadata,
    onCheckTrackAudio,
    onRequeueTrack, onRequeueAlbum,
    pinnedAlbumIds, onTogglePinAlbum,
    onOpenAlbum,
  };

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState(null); // null = not searching
  const threshold = user && user.search_min_confidence != null ? user.search_min_confidence : 0.4;

  useEffect(() => {
    const q = searchQuery.trim();
    if (!q) { setSearchResults(null); return; }
    let active = true;
    const t = setTimeout(async () => {
      try {
        const res = await onSearchLibrary(q, threshold);
        if (active) setSearchResults(res);
      } catch { if (active) setSearchResults([]); }
    }, 250);
    return () => { active = false; clearTimeout(t); };
  }, [searchQuery, threshold, onSearchLibrary]);

  function revealResult(r) {
    setBucket("all");
    if (r.kind === "artist") {
      setOpenArtists((prev) => new Set(prev).add(r.id));
    } else if (r.kind === "album") {
      setOpenArtists((prev) => new Set(prev).add(r.artist_id));
      setOpenAlbums((prev) => new Set(prev).add(r.id));
    } else {
      setOpenArtists((prev) => new Set(prev).add(r.artist_id));
      setOpenAlbums((prev) => new Set(prev).add(r.album_id));
    }
    setSearchQuery("");
    setSearchResults(null);
  }

  return (
    <div className="library-view">
      {visibleArtists.length === 0 && (
        <EmptyState title="No library records" body="Import queued music to populate the managed library." />
      )}
      {visibleArtists.length > 0 && (
        <form className="discover-search library-search-bar" onSubmit={(e) => e.preventDefault()}>
          <Search size={17} />
          <input
            type="text"
            placeholder="Search artists, albums, tracks…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />
          {searchQuery ? (
            <button type="button" className="secondary compact" onClick={() => setSearchQuery("")} title="Clear search">
              ✕
            </button>
          ) : (
            <span />
          )}
        </form>
      )}
      {searchResults !== null ? (
        <div className="tree library-search-results">
          {searchResults.length === 0 ? (
            <div className="tree-empty-message">No matches</div>
          ) : (
            [
              { label: "Artists", kind: "artist" },
              { label: "Albums", kind: "album" },
              { label: "Tracks", kind: "track" },
            ]
              .filter(({ kind }) => searchResults.some((r) => r.kind === kind))
              .map(({ label, kind }) => (
                <div key={kind} className="library-search-group">
                  <div className="library-search-group-label">{label}</div>
                  {searchResults
                    .filter((r) => r.kind === kind)
                    .map((r) => (
                      <button
                        key={`${r.kind}:${r.id}`}
                        type="button"
                        className="library-search-result-row"
                        onClick={() => revealResult(r)}
                        title="Show in library"
                      >
                        <span className="library-search-result-name">{r.name}</span>
                        <small className="library-search-result-confidence muted">{Math.round(r.confidence * 100)}%</small>
                      </button>
                    ))}
                </div>
              ))
          )}
        </div>
      ) : (
        <>
          {visibleArtists.length > 0 && (
            <div className="tree-toolbar library-bucket-bar">
              {availableBuckets.length > 1 && (
                <div className="bucket-row">
                  {["all", ...availableBuckets].map((b) => (
                    <button
                      key={b}
                      type="button"
                      className={`bucket-btn${bucket === b ? " active" : ""}`}
                      title={b === "#" ? "Numbers & symbols" : undefined}
                      onClick={() => setBucket(b)}
                    >
                      {b === "all" ? "All" : b}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          {visibleArtists.length > 0 && (
            <div className="tree-toolbar library-control-row">
              {libraryLayout === "tree" && (
                <button
                  className="secondary compact"
                  onClick={() => {
                    if (libraryEntity === "artist") {
                      const expanded = openArtists.size > 0 || openAlbums.size > 0;
                      if (expanded) { setOpenArtists(new Set()); setOpenAlbums(new Set()); }
                      else {
                        setOpenArtists(new Set(visibleArtists.map((a) => a.id)));
                        setOpenAlbums(new Set(visibleArtists.flatMap((a) => a.albums.map((al) => al.id))));
                      }
                    } else {
                      if (openAlbums.size > 0) setOpenAlbums(new Set());
                      else setOpenAlbums(new Set(pagedAlbums.map((r) => r.album.id)));
                    }
                  }}
                >
                  {(libraryEntity === "artist" ? (openArtists.size > 0 || openAlbums.size > 0) : openAlbums.size > 0) ? "Collapse all" : "Expand all"}
                </button>
              )}
              <div className="library-view-toggle">
                <button type="button" className={libraryLayout === "tree" ? "active" : ""} onClick={() => setLibraryLayout("tree")}>Tree</button>
                <button type="button" className={libraryLayout === "grid" ? "active" : ""} onClick={() => setLibraryLayout("grid")}>Grid</button>
              </div>
              <div className="library-view-toggle">
                <button type="button" className={libraryEntity === "artist" ? "active" : ""} onClick={() => setLibraryEntity("artist")}>Artists</button>
                <button type="button" className={libraryEntity === "album" ? "active" : ""} onClick={() => setLibraryEntity("album")}>Albums</button>
              </div>
            </div>
          )}
          {libraryEntity === "artist" && libraryLayout === "tree" && (
          <div className="tree">
        {pagedArtists.map((artist) => (
          <div key={artist.id}>
            <div className="tree-action-row library-row-actions">
              <TreeRow
                icon={Folder}
                open={openArtists.has(artist.id)}
                title={artist.name}
                meta={`${artist.albums.length} albums`}
                onToggle={() => toggleSet(setOpenArtists, artist.id)}
              />
              <QuickLibraryActions
                onPlay={() => onPlay(artistTracks(artist), { keepLead: false })}
                onQueue={() => onQueue(artistTracks(artist))}
                onRemove={canRemoveLibrary ? () => setRemoveTarget(removeKey("artist", artist.id)) : null}
              />
              {onTogglePinArtist && (
                <button
                  className={`row-icon-button${pinnedArtistIds?.has(artist.id) ? " active" : ""}`}
                  onClick={() => onTogglePinArtist(artist)}
                  title={pinnedArtistIds?.has(artist.id) ? "Unpin from Home" : "Pin to Home"}
                >
                  <Pin size={15} />
                </button>
              )}
              {canEditMetadata && (
                <button className="row-icon-button" onClick={() => toggleSet(setOpenArtistDetails, artist.id)} title="Edit artist">
                  <Pencil size={15} />
                </button>
              )}
            </div>
            {removeTarget === removeKey("artist", artist.id) && (
              <RemoveChoice
                title={artist.name}
                onCancel={() => setRemoveTarget(null)}
                onChoose={(action) => {
                  onQueueRemove("artist", artist.id, action);
                  setRemoveTarget(null);
                }}
              />
            )}
            {openArtistDetails.has(artist.id) && (
              <LibraryMetadataEditor
                targetType="artist"
                targetId={artist.id}
                title={artist.name}
                fields={artistFields(artist)}
                playlists={canUsePlaylists ? playlists : []}
                targetTrackIds={artistTracks(artist).map((track) => track.id)}
                onAddToPlaylist={onAddToPlaylist}
                onQueue={onQueueMetadata}
                onAutoLookup={(field, draft) => artistAutoLookup(field, draft, artist.id, onArtistCoverSearch)}
                onCoverUpload={onArtistCoverUpload ? (file) => onArtistCoverUpload(artist.id, file) : undefined}
                onClose={() => toggleSet(setOpenArtistDetails, artist.id)}
              />
            )}
            {openArtists.has(artist.id) &&
              artist.albums.map((album) => (
                <LibraryAlbumBranch key={album.id} ctx={treeCtx} artist={artist} album={album} />
              ))}
          </div>
        ))}
      </div>
          )}
          {libraryEntity === "artist" && libraryLayout === "grid" && (
            <LibraryArtistGrid
              api={api}
              apiKey={apiKey}
              bucket={bucket}
              pageSize={pageSize}
              onPageSizeChange={changePageSize}
              onPlayArtist={onPlayArtist}
              onQueueArtist={onQueueArtist}
              onOpenArtist={onOpenArtist}
              onTogglePinArtist={onTogglePinArtist}
              pinnedArtistIds={pinnedArtistIds}
              refreshVersion={refreshVersion}
            />
          )}
          {libraryEntity === "album" && libraryLayout === "grid" && (
            <LibraryAlbumGrid
              api={api}
              apiKey={apiKey}
              bucket={bucket}
              pageSize={pageSize}
              onPageSizeChange={changePageSize}
              onPlayAlbum={onPlayAlbum}
              onQueueAlbum={onQueueAlbum}
              onOpenAlbum={onOpenAlbum}
              onTogglePinAlbum={onTogglePinAlbum}
              pinnedAlbumIds={pinnedAlbumIds}
              refreshVersion={refreshVersion}
            />
          )}
          {libraryEntity === "album" && libraryLayout === "tree" && (
            <div className="tree">
              {pagedAlbums.length === 0 ? (
                <p className="muted">No albums in this bucket.</p>
              ) : (
                pagedAlbums.map(({ album, artist }) => (
                  <LibraryAlbumBranch key={album.id} ctx={treeCtx} artist={artist} album={album} depth={0} />
                ))
              )}
            </div>
          )}
          {libraryLayout === "tree" && (
            <div className="tree-toolbar library-page-size-row">
              <span className="muted">
                {libraryEntity === "artist" ? `Showing ${Math.min(pageSize, bucketedArtists.length)} of ${bucketedArtists.length}` : `Showing ${Math.min(pageSize, bucketedAlbums.length)} of ${bucketedAlbums.length}`}
              </span>
              <label className="library-page-size">
                <span>Per page</span>
                <select
                  value={pageSize}
                  onChange={(e) => {
                    const v = Number(e.target.value);
                    setPageSize(v);
                    if (onSavePageSize) onSavePageSize(v);
                  }}
                >
                  {[20, 50, 100, 500, 1000].map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function QueueButton({ onClick, className = "row-icon-button", title = "Add to queue", size = 14, disabled = false, children }) {
  const [added, setAdded] = useState(false);
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <button
      className={`${className}${added ? " queued-flash" : ""}`}
      type="button"
      disabled={disabled}
      title={title}
      onClick={(event) => {
        event.stopPropagation();
        onClick?.(event);
        setAdded(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setAdded(false), 700);
      }}
    >
      {children ?? <ListPlus size={size} />}
    </button>
  );
}

function QuickLibraryActions({ onPlay, onQueue, onRemove }) {
  return (
    <div className="quick-library-actions">
      <button className="row-icon-button" onClick={onPlay} title="Play">
        <Play size={14} />
      </button>
      <QueueButton onClick={onQueue} title="Add to local queue" />
      {onRemove && (
        <button className="row-icon-button" onClick={onRemove} title="Remove">
          <Trash2 size={14} />
        </button>
      )}
    </div>
  );
}

function RemoveChoice({ title, onChoose, onCancel }) {
  return (
    <div className="remove-choice">
      <strong>{title}</strong>
      <span>Queue this change for review.</span>
      <button className="secondary compact" onClick={() => onChoose("move_to_import")}>
        Move to import
      </button>
      <button className="secondary compact danger" onClick={() => onChoose("delete")}>
        Delete from library
      </button>
      <button className="row-icon-button" onClick={onCancel} title="Cancel">
        <X size={14} />
      </button>
    </div>
  );
}

function Approvals({ approvals, onSelection, onSelectOnly, onApprove, onReject }) {
  const groups = useMemo(() => groupApprovalBatches(approvals), [approvals]);

  if (groups.length === 0) {
    return <EmptyState title="No queued changes" body="Import scans, download searches, and maintenance actions will add review items here." />;
  }

  return (
    <div className="approval-tree">
      {groups.map((group) => (
        <ApprovalBatch key={group.id} batch={group} onSelection={onSelection} onSelectOnly={onSelectOnly} onApprove={onApprove} onReject={onReject} />
      ))}
    </div>
  );
}

function ApprovalBatch({ batch, onSelection, onSelectOnly, onApprove, onReject }) {
  const [openItems, setOpenItems] = useState(() => new Set(batch.items.filter((item) => !item.parent_id).map((item) => item.id)));
  const [openCandidatePickers, setOpenCandidatePickers] = useState(() => new Set());
  const tree = useMemo(() => buildItemTree(batch.items), [batch.items]);
  const itemById = useMemo(() => new Map(batch.items.map((item) => [item.id, item])), [batch.items]);
  const selectedItems = batch.items.filter((item) => item.selected);
  const selectedExecutableItems = selectedItems.filter(isExecutableApprovalItem);
  const allSelected = selectedItems.length === batch.items.length && batch.items.length > 0;
  const locked = batch.status === "executing";
  const candidatesSearching = batch.items.some(isCandidateSearchItem);
  const runDisabled = locked || candidatesSearching || selectedExecutableItems.length === 0;

  const prevBatchId = useRef(null);
  useEffect(() => {
    const rootIds = new Set(batch.items.filter((item) => !item.parent_id).map((item) => item.id));
    if (prevBatchId.current !== batch.id) {
      prevBatchId.current = batch.id;
      setOpenItems(rootIds);
    } else {
      setOpenItems((prev) => {
        const next = new Set(prev);
        for (const id of rootIds) next.add(id);
        return next;
      });
    }
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
          <button className="secondary" onClick={() => onReject(selectedItems)} disabled={locked || selectedItems.length === 0}>
            Reject selected
          </button>
          <button className="primary" onClick={() => onApprove(selectedExecutableItems)} disabled={runDisabled}>
            <Check size={16} />
            {locked ? "Running" : candidatesSearching ? "Searching candidates" : "Run selected"}
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
          Select all
        </label>
        <span>{selectedItems.length} selected</span>
        <TreeToolbar
          expanded={openItems.size > 0}
          onExpand={() => setOpenItems(new Set(batch.items.map((item) => item.id)))}
          onCollapse={() => setOpenItems(new Set())}
        />
      </div>
      {tree.roots.map((item) => (
        <ApprovalNode
          item={item}
          childrenById={tree.childrenById}
          openItems={openItems}
          setOpenItems={setOpenItems}
          onSelection={onSelection}
          onSelectOnly={onSelectOnly}
          onReject={onReject}
          openCandidatePickers={openCandidatePickers}
          setOpenCandidatePickers={setOpenCandidatePickers}
          itemById={itemById}
          key={item.id}
        />
      ))}
    </section>
  );
}

function ApprovalNode({
  item,
  childrenById,
  openItems,
  setOpenItems,
  onSelection,
  onSelectOnly,
  onReject,
  allowBranchDelete = false,
  openCandidatePickers,
  setOpenCandidatePickers,
  depth = 0,
  itemById,
}) {
  const children = childrenById.get(item.id) || [];
  const metadataChanges = metadataChangeRows(item);
  const hasChildren = children.length > 0 || metadataChanges.length > 0;
  const open = openItems.has(item.id);
  const descendantIds = collectItemIds(item, childrenById);
  const leafDownloadCandidate = item.kind === "download" && children.length === 0 && Boolean(item.new_value);
  const siblingCandidates = leafDownloadCandidate ? siblingItems(item, childrenById).filter((sibling) => sibling.kind === item.kind && (sibling.new_value || sibling.old_value)) : [];
  const hasAlternateCandidates = siblingCandidates.length > 1;
  const siblingIds = leafDownloadCandidate ? siblingCandidates.map((sibling) => sibling.id) : descendantIds;
  const pickerOpen = leafDownloadCandidate && hasAlternateCandidates && openCandidatePickers?.has(item.parent_id);
  const firstSelectedSibling = siblingCandidates.find((sibling) => sibling.selected);
  const visibleCandidateId = firstSelectedSibling?.id || siblingCandidates[0]?.id;
  const hiddenAlternateCandidate = leafDownloadCandidate && !pickerOpen && visibleCandidateId && visibleCandidateId !== item.id;
  const statusMeta = itemStatusMeta(item);
  // file_move / delete leaves carry old_value (from) + new_value (to) — show the move.
  const isFileMoveLeaf = (item.kind === "file_move" || item.kind === "delete") && children.length === 0 && Boolean(item.new_value);
  const hasDownloadCandidateChildren = children.some((child) => {
    const grandchildren = childrenById.get(child.id) || [];
    return child.kind === "download" && grandchildren.length === 0 && (child.new_value || child.old_value);
  });
  const downloadProgress = item.kind === "download" && hasDownloadCandidateChildren ? downloadStatusProgressForItem(item) : null;
  if (hiddenAlternateCandidate) return null;

  function updateChecked(checked) {
    if (leafDownloadCandidate && checked) {
      onSelectOnly?.(item.batch_id, siblingIds, item.id);
    } else if (itemById) {
      // Group descendant IDs by their actual batch_id to handle items merged across batches.
      const byBatch = new Map();
      for (const id of descendantIds) {
        const batchId = itemById.get(id)?.batch_id ?? item.batch_id;
        if (!byBatch.has(batchId)) byBatch.set(batchId, []);
        byBatch.get(batchId).push(id);
      }
      for (const [batchId, ids] of byBatch) {
        onSelection(batchId, ids, checked);
      }
    } else {
      onSelection(item.batch_id, descendantIds, checked);
    }
  }

  return (
    <>
      <div className={`proposal-row status-${item.status}`} style={{ "--depth": depth }}>
        <input type="checkbox" checked={item.selected} onChange={(event) => updateChecked(event.target.checked)} />
        <button
          className="row-toggle"
          disabled={!hasChildren}
          onClick={() => toggleSet(setOpenItems, item.id)}
          title={hasChildren ? "Toggle branch" : ""}
        >
          {hasChildren ? (open ? <ChevronDown size={15} /> : <ChevronRight size={15} />) : null}
        </button>
        <span className="proposal-title-cell">
          <span className="proposal-title">{item.title}</span>
          {downloadProgress && (
            <InlineProgress value={downloadProgress.value} label={downloadProgress.label} indeterminate={downloadProgress.indeterminate} compact />
          )}
        </span>
        <small title={isFileMoveLeaf ? `${item.old_value || "?"} → ${item.new_value || "?"}` : undefined}>
          {isFileMoveLeaf
            ? `${shortPath(item.old_value)} → ${shortPath(item.new_value)}`
            : metadataChanges.length > 0 ? `${metadataChanges.length} changes` : leafDownloadCandidate ? candidateMeta(item) : statusMeta}
        </small>
        {leafDownloadCandidate && hasAlternateCandidates && (
          <button
            className="row-icon-button"
            onClick={() => toggleSet(setOpenCandidatePickers, item.parent_id)}
            title={pickerOpen ? "Hide candidates" : "Choose candidate"}
          >
            <Pencil size={14} />
          </button>
        )}
        {allowBranchDelete && !leafDownloadCandidate && (
          <button className="row-icon-button danger" onClick={() => onReject?.([item])} title="Delete branch and files">
            <Trash2 size={14} />
          </button>
        )}
      </div>
      {open &&
        metadataChanges.map((change) => (
          <div className="proposal-row metadata-change-row" style={{ "--depth": depth + 1 }} key={`${item.id}:${change.field}`}>
            <span />
            <span />
            <span className="proposal-title">{change.field}</span>
            <small>{change.oldValue} {"->"} {change.newValue}</small>
          </div>
        ))}
      {open &&
        children.map((child) => (
          <ApprovalNode
            item={child}
            childrenById={childrenById}
            openItems={openItems}
            setOpenItems={setOpenItems}
            onSelection={onSelection}
            onSelectOnly={onSelectOnly}
            onReject={onReject}
            allowBranchDelete={allowBranchDelete}
            openCandidatePickers={openCandidatePickers}
            setOpenCandidatePickers={setOpenCandidatePickers}
            depth={depth + 1}
            itemById={itemById}
            key={child.id}
          />
        ))}
    </>
  );
}

function ImportWizard({
  files,
  onFilesChange,
  library,
  onRecheckTrack,
  onRecheckAlbum,
  onCheckAlbum,
  onSearchAlbums,
  seedDownloadRequests = [],
  albumSearchOpen,
  setAlbumSearchOpen,
  onDownloadRequestsChange,
  addAlbumsRef,
}) {
  const [manualAlbums, setManualAlbums] = useState([]);
  const [albumRecords, setAlbumRecords] = useState({});

  // Expose direct add function so callers can bypass the seed mechanism
  useEffect(() => {
    if (!addAlbumsRef) return;
    addAlbumsRef.current = (albums) => {
      setManualAlbums((current) => mergeManualAlbums(current, albums));
      setAlbumRecords((current) => ({
        ...current,
        ...Object.fromEntries(albums.map((album) => [albumRecordKey(album.artist, album.name), album.tracks])),
      }));
    };
    return () => { if (addAlbumsRef) addAlbumsRef.current = null; };
  });

  const seedKey = useMemo(() => stableDownloadRequestKey(seedDownloadRequests), [seedDownloadRequests]);
  const appliedSeedKey = useRef("");

  const updateDownloadRequests = useCallback((requests) => {
    onDownloadRequestsChange?.(requests);
  }, [onDownloadRequestsChange]);

  function addManualAlbum(album) {
    if (!album?.artist || !album?.name) return;
    setManualAlbums((current) => mergeManualAlbums(current, [album]));
    setAlbumRecords((current) => ({
      ...current,
      [albumRecordKey(album.artist, album.name)]: album.tracks,
    }));
    setAlbumSearchOpen(false);
  }

  useEffect(() => {
    if (!seedKey || appliedSeedKey.current === seedKey) return;
    appliedSeedKey.current = seedKey;
    const albums = manualAlbumsFromDownloadRequests(seedDownloadRequests);
    setManualAlbums((current) => mergeManualAlbums(current, albums));
    setAlbumRecords((current) => ({
      ...current,
      ...Object.fromEntries(albums.map((album) => [albumRecordKey(album.artist, album.name), album.tracks])),
    }));
  }, [seedKey, seedDownloadRequests]);

  useEffect(() => {
    if (files.length === 0 && manualAlbums.length === 0) {
      updateDownloadRequests([]);
    }
  }, [files.length, manualAlbums.length, updateDownloadRequests]);

  function removeManualAlbum(artist, album) {
    setManualAlbums((current) => current.filter((entry) => entry.artist !== artist || entry.name !== album));
    setAlbumRecords((current) => {
      const next = { ...current };
      delete next[albumRecordKey(artist, album)];
      return next;
    });
  }

  function removeManualArtist(artist) {
    setManualAlbums((current) => current.filter((entry) => entry.artist !== artist));
    setAlbumRecords((current) =>
      Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(`${normalizeName(artist)}::`))),
    );
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
      {albumSearchOpen && <AlbumSearchPanel onAdd={addManualAlbum} onLookup={checkAlbum} onSearch={onSearchAlbums} />}
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
          onRecheckAlbum={onRecheckAlbum}
          onCheckAlbum={checkAlbum}
          onRemoveManualAlbum={removeManualAlbum}
          onRemoveManualArtist={removeManualArtist}
          onDownloadRequestsChange={updateDownloadRequests}
          seedDownloadRequests={seedDownloadRequests}
        />
      )}
    </div>
  );
}

function AlbumSearchPanel({ onAdd, onLookup, onSearch, initialArtist = "", initialAlbum = "" }) {
  const [artist, setArtist] = useState(initialArtist);
  const [album, setAlbum] = useState(initialAlbum);
  const [results, setResults] = useState([]);
  const [searched, setSearched] = useState(false);

  async function submit(event) {
    event.preventDefault();
    if (!artist.trim() || !album.trim()) return;
    setSearched(true);
    const searchResults = await onSearch(artist.trim(), album.trim());
    setResults(dedupeAlbumResults(searchResults));
  }

  async function addResult(result) {
    const record = await onLookup(result.artist || artist.trim(), result.title || album.trim(), result.id);
    onAdd({
      id: record?.musicbrainz_album_id || result.id || `manual:${Date.now()}`,
      name: record?.album || result.title || album.trim(),
      artist: record?.artist || result.artist || artist.trim(),
      cover_art_url: result.cover_art_url,
      tracks: record?.tracks || [],
    });
  }

  return (
    <div className="album-search-panel">
      <form className="album-search-fields" onSubmit={submit}>
        <label>
          Artist
          <input value={artist} onChange={(event) => setArtist(event.target.value)} />
        </label>
        <label>
          Album
          <input value={album} onChange={(event) => setAlbum(event.target.value)} />
        </label>
        <button className="primary">
          <Search size={16} />
          Search
        </button>
      </form>
      {searched && (
        <div className="album-results">
          {results.length === 0 ? (
            <p>No album results found.</p>
          ) : (
            results.map((result) => (
              <button className="album-result" key={result.id} onClick={() => addResult(result)}>
                <AlbumResultArt src={result.cover_art_url} />
                <span>
                  <strong>{result.title}</strong>
                  <small>
                    {result.artist} {result.date ? `· ${result.date}` : ""} {result.track_count ? `· ${result.track_count} tracks` : ""}
                  </small>
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function AlbumResultArt({ src }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return (
      <span className="album-result-art placeholder">
        <Music size={19} />
      </span>
    );
  }
  return <img className="album-result-art" src={src} alt="" onError={() => setFailed(true)} />;
}

function dedupeAlbumResults(results = []) {
  const seen = new Set();
  return results.filter((result) => {
    if (!result?.title || !result?.artist) return false;
    const key = `${normalizeName(result.artist)}::${normalizeName(result.title)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function ArtistAvatar({ artist }) {
  const [failed, setFailed] = useState(false);
  if (artist?.image_url && !failed) {
    return <img className="artist-avatar" src={artist.image_url} alt="" onError={() => setFailed(true)} />;
  }
  return <span className="artist-avatar">{initials(artist?.name)}</span>;
}

const DISCOVER_ALBUMS_INITIAL = 5;

function DiscoverView({ user, onSearch, onFetchTracks, onWishlist, onQueue, apiKey }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [searching, setSearching] = useState(false);
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [expandedAllAlbums, setExpandedAllAlbums] = useState(() => new Set());
  const [albumTracksCache, setAlbumTracksCache] = useState(() => new Map());
  const [albumTracksLoading, setAlbumTracksLoading] = useState(() => new Set());
  const canWishlist = hasPermission(user, "discover");
  const canQueue = hasPermission(user, "discover");

  function artUrl(src) {
    // Discover art comes straight from iTunes as an external URL — no auth needed.
    return src || null;
  }

  async function loadAlbumTracks(albumId) {
    if (albumTracksCache.has(albumId) || albumTracksLoading.has(albumId) || !onFetchTracks) return;
    setAlbumTracksLoading((prev) => { const next = new Set(prev); next.add(albumId); return next; });
    try {
      const data = await onFetchTracks(albumId);
      setAlbumTracksCache((prev) => new Map([...prev, [albumId, data.tracks || []]]));
    } catch (_) {
      setAlbumTracksCache((prev) => new Map([...prev, [albumId, []]]));
    } finally {
      setAlbumTracksLoading((prev) => { const next = new Set(prev); next.delete(albumId); return next; });
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!query.trim()) return;
    setSearching(true);
    try {
      const data = await onSearch(query.trim());
      setResults(data);
      setOpenArtists(new Set((data.artists || []).map((artist) => artist.id)));
      setExpandedAllAlbums(new Set());
      // Pre-populate cache with any tracks already in the response (track-type search)
      const preloaded = new Map();
      (data.artists || []).forEach((artist) => {
        (artist.albums || []).forEach((album) => {
          if ((album.tracks || []).length > 0) preloaded.set(album.id, album.tracks);
        });
      });
      setAlbumTracksCache(preloaded);
      setAlbumTracksLoading(new Set());
      const focusAlbum = data.focus?.album_id;
      if (focusAlbum) setOpenAlbums(new Set([focusAlbum]));
    } finally {
      setSearching(false);
    }
  }

  function albumRequests(album) {
    return (album.tracks || []).map((track) => ({
      artist: album.artist,
      album: album.title,
      track: track.title,
      track_number: track.track_number,
      disc_number: track.disc_number,
      duration_ms: track.length || track.duration_ms,
      musicbrainz_album_id: album.id,
      musicbrainz_recording_id: track.musicbrainz_recording_id || track.id,
      date: album.date,
    }));
  }

  async function addAlbumWishlist(album) {
    await onWishlist({ kind: "album", artist: album.artist, album: album.title, track: null, source: "discover" });
  }

  async function addTrackWishlist(album, track) {
    await onWishlist({ kind: "track", artist: album.artist, album: album.title, track: track.title, source: "discover" });
  }

  return (
    <div className="discover-view">
      <form className="discover-search" onSubmit={submit}>
        <Search size={17} />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search artist, album, or track" />
        <button className="primary" disabled={searching || !query.trim()}>
          {searching ? "Searching" : "Search"}
        </button>
      </form>
      {!results ? (
        <EmptyState title="Search MusicBrainz" body="Find an artist, album, or track, then add it to your wishlist or task queue." />
      ) : (results.artists || []).length === 0 ? (
        <EmptyState title="No discover results" body="Try a more specific artist, album, or track title." />
      ) : (
        <div className="tree discover-tree">
          <TreeToolbar
            expanded={openArtists.size > 0 || openAlbums.size > 0}
            onExpand={() => {
              setOpenArtists(new Set((results.artists || []).map((artist) => artist.id)));
              setOpenAlbums(new Set((results.artists || []).flatMap((artist) => (artist.albums || []).map((album) => album.id))));
            }}
            onCollapse={() => {
              setOpenArtists(new Set());
              setOpenAlbums(new Set());
            }}
          />
          {(results.artists || []).map((artist) => (
            <div key={artist.id}>
              <div className="tree-action-row discover-tree-row">
                <TreeRow
                  icon={Sparkles}
                  open={openArtists.has(artist.id)}
                  title={artist.name}
                  meta={artist.disambiguation || `${(artist.albums || []).length} album result${(artist.albums || []).length === 1 ? "" : "s"}`}
                  onToggle={() => toggleSet(setOpenArtists, artist.id)}
                />
                <ArtistAvatar artist={{ ...artist, image_url: artUrl(artist.image_url) }} />
                {canWishlist && (
                  <button className="row-icon-button" onClick={() => onWishlist({ kind: "artist", artist: artist.name, album: null, track: null, source: "discover" })} title="Add artist to wishlist">
                    <Heart size={15} />
                  </button>
                )}
              </div>
              {openArtists.has(artist.id) && (() => {
                const seenAlbumKeys = new Set();
                const allAlbums = (artist.albums || [])
                  .filter((a) => {
                    const key = a.id || `${a.title}|${a.date || ""}|${a.track_count || ""}`;
                    if (seenAlbumKeys.has(key)) return false;
                    seenAlbumKeys.add(key);
                    return true;
                  })
                  .sort((a, b) => (b.track_count || 0) - (a.track_count || 0));
                const showAll = expandedAllAlbums.has(artist.id);
                const visibleAlbums = showAll ? allAlbums : allAlbums.slice(0, DISCOVER_ALBUMS_INITIAL);
                return (
                  <>
                    {visibleAlbums.map((album) => {
                      const tracks = albumTracksCache.get(album.id) ?? album.tracks ?? [];
                      const tracksLoading = albumTracksLoading.has(album.id);
                      return (
                        <div key={album.id}>
                          <div className="tree-action-row discover-tree-row">
                            <TreeRow
                              depth={1}
                              icon={Folder}
                              open={openAlbums.has(album.id)}
                              title={album.title}
                              meta={[album.date, album.track_count ? `${album.track_count} tracks` : null].filter(Boolean).join(" · ")}
                              onToggle={() => {
                                toggleSet(setOpenAlbums, album.id);
                                if (!openAlbums.has(album.id) && tracks.length === 0) loadAlbumTracks(album.id);
                              }}
                            />
                            <AlbumResultArt src={artUrl(album.cover_art_url)} />
                            {canWishlist && (
                              <button className="row-icon-button" onClick={() => addAlbumWishlist(album)} title="Add album to wishlist">
                                <Heart size={15} />
                              </button>
                            )}
                            {canQueue && (
                              <button className="row-icon-button" onClick={async () => {
                                // Always fetch the full track list from the API — search results may
                                // contain only a subset of tracks, so never rely on the display cache.
                                let freshTracks = tracks;
                                if (onFetchTracks) {
                                  const data = await onFetchTracks(album.id);
                                  freshTracks = data.tracks || [];
                                  setAlbumTracksCache((prev) => new Map([...prev, [album.id, freshTracks]]));
                                }
                                onQueue(albumRequests({ ...album, tracks: freshTracks }));
                              }} disabled={tracksLoading} title="Queue album">
                                <ListChecks size={15} />
                              </button>
                            )}
                          </div>
                          {openAlbums.has(album.id) && (
                            <>
                              {tracksLoading && (
                                <div className="tree-action-row discover-tree-row">
                                  <TreeRow depth={2} icon={FileAudio} title="Loading tracks…" />
                                </div>
                              )}
                              {tracks.map((track, index) => (
                                <div className="tree-action-row discover-tree-row" key={`${track.disc_number || 1}:${track.track_number || index}:${track.title}`}>
                                  <TreeRow depth={2} icon={FileAudio} title={`${trackNumberLabel(track)} ${track.title}`} meta={formatDuration(track.length || track.duration_ms)} />
                                  {canWishlist && (
                                    <button className="row-icon-button" onClick={() => addTrackWishlist(album, track)} title="Add track to wishlist">
                                      <Heart size={15} />
                                    </button>
                                  )}
                                  {canQueue && (
                                    <button className="row-icon-button" onClick={() => onQueue(albumRequests({ ...album, tracks: [track] }))} title="Queue track">
                                      <ListChecks size={15} />
                                    </button>
                                  )}
                                </div>
                              ))}
                            </>
                          )}
                        </div>
                      );
                    })}
                    {!showAll && allAlbums.length > DISCOVER_ALBUMS_INITIAL && (
                      <div className="tree-action-row discover-tree-row">
                        <button
                          className="discover-show-more"
                          onClick={() => toggleSet(setExpandedAllAlbums, artist.id)}
                        >
                          Show {allAlbums.length - DISCOVER_ALBUMS_INITIAL} more albums
                        </button>
                      </div>
                    )}
                  </>
                );
              })()}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WishlistView({ wishlist, approvals, user, onAdd, onRemove, onRemoveMany, onSubmit, onSearchAlbums, onLookupAlbum, onInspectorActionsChange }) {
  const [albumSearchOpen, setAlbumSearchOpen] = useState(false);
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [openOwners, setOpenOwners] = useState(() => new Set());
  const [selectedItems, setSelectedItems] = useState(() => new Set());
  const canApproveAll = hasPermission(user, "wishlist:approve_all");
  const tree = useMemo(() => buildWishlistTree(wishlist), [wishlist]);
  const ownerTree = useMemo(() => buildWishlistOwnerTree(wishlist), [wishlist]);
  const wantedItems = useMemo(() => wishlist.filter((item) => item.status === "wanted"), [wishlist]);
  const treeKey = useMemo(
    () =>
      canApproveAll
        ? ownerTree.map((owner) => `${owner.name}:${owner.artists.map((artist) => `${artist.name}:${artist.albums.map((album) => album.name).join(",")}`).join("|")}`).join("|")
        : tree.map((artist) => `${artist.name}:${artist.albums.map((album) => album.name).join(",")}`).join("|"),
    [canApproveAll, ownerTree, tree],
  );

  useEffect(() => {
    setOpenOwners(new Set(ownerTree.map((owner) => owner.id)));
    setOpenArtists(new Set(canApproveAll ? ownerTree.flatMap((owner) => owner.artists.map((artist) => `${owner.id}:${artist.name}`)) : tree.map((artist) => artist.name)));
    setOpenAlbums(
      new Set(
        (canApproveAll ? ownerTree.flatMap((owner) => owner.artists.map((artist) => ({ ownerId: owner.id, artist }))) : tree.map((artist) => ({ ownerId: "", artist }))).flatMap(
          ({ ownerId, artist }) => artist.albums.map((album) => `${ownerId ? `${ownerId}:` : ""}${artist.name}/${album.name}`),
        ),
      ),
    );
    setSelectedItems(new Set(wantedItems.map((item) => item.id)));
  }, [treeKey, wantedItems.length]);

  async function addAlbumToWishlist(album) {
    if (album.tracks?.length) {
      for (const track of album.tracks) {
        await onAdd({ kind: "track", artist: album.artist, album: album.name, track: track.title });
      }
    } else {
      await onAdd({ kind: "album", artist: album.artist, album: album.name });
    }
    setAlbumSearchOpen(false);
  }

  useEffect(() => {
    onInspectorActionsChange?.({
      selectedCount: selectedItems.size,
      canApproveAll,
      onToggleAlbumSearch: () => setAlbumSearchOpen((value) => !value),
      onSubmitSelected: canApproveAll ? () => onSubmit([...selectedItems], { denyUnselected: true }) : null,
    });
    return () => onInspectorActionsChange?.(null);
  }, [selectedItems.size, canApproveAll]);

  return (
    <div className="wishlist-view">
      {albumSearchOpen && <AlbumSearchPanel onAdd={addAlbumToWishlist} onLookup={onLookupAlbum} onSearch={onSearchAlbums} />}
      {wishlist.length === 0 ? (
        <EmptyState title="No wishlist items" body={canApproveAll ? "User requests will appear here for approval." : "Add music here to request it."} />
      ) : (
        <div className="tree">
          <TreeToolbar
            expanded={openArtists.size > 0 || openAlbums.size > 0}
            onExpand={() => {
              setOpenOwners(new Set(ownerTree.map((owner) => owner.id)));
              setOpenArtists(new Set((canApproveAll ? ownerTree.flatMap((owner) => owner.artists.map((artist) => `${owner.id}:${artist.name}`)) : tree.map((artist) => artist.name))));
              setOpenAlbums(
                new Set(
                  (canApproveAll ? ownerTree.flatMap((owner) => owner.artists.map((artist) => ({ ownerId: owner.id, artist }))) : tree.map((artist) => ({ ownerId: "", artist }))).flatMap(
                    ({ ownerId, artist }) => artist.albums.map((album) => `${ownerId ? `${ownerId}:` : ""}${artist.name}/${album.name}`),
                  ),
                ),
              );
            }}
            onCollapse={() => {
              setOpenOwners(new Set());
              setOpenArtists(new Set());
              setOpenAlbums(new Set());
            }}
          />
          {canApproveAll
            ? ownerTree.map((owner) => (
                <div key={owner.id}>
                  <TreeRow
                    icon={Users}
                    open={openOwners.has(owner.id)}
                    title={owner.name}
                    meta={`${owner.itemCount} items`}
                    onToggle={() => toggleSet(setOpenOwners, owner.id)}
                  />
                  {openOwners.has(owner.id) &&
                    owner.artists.map((artist) =>
                      renderWishlistArtist(artist, 1, owner.id, openArtists, setOpenArtists, openAlbums, setOpenAlbums, selectedItems, setSelectedItems, onRemove, onRemoveMany),
                    )}
                </div>
              ))
            : tree.map((artist) => renderWishlistArtist(artist, 0, "", openArtists, setOpenArtists, openAlbums, setOpenAlbums, selectedItems, setSelectedItems, onRemove, onRemoveMany))}
        </div>
      )}
    </div>
  );
}

function renderWishlistArtist(artist, depth, prefix, openArtists, setOpenArtists, openAlbums, setOpenAlbums, selectedItems, setSelectedItems, onRemove, onRemoveMany) {
  const artistId = `${prefix ? `${prefix}:` : ""}${artist.name}`;
  return (
    <div key={`${depth}:${artistId}`}>
      <div className="tree-action-row library-row-actions">
        <TreeRow
          depth={depth}
          icon={Sparkles}
          open={openArtists.has(artistId)}
          title={artist.name}
          meta={`${artist.albums.length} albums`}
          onToggle={() => toggleSet(setOpenArtists, artistId)}
        />
        <button className="row-icon-button" onClick={() => onRemoveMany(artist.itemIds)} title="Remove artist requests">
          <X size={15} />
        </button>
      </div>
      {openArtists.has(artistId) &&
        artist.albums.map((album) => {
          const albumId = `${artistId}/${album.name}`;
          return (
            <div key={albumId}>
              <div className="tree-action-row library-row-actions">
                <TreeRow
                  depth={depth + 1}
                  icon={Folder}
                  open={openAlbums.has(albumId)}
                  title={album.name}
                  meta={wishlistAlbumMeta(album)}
                  onToggle={() => toggleSet(setOpenAlbums, albumId)}
                />
                <button className="row-icon-button" onClick={() => onRemoveMany(album.itemIds)} title="Remove album requests">
                  <X size={15} />
                </button>
              </div>
              {openAlbums.has(albumId) &&
                (album.tracks.length > 0 ? (
                  album.tracks.map((track) => (
                    <div className={`tree-action-row library-row-actions wishlist-row${track.status === "removed" ? " removed" : ""}`} key={track.id}>
                      <TreeRow depth={depth + 2} icon={FileAudio} title={track.track || "Track"} meta={wishlistStatusLabel(track.status)} />
                      <DownloadBranchToggle
                        checked={selectedItems.has(track.id)}
                        disabled={track.status !== "wanted"}
                        onChange={(checked) => toggleWishlistItem(setSelectedItems, track.id, checked)}
                        title="Select wishlist track"
                      />
                      {track.status !== "removed" && (
                        <button className="row-icon-button" onClick={() => onRemove(track.id)} title="Remove track">
                          <X size={15} />
                        </button>
                      )}
                    </div>
                  ))
                ) : (
                  <div className={`tree-action-row library-row-actions wishlist-row${album.request?.status === "removed" ? " removed" : ""}`}>
                    <TreeRow depth={depth + 2} icon={FileAudio} title={album.request?.album || "Full album"} meta={wishlistStatusLabel(album.request?.status || "wanted")} />
                    {album.request && (
                      <DownloadBranchToggle
                        checked={selectedItems.has(album.request.id)}
                        disabled={album.request.status !== "wanted"}
                        onChange={(checked) => toggleWishlistItem(setSelectedItems, album.request.id, checked)}
                        title="Select wishlist request"
                      />
                    )}
                    {album.request && album.request.status !== "removed" && (
                      <button className="row-icon-button" onClick={() => onRemove(album.request.id)} title="Remove request">
                        <X size={15} />
                      </button>
                    )}
                  </div>
                ))}
            </div>
          );
        })}
    </div>
  );
}

function PlaylistsView({ playlists, library, onCreatePlaylist, onAddToPlaylist, onRename, onDelete, onPlay, onQueue, onQueuePosition, onInspectorActionsChange, api }) {
  const [pinnedIds, setPinnedIds] = useState(() => new Set());
  useEffect(() => {
    let active = true;
    api("/me/pinned-playlists")
      .then((rows) => { if (active) setPinnedIds(new Set((rows || []).map((r) => r.playlist_id))); })
      .catch(() => {});
    return () => { active = false; };
  }, [api]);
  async function togglePin(playlist) {
    const isPinned = pinnedIds.has(playlist.id);
    try {
      const rows = isPinned
        ? await api(`/me/pinned-playlists/${encodeURIComponent(playlist.id)}`, { method: "DELETE" })
        : await api("/me/pinned-playlists", { method: "POST", body: JSON.stringify({ playlist_id: playlist.id, name: playlist.name }) });
      setPinnedIds(new Set((rows || []).map((r) => r.playlist_id)));
    } catch {
      /* best-effort */
    }
  }
  const [openPlaylists, setOpenPlaylists] = useState(() => new Set());
  const [addOpen, setAddOpen] = useState(null);
  const [editOpen, setEditOpen] = useState(null);
  const [playlistName, setPlaylistName] = useState("");
  const [playlistDraftName, setPlaylistDraftName] = useState("");
  const [playlistSearch, setPlaylistSearch] = useState("");
  const [draftPositions, setDraftPositions] = useState({});

  const positionKey = useMemo(() => playlists.map((playlist) => `${playlist.id}:${playlist.track_count}`).join("|"), [playlists]);
  useEffect(() => {
    setDraftPositions(
      Object.fromEntries(playlists.flatMap((playlist) => playlist.tracks.map((track) => [track.id, String(track.position || "")]))),
    );
  }, [positionKey]);

  function updateDraft(entryId, value) {
    setDraftPositions((current) => ({ ...current, [entryId]: value }));
  }

  async function submitPosition(track) {
    const nextPosition = Number.parseInt(draftPositions[track.id], 10);
    if (!Number.isFinite(nextPosition) || nextPosition < 1 || nextPosition === track.position) {
      updateDraft(track.id, String(track.position || ""));
      return;
    }
    try {
      await onQueuePosition(track.id, nextPosition);
    } catch {
    }
    updateDraft(track.id, String(track.position || ""));
  }

  useEffect(() => {
    onInspectorActionsChange?.({
      playlistName,
      onPlaylistNameChange: setPlaylistName,
      onCreate: () => {
        if (!playlistName.trim()) return;
        onCreatePlaylist(playlistName.trim()).then(() => setPlaylistName("")).catch(() => {});
      },
    });
    return () => onInspectorActionsChange?.(null);
  // onInspectorActionsChange is a stable state setter; onCreatePlaylist is a stable App function
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playlistName, onInspectorActionsChange]);

  return (
    <div className="playlist-view">
      <TreeToolbar
        expanded={openPlaylists.size > 0}
        onExpand={() => setOpenPlaylists(new Set(playlists.map((playlist) => playlist.name)))}
        onCollapse={() => setOpenPlaylists(new Set())}
      />
      {playlists.map((playlist) => {
        const tracks = playlist.tracks || [];
        const playableTracks = tracks.map(playlistPlayableTrack);
        return (
          <div key={playlist.id}>
            <div className="tree-action-row library-row-actions">
              <TreeRow
                icon={playlist.protected ? Heart : FileAudio}
                open={openPlaylists.has(playlist.name)}
                title={playlist.name}
                meta={`${playlist.track_count || 0} tracks`}
                onToggle={() => toggleSet(setOpenPlaylists, playlist.name)}
              />
              <PlaylistPlayActions
                disabled={playableTracks.length === 0}
                onPlay={() => onPlay(playableTracks, { keepLead: false })}
                onQueue={() => onQueue(playableTracks)}
              />
              {!playlist.protected && (
                <button
                  className={`row-icon-button${pinnedIds.has(playlist.id) ? " active" : ""}`}
                  onClick={() => togglePin(playlist)}
                  title={pinnedIds.has(playlist.id) ? "Unpin from Home" : "Pin to Home"}
                >
                  {pinnedIds.has(playlist.id) ? <PinOff size={14} /> : <Pin size={14} />}
                </button>
              )}
              <button className="row-icon-button" onClick={() => setAddOpen(addOpen === playlist.id ? null : playlist.id)} title="Add music">
                <Plus size={14} />
              </button>
              <button
                className="row-icon-button"
                onClick={() => {
                  setEditOpen(editOpen === playlist.id ? null : playlist.id);
                  setPlaylistDraftName(playlist.name);
                }}
                title="Edit playlist"
              >
                <Pencil size={14} />
              </button>
            </div>
            {editOpen === playlist.id && (
              <PlaylistEditPanel
                playlist={playlist}
                draftName={playlistDraftName}
                setDraftName={setPlaylistDraftName}
                onRename={() => onRename(playlist.id, playlistDraftName.trim()).then(() => setEditOpen(null)).catch(() => {})}
                onDelete={() => onDelete(playlist.id).then(() => setEditOpen(null)).catch(() => {})}
              />
            )}
            {addOpen === playlist.id && (
              <PlaylistAddPanel
                library={library}
                search={playlistSearch}
                setSearch={setPlaylistSearch}
                onAdd={(trackIds) => onAddToPlaylist(playlist.id, trackIds)}
              />
            )}
            {openPlaylists.has(playlist.name) &&
              (tracks.length === 0 ? (
                <EmptyState title="No playlist tracks" body="Add tracks to populate this playlist." />
              ) : (
                <div className="playlist-track-tree">
                  {tracks.map((track) => (
                    <div className="tree-action-row library-row-actions" key={track.id}>
                      <TreeRow
                        depth={1}
                        icon={FileAudio}
                        title={track.title}
                        meta={[track.artist, track.album, track.format].filter(Boolean).join(" / ")}
                      />
                      <PlaylistPlayActions
                        onPlay={() => onPlay([playlistPlayableTrack(track)])}
                        onQueue={() => onQueue([playlistPlayableTrack(track)])}
                      />
                    </div>
                  ))}
                </div>
              ))}
          </div>
        );
      })}
    </div>
  );
}

function PlaylistEditPanel({ playlist, draftName, setDraftName, onRename, onDelete }) {
  const protectedPlaylist = playlist.protected;
  return (
    <div className="album-search-panel playlist-edit-panel">
      <label>
        Name
        <input value={draftName} onChange={(event) => setDraftName(event.target.value)} disabled={protectedPlaylist} />
      </label>
      <div className="playlist-edit-actions">
        <button className="primary compact" onClick={onRename} disabled={protectedPlaylist || !draftName.trim() || draftName.trim() === playlist.name}>
          <ListChecks size={15} />
          Rename
        </button>
        {!protectedPlaylist && (
          <button className="secondary compact danger" onClick={onDelete}>
            <Trash2 size={15} />
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

function PlaylistAddPanel({ library, search, setSearch, onAdd }) {
  const results = useMemo(() => searchLibraryTargets(library, search), [library, search]);
  return (
    <div className="album-search-panel playlist-add-panel">
      <label>
        Search library
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Song, artist, or album" />
      </label>
      <div className="album-results">
        {results.map((result) => (
          <button className="album-result" key={result.id} onClick={() => onAdd(result.trackIds)}>
            <span>
              <strong>{result.title}</strong>
              <small>{result.meta}</small>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function PlaylistPlayActions({ disabled = false, onPlay, onQueue }) {
  return (
    <div className="playlist-play-actions">
      <button className="row-icon-button" onClick={onPlay} disabled={disabled} title="Play">
        <Play size={14} />
      </button>
      <QueueButton onClick={onQueue} disabled={disabled} title="Add to local queue" />
    </div>
  );
}

function ImportTree({
  files,
  onFilesChange,
  library,
  manualAlbums,
  albumRecords,
  onRecheckTrack,
  onRecheckAlbum,
  onCheckAlbum,
  onRemoveManualAlbum,
  onRemoveManualArtist,
  onDownloadRequestsChange,
  seedDownloadRequests = [],
}) {
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [openAlbumDetails, setOpenAlbumDetails] = useState(() => new Set());
  const [draggedAlbum, setDraggedAlbum] = useState(null);
  const [draggedTrack, setDraggedTrack] = useState(null);
  const [selectedTracks, setSelectedTracks] = useState(() => new Set());
  const [downloadSelections, setDownloadSelections] = useState(() => new Set());
  const [dismissedGhosts, setDismissedGhosts] = useState(() => new Set());
  const [extraGhosts, setExtraGhosts] = useState({});
  const grouped = useMemo(() => groupImportFiles(files, library, manualAlbums, albumRecords), [files, library, manualAlbums, albumRecords]);
  const seedKey = useMemo(() => stableDownloadRequestKey(seedDownloadRequests), [seedDownloadRequests]);
  const appliedSeedKey = useRef("");
  const manualDownloadKey = useMemo(
    () => manualAlbums.map((album) => `${album.artist}/${album.name}:${(album.tracks || []).length}`).join("|"),
    [manualAlbums],
  );
  const appliedManualDownloadKey = useRef("");

  useEffect(() => {
    setOpenArtists(new Set(grouped.map((artist) => artist.name)));
    setOpenAlbums(new Set(grouped.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
  }, [files.length, manualAlbums.length]);

  useEffect(() => {
    emitDownloadRequests(downloadSelections, dismissedGhosts, extraGhosts);
  }, [grouped, downloadSelections, dismissedGhosts, extraGhosts, onDownloadRequestsChange]);

  useEffect(() => {
    if (!seedKey || appliedSeedKey.current === seedKey) return;
    const selected = selectedSlotIdsForRequests(grouped, seedDownloadRequests);
    if (selected.size === 0) return;
    appliedSeedKey.current = seedKey;
    setDownloadSelections(selected);
    emitDownloadRequests(selected);
  }, [seedKey, grouped, seedDownloadRequests]);

  useEffect(() => {
    if (!manualDownloadKey || appliedManualDownloadKey.current === manualDownloadKey) return;
    const manualSlotIds = new Set();
    grouped.forEach((artist) => {
      artist.albums.forEach((album) => {
        if (!album.manual) return;
        album.slots.forEach((slot) => {
          if (!slot.file && !slot.in_library && !dismissedGhosts.has(slot.id)) manualSlotIds.add(slot.id);
        });
      });
    });
    if (manualSlotIds.size === 0) return;
    appliedManualDownloadKey.current = manualDownloadKey;
    setDownloadSelections((current) => {
      const next = new Set(current);
      manualSlotIds.forEach((id) => next.add(id));
      emitDownloadRequests(next);
      return next;
    });
  }, [manualDownloadKey, grouped, dismissedGhosts]);

  function emitDownloadRequests(selections = downloadSelections, dismissed = dismissedGhosts, extra = extraGhosts) {
    onDownloadRequestsChange?.(buildImportDownloadRequests(grouped, selections, dismissed, extra));
  }

  function setSingleDownloadSelection(id, checked) {
    setDownloadSelections((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      emitDownloadRequests(next);
      return next;
    });
  }

  function setSlotDownloadSelections(slots, checked) {
    setDownloadSelections((current) => {
      const next = new Set(current);
      slots.forEach((slot) => {
        if (checked) next.add(slot.id);
        else next.delete(slot.id);
      });
      emitDownloadRequests(next);
      return next;
    });
  }

  function dismissGhost(id) {
    setDismissedGhosts((current) => {
      const next = new Set(current);
      next.add(id);
      emitDownloadRequests(downloadSelections, next);
      return next;
    });
  }

  return (
    <div className="tree">
      {grouped.length > 0 && (
        <TreeToolbar
          expanded={openArtists.size > 0 || openAlbums.size > 0}
          onExpand={() => {
            setOpenArtists(new Set(grouped.map((artist) => artist.name)));
            setOpenAlbums(new Set(grouped.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
          }}
          onCollapse={() => {
            setOpenArtists(new Set());
            setOpenAlbums(new Set());
          }}
        />
      )}
      {(() => {
        const renderedPlaylists = new Set();
        const sortedGrouped = [
          ...grouped.filter((a) => a.playlistName).sort((a, b) =>
            a.playlistName !== b.playlistName
              ? a.playlistName.localeCompare(b.playlistName)
              : a.name.localeCompare(b.name),
          ),
          ...grouped.filter((a) => !a.playlistName),
        ];
        return sortedGrouped.flatMap((artist) => {
          const elements = [];
          if (artist.playlistName && !renderedPlaylists.has(artist.playlistName)) {
            renderedPlaylists.add(artist.playlistName);
            elements.push(
              <div key={`playlist:${artist.playlistName}`} className="tree-playlist-header">
                <ListMusic size={15} />
                <span>{artist.playlistName}</span>
              </div>,
            );
          }
          const visibleAlbums = artist.albums.filter((album) =>
            album.slots.some((slot) => slot.file || !dismissedGhosts.has(slot.id)),
          );
          if (visibleAlbums.length === 0) return elements;
          elements.push(
          <div key={`${artist.playlistName || ""}:${artist.name}`} className={artist.playlistName ? "tree-playlist-artist" : undefined}>
            <div
              onDragOver={(event) => event.preventDefault()}
              onDrop={() => {
                if (draggedAlbum) {
                  updateImportAlbum(files, onFilesChange, draggedAlbum.artist, draggedAlbum.album, { artist: artist.name, albumartist: artist.name });
                  setDraggedAlbum(null);
                }
              }}
            >
              <div className="tree-action-row one-action">
                <SelectableTreeRow
                  icon={Folder}
                  open={openArtists.has(artist.name)}
                  title={artist.name}
                  meta={`${artist.count} files`}
                  onToggle={() => toggleSet(setOpenArtists, artist.name)}
                  control={
                    <DownloadBranchToggle
                      checked={artistGhostSlots(artist, dismissedGhosts).every((slot) => downloadSelections.has(slot.id))}
                      disabled={artistGhostSlots(artist, dismissedGhosts).length === 0}
                      onChange={(checked) => setSlotDownloadSelections(artistGhostSlots(artist, dismissedGhosts), checked)}
                      title="Select downloads for this artist"
                    />
                  }
                />
                <button
                  className="row-icon-button"
                  onClick={() => {
                    removeImportArtist(files, onFilesChange, artist.name);
                    onRemoveManualArtist(artist.name);
                    setExtraGhosts((current) =>
                      Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(`${artist.name}/`))),
                    );
                  }}
                  title="Remove from this scan"
                >
                  <X size={15} />
                </button>
              </div>
            </div>
            {openArtists.has(artist.name) &&
              visibleAlbums.map((album) => {
              const albumId = `${artist.name}/${album.name}`;
              const albumSlots = [...album.slots, ...(extraGhosts[albumId] || [])];
              const visibleSlots = albumSlots.filter((slot) => slot.file || slot.in_library || !dismissedGhosts.has(slot.id));
              const downloadableSlots = visibleSlots.filter((slot) => !slot.file && !slot.in_library);
              return (
                <div key={albumId}>
                  <div
                    draggable
                    onDragStart={() => setDraggedAlbum({ artist: artist.name, album: album.name })}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={() => {
                      if (draggedTrack) {
                        moveTrackPaths(files, onFilesChange, draggedTrack.paths, {
                          artist: artist.name,
                          albumartist: artist.name,
                          album: album.name,
                        });
                        setDraggedTrack(null);
                      } else if (draggedAlbum) {
                        mergeAlbumIntoAlbum(files, onFilesChange, draggedAlbum, { artist: artist.name, album: album.name, slots: album.slots });
                        setDraggedAlbum(null);
                      }
                    }}
                  >
                    <div className="tree-action-row">
                      <SelectableTreeRow
                        depth={1}
                        icon={Folder}
                        open={openAlbums.has(albumId)}
                        title={album.name}
                        meta={`${album.files.length}/${album.slots.length} matched · ${album.matchStatus}`}
                        warning={album.matchStatus === "partial"}
                        onToggle={() => toggleSet(setOpenAlbums, albumId)}
                        control={
                          <DownloadBranchToggle
                            checked={downloadableSlots.length > 0 && downloadableSlots.every((slot) => downloadSelections.has(slot.id))}
                            disabled={downloadableSlots.length === 0}
                            onChange={(checked) => setSlotDownloadSelections(downloadableSlots, checked)}
                            title="Select downloads for this album"
                          />
                        }
                      />
                      <button className="row-icon-button" onClick={() => onCheckAlbum(artist.name, album.name)} title="Check album records">
                        <Search size={15} />
                      </button>
                      <button className="row-icon-button" onClick={() => onRecheckAlbum(album)} title="Check album tracks with MusicBrainz">
                        <Sparkles size={15} />
                      </button>
                      <button className="row-icon-button" onClick={() => toggleSet(setOpenAlbumDetails, albumId)} title="Album details">
                        <Pencil size={15} />
                      </button>
                      <button
                        className="row-icon-button"
                        onClick={() => {
                          removeImportAlbum(files, onFilesChange, artist.name, album.name);
                          onRemoveManualAlbum(artist.name, album.name);
                          setExtraGhosts((current) => {
                            const next = { ...current };
                            delete next[albumId];
                            return next;
                          });
                        }}
                        title="Remove from this scan"
                      >
                        <X size={15} />
                      </button>
                    </div>
                  </div>
                  {openAlbumDetails.has(albumId) && (
                    <AlbumDetails
                      artist={artist.name}
                      album={album.name}
                      coverUrl={album.cover_art_url}
                      details={{ status: album.matchStatus, tracks: albumSlots.length }}
                      onAddGhost={() =>
                        setExtraGhosts((current) => {
                          const currentSlots = current[albumId] || [];
                          const nextNumber = albumSlots.length + 1;
                          return {
                            ...current,
                            [albumId]: [
                              ...currentSlots,
                              {
                                id: `${albumId}:manual:${nextNumber}`,
                                track_number: nextNumber,
                                disc_number: 1,
                                title: `Track ${nextNumber}`,
                                reason: "Manual slot",
                              },
                            ],
                          };
                        })
                      }
                    />
                  )}
                  {openAlbums.has(albumId) &&
                    visibleSlots.map((slot) =>
                      slot.file ? (
                        <ImportTrackRow
                          file={slot.file}
                          album={album}
                          selected={selectedTracks.has(slot.file.path)}
                          onClick={(event) => toggleTrackSelection(setSelectedTracks, slot.file.path, event.shiftKey)}
                          onDragStart={() => setDraggedTrack({ paths: dragPathsForTrack(selectedTracks, slot.file.path) })}
                          onChange={(patch) => updateImportFile(files, onFilesChange, slot.file.path, patch)}
                          onRecheck={() => onRecheckTrack(slot.file)}
                          key={slot.file.path}
                        />
                      ) : slot.in_library ? (
                        <LibraryTrackRow
                          key={`lib:${albumId}:${slot.disc_number || 1}:${slot.track_number}:${slot.title}`}
                          slot={slot}
                        />
                      ) : (
                        <GhostTrackRow
                          key={`${albumId}:${slot.disc_number || 1}:${slot.track_number}:${slot.title}`}
                          slot={slot}
                          checked={downloadSelections.has(slot.id)}
                          onChecked={(checked) => setSingleDownloadSelection(slot.id, checked)}
                          onDismiss={() => dismissGhost(slot.id)}
                          onDrop={() => {
                            if (draggedTrack) {
                              if (draggedTrack.paths.length > 1) {
                                moveTrackPaths(files, onFilesChange, draggedTrack.paths, {
                                  artist: artist.name,
                                  albumartist: artist.name,
                                  album: album.name,
                                });
                              } else {
                                const primaryPath = draggedTrack.paths[0];
                                const draggedFile = files.find((file) => file.path === primaryPath);
                                moveTrackPaths(files, onFilesChange, draggedTrack.paths, {
                                  artist: artist.name,
                                  albumartist: artist.name,
                                  album: album.name,
                                  track_number: slot.track_number,
                                  title: titleForDroppedSlot(slot, draggedFile),
                                });
                              }
                              setDraggedTrack(null);
                            } else if (draggedAlbum) {
                              mergeAlbumIntoAlbum(files, onFilesChange, draggedAlbum, { artist: artist.name, album: album.name, slots: album.slots });
                              setDraggedAlbum(null);
                            }
                          }}
                        />
                      ),
                    )}
                </div>
              );
            })}
          </div>,
          );
          return elements;
        });
      })()}
    </div>
  );
}

function ImportTrackRow({ file, album, selected, onClick, onChange, onDragStart, onRecheck }) {
  const metadata = file.metadata || {};
  const [editing, setEditing] = useState(false);
  return (
    <>
      <div className={selected ? "import-edit-row selected" : "import-edit-row"} draggable onClick={onClick} onDragStart={onDragStart}>
        <GripVertical className="grip" size={16} />
        <FileAudio size={17} />
        <DraftInput value={metadata.artist || ""} onCommit={(value) => onChange({ artist: value, albumartist: value })} />
        <DraftInput value={metadata.album || ""} onCommit={(value) => onChange({ album: value })} />
        <DraftInput
          value={metadata.track_number || ""}
          onCommit={(value) => onChange({ track_number: parseInt(value, 10) || null })}
        />
        <DraftInput value={metadata.title || ""} onCommit={(value) => onChange({ title: value })} />
        <small>{metadata.musicbrainz_match ? `MusicBrainz ${metadata.musicbrainz_match}${metadata.musicbrainz_score ? ` ${metadata.musicbrainz_score}%` : ""}` : album?.matchStatus === "full" ? "In library" : formatBytes(file.size_bytes)}</small>
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

function AlbumDetails({ artist, album, coverUrl, details = {}, onAddGhost }) {
  const [artFailed, setArtFailed] = useState(false);
  return (
    <div className="album-details">
      <div className="album-art">{coverUrl && !artFailed ? <img src={coverUrl} alt="" onError={() => setArtFailed(true)} /> : <Music size={24} />}</div>
      <div className="album-detail-grid">
        <label>Artist</label>
        <strong>{artist}</strong>
        <label>Album</label>
        <strong>{album}</strong>
        {Object.entries(details).map(([key, value]) => (
          <React.Fragment key={key}>
            <label>{key}</label>
            <span>{String(value ?? "")}</span>
          </React.Fragment>
        ))}
      </div>
      {onAddGhost && (
        <button className="secondary" onClick={onAddGhost}>
          <Plus size={15} />
          Add ghost track
        </button>
      )}
    </div>
  );
}

function LibraryMetadataEditor({
  targetType,
  targetId,
  title,
  coverUrl,
  fields,
  details = {},
  onAutoLookup,
  onSearchAlbums,
  onCoverUpload,
  playlists = [],
  targetTrackIds = [],
  onAddToPlaylist,
  onVerifyAudio,
  onRequeue,
  onRemove,
  onQueue,
  onClose,
}) {
  const [draft, setDraft] = useState(() => initialFieldValues(fields));
  const [baseline, setBaseline] = useState(() => initialFieldValues(fields));
  const [artFailed, setArtFailed] = useState(false);
  const [audioCheckLoading, setAudioCheckLoading] = useState(false);
  const [openInfo, setOpenInfo] = useState(() => new Set());
  const coverUploadRef = useRef(null);

  useEffect(() => {
    const fresh = initialFieldValues(fields);
    setDraft(fresh);
    setBaseline(fresh);
  }, [targetId]);

  // Library metadata edits apply directly when a field loses focus. The committed
  // values become the new baseline so the same field isn't re-submitted on later blurs.
  async function commit(nextDraft) {
    const pending = Object.fromEntries(
      Object.entries(nextDraft).filter(([key, value]) => String(value ?? "") !== String(baseline[key] ?? "")),
    );
    if (Object.keys(pending).length === 0) return;
    try {
      await onQueue(targetType, targetId, normalizeEntityChanges(pending, fields));
      setBaseline(nextDraft);
    } catch {
      // onQueue surfaces its own error; keep the draft so the user can retry.
    }
  }

  async function runAudioCheck() {
    if (!onVerifyAudio) return;
    setAudioCheckLoading(true);
    await onVerifyAudio();
    setAudioCheckLoading(false);
  }

  return (
    <div className="album-details metadata-panel">
      {coverUrl !== undefined && <div className="album-art">{coverUrl && !artFailed ? <img src={coverUrl} alt="" onError={() => setArtFailed(true)} /> : <Music size={24} />}</div>}
      <div className="library-metadata-form">
        <strong>{title}</strong>
        {Object.entries(details).map(([key, value]) => (
          <small key={key}>
            {key}: {String(value ?? "")}
          </small>
        ))}
        <div className="metadata-field-grid">
          {fields.map((field) => {
            const isChanged = String(draft[field.key] ?? "") !== String(baseline[field.key] ?? "");
            return (
              <label className={isChanged ? "changed" : ""} key={field.key}>
                <span>
                  {field.label}
                  {field.info && (
                    <button
                      type="button"
                      className="field-info-button"
                      title={field.info}
                      aria-label={`About ${field.label}`}
                      onClick={() => toggleSet(setOpenInfo, field.key)}
                    >
                      <Info size={13} />
                    </button>
                  )}
                </span>
                <div className="metadata-input-action">
                  {field.type === "boolean" ? (
                    <input
                      type="checkbox"
                      checked={Boolean(draft[field.key])}
                      disabled={field.readOnly}
                      onChange={(event) => {
                        if (field.readOnly) return;
                        const next = { ...draft, [field.key]: event.target.checked };
                        setDraft(next);
                        commit(next);
                      }}
                    />
                  ) : (
                    <input
                      type={field.type === "number" ? "number" : "text"}
                      value={draft[field.key] ?? ""}
                      readOnly={field.readOnly}
                      onChange={(event) => field.readOnly ? undefined : setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
                      onBlur={field.readOnly ? undefined : () => commit(draft)}
                    />
                  )}
                  {field.key === "cover_path" && onCoverUpload && (
                    <>
                      <button
                        className="row-icon-button"
                        type="button"
                        onClick={() => coverUploadRef.current?.click()}
                        title="Upload image"
                      >
                        <Upload size={14} />
                      </button>
                      <input
                        ref={coverUploadRef}
                        type="file"
                        accept="image/*"
                        style={{ display: "none" }}
                        onChange={(event) => {
                          const file = event.target.files?.[0];
                          event.target.value = "";
                          if (file) onCoverUpload(file);
                        }}
                      />
                    </>
                  )}
                </div>
                {field.info && openInfo.has(field.key) && (
                  <small className="field-info-text">{field.info}</small>
                )}
              </label>
            );
          })}
        </div>
        {(onVerifyAudio || onRequeue || onRemove || (playlists.length > 0 && targetTrackIds.length > 0)) && (
          <div className="metadata-menu-actions">
            {onVerifyAudio && (
              <button className="secondary compact" onClick={runAudioCheck} disabled={audioCheckLoading}>
                <FileAudio size={15} />
                {audioCheckLoading ? "Checking…" : "Check audio"}
              </button>
            )}
            {onRequeue && (
              <button className="secondary compact" onClick={onRequeue} title="Queue a replacement download">
                <RefreshCw size={15} />
                Replace
              </button>
            )}
            {playlists.length > 0 && targetTrackIds.length > 0 && (
              <select
                defaultValue=""
                onChange={(event) => {
                  if (!event.target.value) return;
                  onAddToPlaylist?.(event.target.value, targetTrackIds);
                  event.target.value = "";
                }}
              >
                <option value="">Add to playlist</option>
                {playlists.map((playlist) => (
                  <option key={playlist.id} value={playlist.id}>
                    {playlist.name}
                  </option>
                ))}
              </select>
            )}
            {onRemove && (
              <button className="secondary compact danger" onClick={onRemove}>
                <Trash2 size={15} />
                Remove
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function initialFieldValues(fields) {
  return Object.fromEntries(fields.map((field) => [field.key, field.value ?? ""]));
}

const SORT_NAME_INFO =
  "An optional alternate spelling used only for alphabetical sorting — e.g. \"Beatles, The\" for \"The Beatles\". Leave blank to sort by the displayed name.";
const MB_ID_INFO =
  "MusicBrainz's unique identifier for this record. It links the entry to MusicBrainz so metadata, artwork, and matching stay accurate. Usually filled automatically; only change it if you know the correct ID.";
const REPLAYGAIN_INFO =
  "Volume adjustment in dB applied at playback so tracks sound equally loud (ReplayGain, -18 LUFS reference). Negative values quieten loud tracks. Non-destructive — the audio file isn't changed. Measured by the \"Apply ReplayGain\" tool; clear to disable for this track.";
const ALBUM_ARTIST_MOVE_INFO =
  "Type a different artist name to move this whole album to that artist. The artist is created if it doesn't exist, the files are moved into its folder, and the old artist is removed if it ends up empty. Use this to fix a mis-attributed album.";
const TRACK_ARTIST_MOVE_INFO =
  "Type a different artist name to move just this track to that artist (under an album of the same title, created if needed). The file is moved and any emptied album/artist is removed. Use this to fix a single mis-filed song.";

function artistFields(artist) {
  return [
    { key: "name", label: "Name", value: artist.name },
    { key: "sort_name", label: "Sort name", value: artist.sort_name, info: SORT_NAME_INFO },
    { key: "musicbrainz_id", label: "MusicBrainz ID", value: artist.musicbrainz_id, info: MB_ID_INFO },
    { key: "cover_path", label: "Cover art", value: artist.cover_path },
  ];
}

async function artistAutoLookup(field, draft, artistId, onCoverSearch) {
  // Artist cover "Auto lookup" hits Deezer (keyless) the same way Check Artist Covers
  // does; apply downloads the URL into the artist folder.
  if (field === "cover_path" && onCoverSearch && artistId) {
    const found = await onCoverSearch(artistId);
    if (found?.cover_path) return { cover_path: found.cover_path };
  }
  return null;
}

function albumFields(album) {
  return [
    { key: "title", label: "Album", value: album.title },
    { key: "artist", label: "Artist", value: album.artist_name, info: ALBUM_ARTIST_MOVE_INFO },
    { key: "sort_name", label: "Sort name", value: album.sort_name, info: SORT_NAME_INFO },
    { key: "release_title", label: "Release title", value: album.release_title },
    { key: "musicbrainz_release_id", label: "MusicBrainz release ID", value: album.musicbrainz_release_id, info: MB_ID_INFO },
    { key: "musicbrainz_release_group_id", label: "MusicBrainz release group ID", value: album.musicbrainz_release_group_id, info: MB_ID_INFO },
    { key: "cover_path", label: "Cover art", value: album.cover_path },
    { key: "path", label: "Path", value: album.path, readOnly: true },
  ];
}

function trackFields(track) {
  return [
    { key: "title", label: "Title", value: track.title },
    { key: "artist", label: "Artist", value: track.artist_name, info: TRACK_ARTIST_MOVE_INFO },
    { key: "track_number", label: "Track number", value: track.track_number, type: "number" },
    { key: "disc_number", label: "Disc number", value: track.disc_number, type: "number" },
    { key: "duration_ms", label: "Duration ms", value: track.duration_ms, type: "number", readOnly: true },
    { key: "format", label: "Format", value: track.format },
    { key: "bitrate", label: "Bitrate", value: track.bitrate, type: "number", readOnly: true },
    { key: "path", label: "Path", value: track.path, readOnly: true },
    { key: "musicbrainz_recording_id", label: "MusicBrainz recording ID", value: track.musicbrainz_recording_id, info: MB_ID_INFO },
    { key: "replaygain_track_gain", label: "ReplayGain (dB)", value: track.replaygain_track_gain, type: "number", info: REPLAYGAIN_INFO },
    { key: "explicit", label: "Explicit", value: track.explicit, type: "boolean" },
    { key: "is_lossless", label: "Lossless", value: track.is_lossless, type: "boolean", readOnly: true },
    { key: "musicbrainz_verified", label: "MusicBrainz verified", value: track.musicbrainz_verified, type: "boolean", readOnly: true },
    { key: "metadata_locked", label: "Metadata locked", value: track.metadata_locked, type: "boolean" },
    { key: "artwork_locked", label: "Artwork locked", value: track.artwork_locked, type: "boolean" },
    { key: "filename_locked", label: "Filename locked", value: track.filename_locked, type: "boolean" },
  ];
}

async function albumAutoLookup(field, draft, artistName, onCheckAlbum, albumId, onCoverSearch) {
  const releaseId = draft.musicbrainz_release_id || null;
  if (field === "cover_path") {
    // Search MusicBrainz + iTunes the same way the Check Album Covers tool does
    // (works even without a stored release id); apply downloads it to the library.
    if (onCoverSearch && albumId) {
      const found = await onCoverSearch(albumId);
      if (found?.cover_path) return { cover_path: found.cover_path };
    }
    if (releaseId) return { cover_path: `https://coverartarchive.org/release/${releaseId}/front-250` };
    return null;
  }
  if (!releaseId) return null;
  const lookup = await onCheckAlbum(artistName, draft.title || draft.release_title || "", releaseId);
  if (!lookup) return null;
  if (field === "title" || field === "release_title") {
    return { [field]: lookup.album };
  }
  return null;
}

async function trackAutoLookup(field, draft, artistName, albumTitle, onCheckAlbum) {
  const lookup = await onCheckAlbum(artistName, albumTitle, null);
  const match = lookup?.tracks?.find((track) => track.track_number === Number(draft.track_number)) || lookup?.tracks?.find((track) => track.title === draft.title);
  if (!match) return null;
  if (field === "title") return { title: match.title };
  if (field === "track_number") return { track_number: match.track_number };
  if (field === "disc_number") return { disc_number: match.disc_number };
  if (field === "duration_ms") return { duration_ms: match.length };
  if (field === "musicbrainz_recording_id") return { musicbrainz_recording_id: match.musicbrainz_recording_id };
  return null;
}

function metadataPatchFromAlbum(targetType, draft, album) {
  if (targetType === "album") {
    return {
      title: album.name,
      release_title: album.name,
      musicbrainz_release_id: album.id,
      cover_path: album.cover_art_url,
    };
  }
  if (targetType === "track") {
    const trackNumber = Number(draft.track_number);
    const match = album.tracks?.find((track) => track.track_number === trackNumber) || album.tracks?.[0];
    if (!match) return {};
    return {
      title: match.title,
      track_number: match.track_number,
      disc_number: match.disc_number,
      duration_ms: match.length,
      musicbrainz_recording_id: match.musicbrainz_recording_id,
    };
  }
  return {};
}

function artistTracks(artist) {
  return artist.albums.flatMap((album) => albumTracks(artist, album));
}

function albumTracks(artist, album) {
  return album.tracks.map((track) => hydrateTrack(track, artist, album));
}

function hydrateTrack(track, artist, album) {
  return {
    ...track,
    album_id: track.album_id ?? album.id,
    _artist: artist.name,
    _album: album.title,
    _coverUrl: album._coverUrl || album.cover_path,
  };
}

let coverCacheBust = 0;

function albumCoverUrl(album, apiKey) {
  const coverPath = album?.cover_path || "";
  if (!coverPath) return "";
  if (/^(https?:|data:|blob:)/i.test(coverPath) || coverPath.startsWith(`${API_BASE}/`)) {
    return coverPath;
  }
  if (!apiKey || !album?.id) return "";
  return `${API_BASE}/library/albums/${album.id}/cover?api_key=${encodeURIComponent(apiKey)}${coverCacheBust ? `&_cb=${coverCacheBust}` : ""}`;
}

function playerCoverUrl(track, apiKey) {
  const c = track?._coverUrl || "";
  if (/^(https?:|data:|blob:)/i.test(c) || c.startsWith(`${API_BASE}/`)) return c;
  if (track?.album_id && apiKey) {
    return `${API_BASE}/library/albums/${encodeURIComponent(track.album_id)}/cover?api_key=${encodeURIComponent(apiKey)}${coverCacheBust ? `&_cb=${coverCacheBust}` : ""}`;
  }
  return c;
}

function artistCoverUrl(artist, apiKey) {
  const coverPath = artist?.cover_path || "";
  if (!coverPath) return "";
  if (/^(https?:|data:|blob:)/i.test(coverPath) || coverPath.startsWith(`${API_BASE}/`)) {
    return coverPath;
  }
  if (!apiKey || !artist?.id) return "";
  return `${API_BASE}/library/artists/${artist.id}/cover?api_key=${encodeURIComponent(apiKey)}${coverCacheBust ? `&_cb=${coverCacheBust}` : ""}`;
}

function artistBucket(artist) {
  const s = ((artist.sort_name || artist.name) || "").trim();
  if (!s) return "#";
  const c = s[0].toUpperCase();
  if (c >= "A" && c <= "Z") return c;
  return "#";
}

function titleBucket(title) {
  const s = (title || "").trim();
  if (!s) return "#";
  const c = s[0].toUpperCase();
  if (c >= "A" && c <= "Z") return c;
  return "#";
}

function playlistPlayableTrack(track) {
  return {
    id: track.track_id,
    title: track.title,
    format: track.format,
    album_id: track.album_id,
    _artist: track.artist,
    _album: track.album,
  };
}

function searchLibraryTargets(library, search) {
  const needle = normalizeName(search || "");
  const targets = [];
  library.forEach((artist) => {
    const artistTrackIds = artistTracks(artist).map((track) => track.id);
    if (!needle || normalizeName(artist.name).includes(needle)) {
      targets.push({ id: `artist:${artist.id}`, title: artist.name, meta: `${artistTrackIds.length} tracks`, trackIds: artistTrackIds });
    }
    artist.albums.forEach((album) => {
      const albumTrackIds = album.tracks.map((track) => track.id);
      if (!needle || normalizeName(`${artist.name} ${album.title}`).includes(needle)) {
        targets.push({ id: `album:${album.id}`, title: album.title, meta: `${artist.name} / ${albumTrackIds.length} tracks`, trackIds: albumTrackIds });
      }
      album.tracks.forEach((track) => {
        if (!needle || normalizeName(`${artist.name} ${album.title} ${track.title}`).includes(needle)) {
          targets.push({ id: `track:${track.id}`, title: track.title, meta: `${artist.name} / ${album.title}`, trackIds: [track.id] });
        }
      });
    });
  });
  return targets.slice(0, 40);
}

function removeKey(type, id) {
  return `${type}:${id}`;
}

function canManageSettings(user) {
  return Boolean(user?.is_admin || user?.permissions?.includes("settings:manage"));
}

function canManageUsers(user) {
  return Boolean(user?.is_admin || user?.permissions?.includes("users:manage"));
}

function hasPermission(user, permission) {
  return Boolean(user?.is_admin || user?.permissions?.includes(permission));
}

function canViewPage(user, page) {
  if (!user) return page === "Settings";
  if (page === "Home") return true;
  if (page === "Library") return hasPermission(user, "library:view") || hasPermission(user, "library:edit");
  if (page === "Discover") return hasPermission(user, "discover");
  if (page === "Import/Add") return hasPermission(user, "import:run");
  if (page === "Wishlist") return hasPermission(user, "discover") || hasPermission(user, "wishlist:approve_all");
  if (page === "Task Queue") return hasPermission(user, "approvals:manage");
  if (page === "Playlists") return hasPermission(user, "playlists:manage");
  if (page === "Activity") return hasPermission(user, "activity:read");
  if (page === "Tools") return hasPermission(user, "tools:manage");
  if (page === "Automations") return hasPermission(user, "automations:manage");
  if (page === "Users") return hasPermission(user, "users:manage");
  if (page === "Settings") return true;
  return false;
}

function initials(value) {
  return String(value || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "?";
}

function trackNumberLabel(track) {
  const disc = track.disc_number && Number(track.disc_number) > 1 ? `${track.disc_number}.` : "";
  const number = track.track_number ? String(track.track_number).padStart(2, "0") : "##";
  return `${disc}${number}`;
}

function formatDuration(value) {
  const ms = Number(value || 0);
  if (!ms) return "";
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function copyStylesToWindow(targetWindow) {
  for (const sheet of document.styleSheets) {
    try {
      const style = targetWindow.document.createElement("style");
      style.textContent = [...sheet.cssRules].map((rule) => rule.cssText).join("\n");
      targetWindow.document.head.appendChild(style);
    } catch {
      if (sheet.href) {
        const link = targetWindow.document.createElement("link");
        link.rel = "stylesheet";
        link.href = sheet.href;
        targetWindow.document.head.appendChild(link);
      }
    }
  }
}

function normalizeEntityChanges(changes, fields) {
  const fieldByKey = new Map(fields.map((field) => [field.key, field]));
  return Object.fromEntries(
    Object.entries(changes).map(([key, value]) => {
      const field = fieldByKey.get(key);
      if (field?.type === "number") return [key, value === "" ? null : Number(value)];
      if (field?.type === "boolean") return [key, Boolean(value)];
      return [key, value === "" ? null : value];
    }),
  );
}

function LibraryTrackRow({ slot }) {
  return (
    <div className="library-track-row">
      <span className="chevron" />
      <CheckCircle size={15} className="library-track-icon" />
      <span className="library-track-title">{slot.track_number ? `${slot.track_number}. ` : ""}{slot.title}</span>
      <span className="library-track-badge">In library</span>
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
        {trackNumberLabel(slot)}-{slot.title}
      </span>
      <small>{slot.reason}</small>
      <button className="row-icon-button" onClick={onDismiss} title="Dismiss slot">
        <X size={15} />
      </button>
    </div>
  );
}

function DownloadBranchToggle({ checked, disabled, onChange, title }) {
  return (
    <label className="download-branch-toggle" title={title}>
      <input type="checkbox" checked={checked && !disabled} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
    </label>
  );
}

function buildImportDownloadRequests(grouped, downloadSelections, dismissedGhosts, extraGhosts) {
  const requests = [];
  grouped.forEach((artist) => {
    artist.albums.forEach((album) => {
      album.slots.forEach((slot) => {
        if (slot.file || slot.in_library || !downloadSelections.has(slot.id) || dismissedGhosts.has(slot.id)) return;
        if (isGenericTrackTitle(slot.title)) return;
        requests.push({ artist: artist.name, album: album.name, track: slot.title, track_number: slot.track_number, disc_number: slot.disc_number, duration_ms: slot.length || slot.duration_ms });
      });
    });
  });
  Object.entries(extraGhosts).forEach(([albumId, slots]) => {
    const [artistName, ...albumParts] = albumId.split("/");
    const albumName = albumParts.join("/");
    slots.forEach((slot) => {
      if (!downloadSelections.has(slot.id) || dismissedGhosts.has(slot.id)) return;
      if (isGenericTrackTitle(slot.title)) return;
      requests.push({ artist: artistName, album: albumName, track: slot.title, track_number: slot.track_number, disc_number: slot.disc_number, duration_ms: slot.length || slot.duration_ms });
    });
  });
  return requests;
}

function stableDownloadRequestKey(requests = []) {
  return requests
    .map((request) => [request.artist || "", request.album || "", request.track || request.title || "", request.track_number || ""].join("::"))
    .sort()
    .join("|");
}

function manualAlbumsFromDownloadRequests(requests = []) {
  const albumMap = new Map();
  requests.forEach((request, index) => {
    const artist = request.artist || "Unknown Artist";
    const album = request.album || "Singles";
    const key = albumRecordKey(artist, album);
    if (!albumMap.has(key)) {
      albumMap.set(key, { id: `seed:${key}`, artist, name: album, tracks: [], playlistName: request.playlist_name || null });
    }
    const entry = albumMap.get(key);
    entry.tracks.push({
      track_number: request.track_number || entry.tracks.length + 1,
      disc_number: request.disc_number || 1,
      title: request.track || request.title || `Track ${index + 1}`,
    });
  });
  return [...albumMap.values()];
}

function mergeManualAlbums(current, incoming) {
  const albumMap = new Map(current.map((album) => [albumRecordKey(album.artist, album.name), { ...album, tracks: [...(album.tracks || [])] }]));
  incoming.forEach((album) => {
    const key = albumRecordKey(album.artist, album.name);
    if (!albumMap.has(key)) {
      albumMap.set(key, album);
      return;
    }
    const existing = albumMap.get(key);
    const seenTracks = new Set((existing.tracks || []).map((track) => downloadTrackKey(track)));
    const tracks = [...(existing.tracks || [])];
    (album.tracks || []).forEach((track) => {
      const key = downloadTrackKey(track);
      if (seenTracks.has(key)) return;
      seenTracks.add(key);
      tracks.push(track);
    });
    albumMap.set(key, { ...existing, tracks });
  });
  return [...albumMap.values()];
}

function selectedSlotIdsForRequests(grouped, requests = []) {
  const selected = new Set();
  grouped.forEach((artist) => {
    artist.albums.forEach((album) => {
      album.slots.forEach((slot) => {
        if (slot.file) return;
        const match = requests.some((request) => {
          const sameArtist = normalizeName(request.artist || "Unknown Artist") === normalizeName(artist.name);
          const sameAlbum = normalizeName(request.album || "Singles") === normalizeName(album.name);
          const sameNumber = request.track_number && Number(request.track_number) === Number(slot.track_number) && Number(request.disc_number || 1) === Number(slot.disc_number || 1);
          const sameTitle = normalizeName(request.track || request.title || "") === normalizeName(slot.title);
          return sameArtist && sameAlbum && (sameNumber || sameTitle);
        });
        if (match) selected.add(slot.id);
      });
    });
  });
  return selected;
}

function downloadTrackKey(track) {
  return `${track.disc_number || 1}:${track.track_number || ""}:${normalizeName(track.title || track.track || "")}`;
}

function artistGhostSlots(artist, dismissedGhosts) {
  return artist.albums.flatMap((album) => album.slots.filter((slot) => !slot.file && !dismissedGhosts.has(slot.id)));
}

function TasksView({ tasks, playback, onCancel }) {
  const [openTasks, setOpenTasks] = useState(() => new Set());
  const nowPlaying = activePlaybackRows(playback);

  return (
    <div className="activity-view">
      {nowPlaying.length > 0 && (
        <section className="now-playing-strip">
          <h2>Now playing</h2>
          <div className="now-playing-list">
            {nowPlaying.map((row, index) => <PlaybackRow row={row} key={`${row.source}:${row.user_name}:${row.title}:${index}`} />)}
          </div>
        </section>
      )}
      {tasks.length === 0 ? (
        <EmptyState title="No activity" body="Scans, queued changes, downloads, and notifications will appear here." />
      ) : (
        <div className="task-list">
          {tasks.map((task) => (
            <section className="task-entry" key={task.id}>
              <button className="task-row" onClick={() => toggleSet(setOpenTasks, task.id)}>
                <strong>{task.type}</strong>
                <span>{task.status}</span>
                <small>{taskSummary(task)}</small>
                <TaskProgress task={task} />
              </button>
              {["queued", "running"].includes(task.status) && (
                <button className="secondary compact task-cancel" onClick={() => onCancel(task.id)}>
                  <X size={14} />
                  Cancel
                </button>
              )}
              {openTasks.has(task.id) && (
                <pre className="task-detail">{JSON.stringify({ payload: task.payload, result: task.result, error: task.error }, null, 2)}</pre>
              )}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

function ActiveWorkBar({ tasks }) {
  const activeTasks = tasks.filter((task) => ["queued", "running"].includes(task.status));
  if (activeTasks.length === 0) return null;
  return (
    <div className="active-work-bar">
      {activeTasks.slice(0, 3).map((task) => {
        const progress = taskProgress(task);
        return (
          <div className="active-work-item" key={task.id}>
            <strong>{taskDisplayName(task)}</strong>
            <InlineProgress
              value={progress?.percent || 0}
              label={progress?.message || task.status}
              indeterminate={!progress}
            />
          </div>
        );
      })}
    </div>
  );
}

function TaskProgress({ task }) {
  const progress = taskProgress(task);
  if (!progress) return null;
  return <InlineProgress value={progress.percent} label={progress.message} />;
}

function taskDisplayName(task) {
  if (task.type === "execute_proposal_batch") return "Processing task queue";
  if (task.type === "sync_favorites_jellyfin") return "Syncing playlists";
  const names = {
    check_files: "Checking files",
    check_duplicates: "Checking duplicates",
    check_lyrics: "Checking lyrics",
    check_album_covers: "Checking album covers",
    check_musicbrainz_ids: "Filling MusicBrainz info",
    check_missing_tracks: "Checking missing tracks",
    check_non_lossless: "Checking audio quality",
    check_audio_content: "Verifying audio content",
    apply_replaygain: "Measuring ReplayGain",
    propose_import: "Preparing import",
    ytdlp_download: "Downloading",
    jellyfin_scan: "Scanning Jellyfin",
    enrich_imports: "Enriching imports",
    create_pending_playlists: "Creating playlist",
  };
  return names[task.type] || task.type.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function InlineProgress({ value = 0, label = "", indeterminate = false, compact = false }) {
  const clamped = Math.max(0, Math.min(100, Number(value) || 0));
  return (
    <div className={`${indeterminate ? "inline-progress indeterminate" : "inline-progress"}${compact ? " compact" : ""}`}>
      {!compact && (
        <div className="inline-progress-label">
          <span>{label || "Working"}</span>
          {!indeterminate && <span>{Math.round(clamped)}%</span>}
        </div>
      )}
      <div className="inline-progress-track">
        <span style={{ width: indeterminate ? "42%" : `${clamped}%` }} />
      </div>
    </div>
  );
}

function ToolsView({ tasks, appLogs, user, backups, onRun, onFix, api, notify }) {
  const [query, setQuery] = useState("");
  const [restoreBackupPath, setRestoreBackupPath] = useState("");
  const tools = [
    ["Scan Jellyfin", "Request Jellyfin re-scans filles.", "jellyfin-scan", "tools:manage"],
    ["Remap tracks", "Match Nudibranch tracks to Jellyfin item IDs if playlists are not working.", "remap-tracks", "tools:manage"],
    ["Find missing album tracks", "Compare known albums against library records and prepare download approvals.", "check-missing-tracks", "tools:manage"],
    ["Check files against database", "Find library files missing from the database and records with missing files.", "check-files", "tools:manage"],
    ["Find duplicate files", "Find tracks with the same artist + album + title in multiple files; queue the extras to be moved to trash.", "check-duplicates", "tools:manage"],
    ["Check album covers", "Find albums without cover art and prepare images for review.", "check-album-covers", "tools:manage"],
    ["Check artist covers", "Find artists without cover art and prepare images for review.", "check-artist-covers", "tools:manage"],
    ["Check lyrics", "Find tracks without lyrics and", "check-lyrics", "tools:manage"],
    ["Check MusicBrainz info", "Scan the library for missing MusicBrainz IDs, disc/track numbers, and prepare metadata updates.", "check-musicbrainz-ids", "tools:manage"],
    ["Check audio content", "Verify each track's audio actually matches its album slot (duration + AcoustID) and queue replacements for incorrect files.", "check-audio-content", "tools:manage"],
    ["Check lossy tracks", "Find fake lossless or less than lossless files and prepare lossless replacement downloads.", "check-non-lossless", "tools:manage"],
    ["Apply ReplayGain", "Measure loudness and propose ReplayGain for all tracks (non-destructive; review-gated).", "apply-replaygain", "tools:manage"],
    ["Consolidate album folders", "Find albums whose tracks are split across folders and consolidate.", "consolidate-folders", "tools:manage"],
    ["Clear downloads folder", "Remove all files from /app/downloads.", "clear-downloads", "tools:manage"],
    ["Backup now", "Create a database backup.", "backup", "tools:manage"],
  ].filter(([, , , permission]) => hasPermission(user, permission));

  const logs = buildLiveLog(tasks, appLogs).filter((entry) => entry.text.toLowerCase().includes(query.toLowerCase()));
  return (
    <div className="tools-view">
      {tools.length > 0 && (
        <div className="tool-grid">
          {tools.map(([title, body, action]) => (
            <button className="tool-card" key={title} onClick={() => onRun(action)}>
              <Wrench size={18} />
              <span>
                <strong>{title}</strong>
                <small>{body}</small>
              </span>
            </button>
          ))}
        </div>
      )}
      {hasPermission(user, "tools:manage") && (
        <section className="restore-panel">
          <h2>Restore</h2>
          <div className="restore-actions">
            <button className="secondary compact danger" onClick={() => onRun("restore-default")}>
              <RefreshCw size={15} />
              Restore to default
            </button>
            <select value={restoreBackupPath} onChange={(event) => setRestoreBackupPath(event.target.value)}>
              <option value="">Choose backup</option>
              {(backups || []).map((backup) => (
                <option key={backup.path} value={backup.path}>
                  {backup.name}
                </option>
              ))}
            </select>
            <button className="secondary compact" onClick={() => onRun("restore-backup", { backup_path: restoreBackupPath })} disabled={!restoreBackupPath}>
              <RefreshCw size={15} />
              Restore backup
            </button>
          </div>
        </section>
      )}
      <AllSessionsPanel api={api} notify={notify} />
      {hasPermission(user, "activity:read") && (
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
      )}
    </div>
  );
}

// ─── Automations ─────────────────────────────────────────────────────────────

function buildCron({ frequency, time, weekday }) {
  const [h, m] = (time || "00:00").split(":").map((n) => parseInt(n, 10) || 0);
  if (frequency === "weekly") return `${m} ${h} * * ${weekday ?? 0}`;
  return `${m} ${h} * * *`; // daily
}

function parseCronToSimple(cron) {
  // Returns {simpleMode: true, frequency, time, weekday} or {simpleMode: false, raw}
  if (!cron) return { simpleMode: true, frequency: "daily", time: "00:00", weekday: 0 };
  const parts = cron.trim().split(/\s+/);
  if (parts.length === 5) {
    const [minute, hour, dom, month, dow] = parts;
    if (dom === "*" && month === "*") {
      const h = parseInt(hour, 10);
      const m = parseInt(minute, 10);
      const timeStr = `${String(isNaN(h) ? 0 : h).padStart(2, "0")}:${String(isNaN(m) ? 0 : m).padStart(2, "0")}`;
      if (dow === "*") return { simpleMode: true, frequency: "daily", time: timeStr, weekday: 0 };
      const dowNum = parseInt(dow, 10);
      if (!isNaN(dowNum)) return { simpleMode: true, frequency: "weekly", time: timeStr, weekday: dowNum };
    }
  }
  return { simpleMode: false, raw: cron };
}

const TOOL_OPTIONS = [
  ["Scan Jellyfin", "jellyfin-scan"],
  ["Remap tracks", "remap-tracks"],
  ["Find missing album tracks", "check-missing-tracks"],
  ["Check files", "check-files"],
  ["Find duplicates", "check-duplicates"],
  ["Check album covers", "check-album-covers"],
  ["Check artist covers", "check-artist-covers"],
  ["Check lyrics", "check-lyrics"],
  ["Check MusicBrainz info", "check-musicbrainz-ids"],
  ["Check audio content", "check-audio-content"],
  ["Check lossy tracks", "check-non-lossless"],
  ["Apply ReplayGain", "apply-replaygain"],
  ["Consolidate folders", "consolidate-folders"],
  ["Clear downloads", "clear-downloads"],
  ["Backup now", "backup"],
];

function triggerSummary(automation) {
  const { trigger_type, trigger_config } = automation;
  if (trigger_type === "webhook") return "Webhook";
  if (trigger_type === "event") {
    const labels = { download_complete: "On download complete", wishlist_match: "On wishlist match", scan_complete: "On scan complete" };
    return labels[trigger_config?.event] || "On event";
  }
  if (trigger_type === "interval") {
    const secs = trigger_config?.seconds || 0;
    if (secs >= 3600 && secs % 3600 === 0) return `Every ${secs / 3600} hr`;
    return `Every ${Math.round(secs / 60)} min`;
  }
  if (trigger_type === "time") {
    const cron = trigger_config?.cron || "";
    const parsed = parseCronToSimple(cron);
    if (parsed.simpleMode) {
      const label = parsed.frequency === "weekly"
        ? `Weekly ${["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][parsed.weekday ?? 0]} ${parsed.time}`
        : `Daily ${parsed.time}`;
      return label;
    }
    return `Cron: ${cron}`;
  }
  return trigger_type;
}

function actionSummary(automation) {
  const { action_type, action_config } = automation;
  if (action_type === "tool") {
    const found = TOOL_OPTIONS.find(([, slug]) => slug === action_config?.action);
    return `Tool: ${found ? found[0] : action_config?.action || "—"}`;
  }
  if (action_type === "play") {
    const parts = [`Play ${action_config?.target_type || "?"} "${action_config?.target_query || ""}"`];
    if (action_config?.shuffle) parts.push("shuffle");
    if (action_config?.loop && action_config.loop !== "off") parts.push(`loop ${action_config.loop}`);
    return parts.join(", ");
  }
  if (action_type === "media_control") return `Media: ${action_config?.control || "—"}`;
  return action_type;
}

function AutomationsView({ api, notify }) {
  const [automations, setAutomations] = useState([]);
  const [editingId, setEditingId] = useState(null);
  const [showForm, setShowForm] = useState(false);

  // Form state
  const [name, setName] = useState("");
  const [triggerType, setTriggerType] = useState("time");
  const [actionType, setActionType] = useState("tool");
  const [notifyMode, setNotifyMode] = useState("log");
  const [notifyPriority, setNotifyPriority] = useState("normal");

  // Trigger sub-fields
  const [cronSimple, setCronSimple] = useState(true); // true = simple, false = raw cron
  const [cronFrequency, setCronFrequency] = useState("daily");
  const [cronTime, setCronTime] = useState("00:00");
  const [cronWeekday, setCronWeekday] = useState(0);
  const [cronRaw, setCronRaw] = useState("");
  const [intervalValue, setIntervalValue] = useState(30);
  const [intervalUnit, setIntervalUnit] = useState("minutes");
  const [eventType, setEventType] = useState("download_complete");

  // Action sub-fields
  const [toolSlug, setToolSlug] = useState("backup");
  const [playTargetType, setPlayTargetType] = useState("artist");
  const [playTargetQuery, setPlayTargetQuery] = useState(""); // display label of the selected item
  const [playTargetId, setPlayTargetId] = useState(""); // definitive selection
  const [playLoop, setPlayLoop] = useState("off");
  const [playShuffle, setPlayShuffle] = useState(false);
  const [mediaControl, setMediaControl] = useState("pause");
  const [deviceId, setDeviceId] = useState(""); // "" = any device (broadcast)
  const [sessions, setSessions] = useState([]);
  // Live search for definitive target selection
  const [targetSearch, setTargetSearch] = useState("");
  const [targetResults, setTargetResults] = useState([]);

  async function reload() {
    try {
      const data = await api("/automations");
      setAutomations(data);
    } catch (err) {
      notify("Automation error", err.message, "ui_error");
    }
  }

  useEffect(() => { reload(); }, []);
  useEffect(() => {
    api("/me/sessions").then((rows) => setSessions(rows || [])).catch(() => {});
  }, []);

  // Debounced live search for the play-target picker.
  useEffect(() => {
    const q = targetSearch.trim();
    if (!q || playTargetType === "playlist") { setTargetResults([]); return undefined; }
    let active = true;
    const t = setTimeout(async () => {
      try {
        const data = await api(`/library/search?q=${encodeURIComponent(q)}&types=${playTargetType}&limit=8`);
        if (active) setTargetResults(data?.results || []);
      } catch { if (active) setTargetResults([]); }
    }, 250);
    return () => { active = false; clearTimeout(t); };
  }, [targetSearch, playTargetType, api]);

  function resetForm() {
    setEditingId(null);
    setName("");
    setTriggerType("time");
    setActionType("tool");
    setNotifyMode("log");
    setNotifyPriority("normal");
    setCronSimple(true);
    setCronFrequency("daily");
    setCronTime("00:00");
    setCronWeekday(0);
    setCronRaw("");
    setIntervalValue(30);
    setIntervalUnit("minutes");
    setEventType("download_complete");
    setToolSlug("backup");
    setPlayTargetType("artist");
    setPlayTargetQuery("");
    setPlayTargetId("");
    setPlayLoop("off");
    setPlayShuffle(false);
    setMediaControl("pause");
    setDeviceId("");
    setTargetSearch("");
    setTargetResults([]);
    setShowForm(false);
  }

  function openCreate() {
    resetForm();
    setShowForm(true);
  }

  function openEdit(a) {
    setEditingId(a.id);
    setName(a.name || "");
    setTriggerType(a.trigger_type || "time");
    setActionType(a.action_type || "tool");
    setNotifyMode(a.notify_mode || "log");
    setNotifyPriority(a.notify_priority || "normal");

    // Reverse-map trigger_config
    const tc = a.trigger_config || {};
    if (a.trigger_type === "time") {
      const parsed = parseCronToSimple(tc.cron || "");
      if (parsed.simpleMode) {
        setCronSimple(true);
        setCronFrequency(parsed.frequency);
        setCronTime(parsed.time);
        setCronWeekday(parsed.weekday ?? 0);
        setCronRaw("");
      } else {
        setCronSimple(false);
        setCronRaw(parsed.raw || tc.cron || "");
      }
    } else if (a.trigger_type === "interval") {
      const secs = tc.seconds || 60;
      if (secs >= 3600 && secs % 3600 === 0) { setIntervalValue(secs / 3600); setIntervalUnit("hours"); }
      else { setIntervalValue(Math.round(secs / 60)); setIntervalUnit("minutes"); }
    } else if (a.trigger_type === "event") {
      setEventType(tc.event || "download_complete");
    }

    // Reverse-map action_config
    const ac = a.action_config || {};
    if (a.action_type === "tool") {
      setToolSlug(ac.action || "backup");
    } else if (a.action_type === "play") {
      setPlayTargetType(ac.target_type || "artist");
      setPlayTargetQuery(ac.target_query || "");
      setPlayTargetId(ac.target_id || "");
      setPlayLoop(ac.loop || "off");
      setPlayShuffle(ac.shuffle || false);
    } else if (a.action_type === "media_control") {
      setMediaControl(ac.control || "pause");
    }
    setDeviceId(ac.device_id || "");

    setShowForm(true);
  }

  function buildTriggerConfig() {
    if (triggerType === "time") {
      const cron = cronSimple ? buildCron({ frequency: cronFrequency, time: cronTime, weekday: cronWeekday }) : cronRaw;
      return { cron };
    }
    if (triggerType === "interval") return { seconds: intervalValue * (intervalUnit === "hours" ? 3600 : 60) };
    if (triggerType === "webhook") return {};
    if (triggerType === "event") return { event: eventType };
    return {};
  }

  function buildActionConfig() {
    if (actionType === "tool") return { action: toolSlug };
    if (actionType === "play") {
      const cfg = { target_type: playTargetType, target_query: playTargetQuery, loop: playLoop, shuffle: playShuffle };
      if (playTargetId) cfg.target_id = playTargetId;
      if (deviceId) cfg.device_id = deviceId;
      return cfg;
    }
    if (actionType === "media_control") {
      const cfg = { control: mediaControl };
      if (deviceId) cfg.device_id = deviceId;
      return cfg;
    }
    return {};
  }

  async function handleSave() {
    if (!name.trim()) { notify("Validation", "Name is required.", "ui_error"); return; }
    if (actionType === "play") {
      if (playTargetType === "playlist" && !playTargetQuery.trim()) {
        notify("Validation", "Enter a playlist name to play.", "ui_error");
        return;
      }
      if (playTargetType !== "playlist" && !playTargetId) {
        notify("Validation", `Search and select the ${playTargetType} to play.`, "ui_error");
        return;
      }
    }
    const body = {
      name: name.trim(),
      trigger_type: triggerType,
      trigger_config: buildTriggerConfig(),
      action_type: actionType,
      action_config: buildActionConfig(),
      notify_mode: notifyMode,
      notify_priority: notifyPriority,
    };
    try {
      if (editingId) {
        await api(`/automations/${editingId}`, { method: "PATCH", body: JSON.stringify(body) });
        notify("Automation updated", name.trim());
      } else {
        await api("/automations", { method: "POST", body: JSON.stringify(body) });
        notify("Automation created", name.trim());
      }
      resetForm();
      reload();
    } catch (err) {
      notify("Automation error", err.message, "ui_error");
    }
  }

  async function handleToggle(a) {
    try {
      await api(`/automations/${a.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !a.enabled }) });
      reload();
    } catch (err) {
      notify("Automation error", err.message, "ui_error");
    }
  }

  async function handleRunNow(a) {
    try {
      const result = await api(`/automations/${a.id}/run`, { method: "POST" });
      notify("Automation triggered", result.message || result.status || "Running");
      reload();
    } catch (err) {
      notify("Automation error", err.message, "ui_error");
    }
  }

  async function handleDelete(a) {
    try {
      await api(`/automations/${a.id}`, { method: "DELETE" });
      notify("Automation deleted", a.name);
      reload();
    } catch (err) {
      notify("Automation error", err.message, "ui_error");
    }
  }

  function handleCopyWebhook(a) {
    const url = window.location.origin + (a.webhook_url || "");
    navigator.clipboard.writeText(url).then(
      () => notify("Copied", "Webhook URL copied to clipboard."),
      () => notify("Copy failed", "Could not access clipboard.", "ui_error"),
    );
  }

  function fmtDate(iso) {
    if (!iso) return "Never";
    const d = new Date(iso);
    return d.toLocaleString();
  }

  return (
    <div className="automations-view">
      {showForm && (
        <section className="settings-section automation-form">
          <h2>{editingId ? "Edit automation" : "New automation"}</h2>

          <div className="discover-search automation-name-bar">
            <Zap size={17} />
            <input
              type="text"
              placeholder="Automation name (e.g. Daily backup)"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <span />
          </div>

          <div className="automation-builder">
            {/* Trigger */}
            <div className="automation-stage">
              <div className="automation-stage-head">Trigger</div>
              <div className="automation-stage-body">
                <label className="automation-field">
                  <span>Type</span>
                  <select value={triggerType} onChange={(e) => setTriggerType(e.target.value)}>
                    <option value="time">Time (schedule)</option>
                    <option value="interval">Interval</option>
                    <option value="webhook">Webhook</option>
                    <option value="event">Event</option>
                  </select>
                </label>

                {triggerType === "time" && (
                  <>
                    <label className="automation-field">
                      <span>Mode</span>
                      <select value={cronSimple ? "simple" : "cron"} onChange={(e) => setCronSimple(e.target.value === "simple")}>
                        <option value="simple">Simple</option>
                        <option value="cron">Cron expression</option>
                      </select>
                    </label>
                    {cronSimple ? (
                      <>
                        <label className="automation-field">
                          <span>Frequency</span>
                          <select value={cronFrequency} onChange={(e) => setCronFrequency(e.target.value)}>
                            <option value="daily">Daily</option>
                            <option value="weekly">Weekly</option>
                          </select>
                        </label>
                        <label className="automation-field">
                          <span>Time</span>
                          <input type="time" value={cronTime} onChange={(e) => setCronTime(e.target.value)} />
                        </label>
                        {cronFrequency === "weekly" && (
                          <label className="automation-field">
                            <span>Weekday</span>
                            <select value={cronWeekday} onChange={(e) => setCronWeekday(Number(e.target.value))}>
                              {["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"].map((day, i) => (
                                <option key={day} value={i}>{day}</option>
                              ))}
                            </select>
                          </label>
                        )}
                      </>
                    ) : (
                      <label className="automation-field">
                        <span>Cron expression</span>
                        <input type="text" placeholder="M H * * D" value={cronRaw} onChange={(e) => setCronRaw(e.target.value)} />
                      </label>
                    )}
                  </>
                )}

                {triggerType === "interval" && (
                  <>
                    <label className="automation-field">
                      <span>Every</span>
                      <input type="number" min={1} value={intervalValue} onChange={(e) => setIntervalValue(Math.max(1, Number(e.target.value)))} />
                    </label>
                    <label className="automation-field">
                      <span>Unit</span>
                      <select value={intervalUnit} onChange={(e) => setIntervalUnit(e.target.value)}>
                        <option value="minutes">Minutes</option>
                        <option value="hours">Hours</option>
                      </select>
                    </label>
                  </>
                )}

                {triggerType === "webhook" && (
                  <p className="automation-stage-note">A webhook URL is generated after saving. POST to it to trigger this automation.</p>
                )}

                {triggerType === "event" && (
                  <label className="automation-field">
                    <span>Event</span>
                    <select value={eventType} onChange={(e) => setEventType(e.target.value)}>
                      <option value="download_complete">Download complete</option>
                      <option value="wishlist_match">Wishlist match</option>
                      <option value="scan_complete">Scan complete</option>
                    </select>
                  </label>
                )}
              </div>
            </div>

            <div className="automation-stage-arrow">›</div>

            {/* Action */}
            <div className="automation-stage">
              <div className="automation-stage-head">Action</div>
              <div className="automation-stage-body">
                <label className="automation-field">
                  <span>Type</span>
                  <select value={actionType} onChange={(e) => setActionType(e.target.value)}>
                    <option value="tool">Run a tool</option>
                    <option value="play">Play music</option>
                    <option value="media_control">Media control</option>
                  </select>
                </label>

                {actionType === "tool" && (
                  <label className="automation-field">
                    <span>Tool</span>
                    <select value={toolSlug} onChange={(e) => setToolSlug(e.target.value)}>
                      {TOOL_OPTIONS.map(([label, slug]) => (
                        <option key={slug} value={slug}>{label}</option>
                      ))}
                    </select>
                  </label>
                )}

                {actionType === "play" && (
                  <>
                    <label className="automation-field">
                      <span>Target type</span>
                      <select
                        value={playTargetType}
                        onChange={(e) => { setPlayTargetType(e.target.value); setPlayTargetId(""); setPlayTargetQuery(""); setTargetSearch(""); setTargetResults([]); }}
                      >
                        <option value="artist">Artist</option>
                        <option value="album">Album</option>
                        <option value="track">Track</option>
                        <option value="playlist">Playlist</option>
                      </select>
                    </label>
                    {playTargetType === "playlist" ? (
                      <label className="automation-field">
                        <span>Playlist name</span>
                        <input
                          type="text"
                          placeholder="e.g. Favorites"
                          value={playTargetQuery}
                          onChange={(e) => { setPlayTargetQuery(e.target.value); setPlayTargetId(""); }}
                        />
                      </label>
                    ) : (
                      <div className="automation-field">
                        <span>{playTargetType.charAt(0).toUpperCase() + playTargetType.slice(1)}</span>
                        {playTargetId || playTargetQuery ? (
                          <div className="automation-selected">
                            <span className="automation-selected-name" title={playTargetQuery}>{playTargetQuery}</span>
                            <button
                              type="button"
                              className="icon-button"
                              title="Change selection"
                              onClick={() => { setPlayTargetId(""); setPlayTargetQuery(""); setTargetSearch(""); setTargetResults([]); }}
                            >
                              <X size={14} />
                            </button>
                          </div>
                        ) : (
                          <div className="automation-search">
                            <input
                              type="text"
                              placeholder={`Search ${playTargetType}…`}
                              value={targetSearch}
                              onChange={(e) => setTargetSearch(e.target.value)}
                            />
                            {targetResults.length > 0 && (
                              <div className="automation-search-results">
                                {targetResults.map((r) => (
                                  <button
                                    type="button"
                                    key={`${r.kind}:${r.id}`}
                                    className="automation-search-result"
                                    onClick={() => { setPlayTargetId(r.id); setPlayTargetQuery(r.name); setTargetSearch(""); setTargetResults([]); }}
                                  >
                                    {r.name}
                                  </button>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )}
                    <label className="automation-field">
                      <span>Loop</span>
                      <select value={playLoop} onChange={(e) => setPlayLoop(e.target.value)}>
                        <option value="off">Off</option>
                        <option value="one">Repeat one</option>
                        <option value="all">Repeat all</option>
                      </select>
                    </label>
                    <label className="automation-field automation-field-inline">
                      <span>Shuffle</span>
                      <input type="checkbox" checked={playShuffle} onChange={(e) => setPlayShuffle(e.target.checked)} />
                    </label>
                    <label className="automation-field">
                      <span>Device</span>
                      <select value={deviceId} onChange={(e) => setDeviceId(e.target.value)}>
                        <option value="">Any device</option>
                        {sessions.map((s) => (
                          <option key={s.id} value={s.id}>{s.device_label || "Unknown device"}</option>
                        ))}
                      </select>
                    </label>
                  </>
                )}

                {actionType === "media_control" && (
                  <>
                    <label className="automation-field">
                      <span>Control</span>
                      <select value={mediaControl} onChange={(e) => setMediaControl(e.target.value)}>
                        <option value="pause">Pause</option>
                        <option value="resume">Resume</option>
                        <option value="next">Next</option>
                        <option value="previous">Previous</option>
                        <option value="stop">Stop</option>
                      </select>
                    </label>
                    <label className="automation-field">
                      <span>Device</span>
                      <select value={deviceId} onChange={(e) => setDeviceId(e.target.value)}>
                        <option value="">Any device</option>
                        {sessions.map((s) => (
                          <option key={s.id} value={s.id}>{s.device_label || "Unknown device"}</option>
                        ))}
                      </select>
                    </label>
                  </>
                )}
              </div>
            </div>

            <div className="automation-stage-arrow">›</div>

            {/* Notify */}
            <div className="automation-stage">
              <div className="automation-stage-head">Notify</div>
              <div className="automation-stage-body">
                <label className="automation-field">
                  <span>Mode</span>
                  <select value={notifyMode} onChange={(e) => setNotifyMode(e.target.value)}>
                    <option value="log">Log only</option>
                    <option value="notification">Notification</option>
                    <option value="both">Both</option>
                  </select>
                </label>
                <label className="automation-field">
                  <span>Priority</span>
                  <select value={notifyPriority} onChange={(e) => setNotifyPriority(e.target.value)}>
                    <option value="low">Low</option>
                    <option value="normal">Normal</option>
                    <option value="high">High</option>
                  </select>
                </label>
              </div>
            </div>
          </div>

          <div className="automation-builder-actions">
            <button className="secondary compact" onClick={resetForm}>
              <X size={15} />
              Cancel
            </button>
            <button onClick={handleSave}>
              <Check size={15} />
              {editingId ? "Save changes" : "Add"}
            </button>
          </div>
        </section>
      )}

      {automations.length === 0 && !showForm && (
        <div className="automation-empty">
          <Zap size={28} />
          <p>No automations yet. Create one to run tools or play music on a schedule.</p>
        </div>
      )}

      {automations.map((a) => (
        <div key={a.id} className="automation-card">
          <div className="automation-card-row">
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Zap size={16} />
              <strong>{a.name}</strong>
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button
                className={`secondary compact${a.enabled ? "" : " danger"}`}
                title={a.enabled ? "Disable" : "Enable"}
                onClick={() => handleToggle(a)}
              >
                {a.enabled ? "Enabled" : "Disabled"}
              </button>
              <button className="secondary compact" onClick={() => handleRunNow(a)}>
                <Play size={13} />
                Run now
              </button>
              <button className="secondary compact" onClick={() => openEdit(a)}>
                <Pencil size={13} />
                Edit
              </button>
              <button className="secondary compact danger" onClick={() => handleDelete(a)}>
                <Trash2 size={13} />
                Delete
              </button>
            </span>
          </div>
          <div className="automation-summary">
            <span>{triggerSummary(a)}</span>
            <span>→</span>
            <span>{actionSummary(a)}</span>
          </div>
          {a.trigger_type === "webhook" && a.webhook_url && (
            <div className="automation-summary" style={{ gap: 6 }}>
              <span style={{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>
                {window.location.origin + a.webhook_url}
              </span>
              <button className="secondary compact" style={{ flexShrink: 0 }} onClick={() => handleCopyWebhook(a)}>
                Copy
              </button>
            </div>
          )}
          <div className="automation-summary">
            <span>Last run: {fmtDate(a.last_run_at)}</span>
            {a.last_status && (
              <span className={a.last_status === "error" ? "automation-status-error" : ""}>
                {a.last_status}
              </span>
            )}
            {a.next_run_at && <span>Next: {fmtDate(a.next_run_at)}</span>}
          </div>
          {a.last_error && <div className="automation-status-error" style={{ fontSize: 12 }}>{a.last_error}</div>}
        </div>
      ))}

      <div className="automation-card-row" style={{ justifyContent: "center" }}>
        {!showForm && (
          <button className="secondary compact" onClick={openCreate}>
            <Plus size={15} />
            New automation
          </button>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

function CheckFilesResult({ result, onFix }) {
  const missingFiles = result?.missing_files || [];
  const missingRecords = result?.missing_records || [];
  const relinked = result?.relinked || 0;
  if (missingRecords.length === 0) {
    return (
      <section className="check-files-panel">
        <h2>File Check</h2>
        {relinked > 0 && <p>Relinked {relinked} record{relinked === 1 ? "" : "s"} to files that moved on disk.</p>}
        <p>
          {missingFiles.length
            ? `${missingFiles.length} records with missing files were added to the task queue.`
            : relinked > 0
              ? "All other records already match files on disk."
              : "No untracked library files found."}
        </p>
      </section>
    );
  }
  return (
    <section className="check-files-panel">
      <h2>File Check</h2>
      {relinked > 0 && <p>Relinked {relinked} record{relinked === 1 ? "" : "s"} to files that moved on disk.</p>}
      {missingFiles.length > 0 && <p>{missingFiles.length} records with missing files were added to the task queue.</p>}
      <div className="check-files-grid">
        <div>
          <h3>Files With No Records</h3>
          {missingRecords.map((file) => (
            <div className="check-file-row" key={file.path}>
              <span>
                <strong>{file.name}</strong>
                <small>{file.path}</small>
              </span>
              <button className="secondary compact" onClick={() => onFix({ action: "create_record", path: file.path })}>
                <Plus size={15} />
                Create record
              </button>
              <button className="secondary compact danger" onClick={() => onFix({ action: "delete_file", path: file.path })}>
                <Trash2 size={15} />
                Delete file
              </button>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function PlayHistoryPanel({ api }) {
  const [plays, setPlays] = useState(null);
  useEffect(() => {
    let active = true;
    api("/me/plays?limit=50")
      .then((data) => { if (active) setPlays(data || []); })
      .catch(() => { if (active) setPlays([]); });
    return () => { active = false; };
  }, [api]);
  return (
    <section className="settings-section play-history">
      <h2>My play history</h2>
      {plays === null ? (
        <p className="muted">Loading…</p>
      ) : plays.length === 0 ? (
        <p className="muted">No plays recorded yet.</p>
      ) : (
        <ul className="home-list play-history-list">
          {plays.map((p, i) => (
            <li key={`${p.track_id}-${i}`}>
              <span className="home-list-main">{p.title || "Unknown"}</span>
              <span className="home-list-sub">
                {[p.artist, p.album].filter(Boolean).join(" · ")}
                {p.played_at ? ` · ${new Date(p.played_at).toLocaleString()}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function UsersView({ users, permissions, currentUser, canManage, onCreate, onUpdate, onDelete, onUpdatePin, onUpdateOwnPin, jellyfinUsers, jellyfinUsersLoading, onLoadJellyfinUsers, onUpdateJellyfinUser, api }) {
  const [newUser, setNewUser] = useState({ display_name: "", username: "", password: "", is_admin: false, permissions: [] });
  const permissionGroups = useMemo(() => groupBy(permissions, (permission) => permission.section), [permissions]);
  const visibleUsers = canManage ? users : currentUser ? [currentUser] : [];
  const [presence, setPresence] = useState({});
  useEffect(() => {
    let active = true;
    async function poll() {
      try {
        const fresh = await api("/users");
        if (active) setPresence(Object.fromEntries(fresh.map((u) => [u.id, !!u.online])));
      } catch {
        /* ignore presence poll errors */
      }
    }
    const timer = setInterval(poll, 20000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [api]);

  function toggleNewPermission(value) {
    setNewUser((current) => ({
      ...current,
      permissions: toggleArrayValue(current.permissions, value),
    }));
  }

  async function submitNewUser(event) {
    event.preventDefault();
    if (!newUser.display_name.trim() || !newUser.username.trim() || !newUser.password) return;
    await onCreate(newUser);
    setNewUser({ display_name: "", username: "", password: "", is_admin: false, permissions: [] });
  }

  return (
    <div className="users-view">
      {canManage && (
        <form className="user-create-panel" onSubmit={submitNewUser}>
          <h2>Create user</h2>
          <label>
            Name
            <input value={newUser.display_name} onChange={(event) => setNewUser((current) => ({ ...current, display_name: event.target.value }))} />
          </label>
          <label>
            Username
            <input value={newUser.username} onChange={(event) => setNewUser((current) => ({ ...current, username: event.target.value }))} />
          </label>
          <label>
            Password
            <input type="password" value={newUser.password} onChange={(event) => setNewUser((current) => ({ ...current, password: event.target.value }))} />
          </label>
          <label className="inline-check">
            <input type="checkbox" checked={newUser.is_admin} onChange={(event) => setNewUser((current) => ({ ...current, is_admin: event.target.checked }))} />
            Admin
          </label>
          {!newUser.is_admin && (
            <PermissionGrid
              groups={permissionGroups}
              selected={newUser.permissions}
              onToggle={toggleNewPermission}
            />
          )}
          <button className="primary compact-button" disabled={!newUser.display_name.trim() || !newUser.username.trim() || !newUser.password}>
            <Plus size={15} />
            Create user
          </button>
        </form>
      )}
      <div className="user-list">
        {visibleUsers.map((managedUser) => (
          <UserCard
            key={managedUser.id}
            user={{ ...managedUser, online: presence[managedUser.id] ?? managedUser.online }}
            currentUser={currentUser}
            permissionGroups={permissionGroups}
            canManage={canManage}
            onUpdate={onUpdate}
            onDelete={canManage ? onDelete : null}
            onUpdatePin={canManage ? onUpdatePin : (_userId, password) => onUpdateOwnPin(password)}
            jellyfinUsers={jellyfinUsers}
            jellyfinUsersLoading={jellyfinUsersLoading}
            onLoadJellyfinUsers={onLoadJellyfinUsers}
            onUpdateJellyfinUser={canManage ? onUpdateJellyfinUser : null}
          />
        ))}
      </div>
    </div>
  );
}

function PlaybackRow({ row }) {
  const location = [row.source, row.client || row.device_name].filter(Boolean).join(" / ");
  const meta = [
    row.title,
    row.artist,
    row.album,
  ].filter(Boolean).join(" · ");
  return (
    <div className="playback-row">
      <strong>{row.user_name}</strong>
      <span>{[location, row.status || "stopped"].filter(Boolean).join(" · ")}</span>
      <small>{meta || "Nothing playing"}</small>
    </div>
  );
}

function UserCard({ user, currentUser, permissionGroups, canManage, onUpdate, onDelete, onUpdatePin, jellyfinUsers, jellyfinUsersLoading, onLoadJellyfinUsers, onUpdateJellyfinUser }) {
  const [draft, setDraft] = useState(() => ({ display_name: user.display_name, is_admin: user.is_admin, permissions: user.permissions || [] }));
  const [password, setPassword] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const changed =
    draft.display_name !== user.display_name ||
    draft.is_admin !== user.is_admin ||
    stablePermissionKey(draft.permissions) !== stablePermissionKey(user.permissions || []);

  useEffect(() => {
    setDraft({ display_name: user.display_name, is_admin: user.is_admin, permissions: user.permissions || [] });
    setPassword("");
  }, [user.id, user.display_name, user.is_admin, stablePermissionKey(user.permissions || [])]);

  function togglePermission(value) {
    setDraft((current) => ({
      ...current,
      permissions: toggleArrayValue(current.permissions, value),
    }));
  }

  return (
    <section className="user-card">
      <div className="user-card-header">
        <span
          title={user.online ? "Online" : "Offline"}
          style={{ display: "inline-block", width: 9, height: 9, borderRadius: "50%", marginRight: 8, alignSelf: "center", background: user.online ? "#37c871" : "#9aa0a6" }}
        />
        <label>
          Name
          <input value={draft.display_name} onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))} disabled={!canManage} />
        </label>
        {user.username && (
          <label>
            Username
            <input value={user.username} disabled />
          </label>
        )}
        {canManage && (
          <label className="inline-check">
            <input
              type="checkbox"
              checked={draft.is_admin}
              onChange={(event) => setDraft((current) => ({ ...current, is_admin: event.target.checked }))}
              disabled={user.id === currentUser?.id && user.is_admin}
            />
            Admin
          </label>
        )}
        {canManage && (
          <button
            className="primary compact-button"
            disabled={!changed || !draft.display_name.trim()}
            onClick={() => onUpdate(user.id, draft)}
          >
            Save
          </button>
        )}
      </div>
      {!draft.is_admin && (
        <PermissionGrid
          groups={permissionGroups}
          selected={draft.permissions}
          onToggle={canManage ? togglePermission : null}
        />
      )}
      {draft.is_admin && <p className="user-note">Admin users have every permission.</p>}
      {onUpdateJellyfinUser && (
        <div className="pin-reset-row">
          <label>
            Jellyfin account
          </label>
          <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
            <select
              value={user.jellyfin_user_id || ""}
              onChange={async (event) => {
                try {
                  await onUpdateJellyfinUser(user.id, event.target.value || null);
                } catch {
                  // error notification handled upstream
                }
              }}
            >
              <option value="">Not linked</option>
              {(jellyfinUsers || (user.jellyfin_user_id ? [{ id: user.jellyfin_user_id, name: user.jellyfin_user_id }] : [])).map((u) => (
                <option key={u.id} value={u.id}>{u.name}</option>
              ))}
            </select>
            <button className="secondary compact" type="button" disabled={jellyfinUsersLoading} onClick={onLoadJellyfinUsers}>
              {jellyfinUsersLoading ? "…" : "Load"}
            </button>
          </div>
        </div>
      )}
      <div className="pin-reset-row">
        <label>
          New Password
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <button className="secondary compact" disabled={!password} onClick={() => onUpdatePin(user.id, password).then(() => setPassword(""))}>
          Reset Password
        </button>
      </div>
      {onDelete && user.id !== currentUser?.id && (
        <div className="pin-reset-row user-delete-row">
          {confirmDelete ? (
            <>
              <span className="user-note">Delete this user and all their data?</span>
              <button className="secondary compact danger" onClick={() => { setConfirmDelete(false); onDelete(user.id); }}>
                Confirm delete
              </button>
              <button className="secondary compact" onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button className="secondary compact danger" onClick={() => setConfirmDelete(true)}>
              <Trash2 size={14} /> Delete user
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function PermissionGrid({ groups, selected, onToggle }) {
  return (
    <div className="permission-grid">
      {[...groups.entries()].map(([section, permissions]) => (
        <fieldset key={section}>
          <legend>{section}</legend>
          {permissions.map((permission) => (
            <label className="inline-check" key={permission.value}>
              <input type="checkbox" checked={selected.includes(permission.value)} disabled={!onToggle} onChange={() => onToggle?.(permission.value)} />
              {permission.label}
            </label>
          ))}
        </fieldset>
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

function AllSessionsPanel({ api, notify }) {
  const [sessions, setSessions] = useState(null);
  const [open, setOpen] = useState(false);
  const [loadingRevoke, setLoadingRevoke] = useState({});

  async function loadSessions() {
    try {
      setSessions(await api("/sessions"));
    } catch (err) {
      notify("Sessions error", err.message, "ui_error");
    }
  }

  useEffect(() => {
    loadSessions();
    const timer = setInterval(loadSessions, 20000);
    return () => clearInterval(timer);
  }, []);

  async function revokeSession(id) {
    setLoadingRevoke((prev) => ({ ...prev, [id]: true }));
    try {
      await api(`/sessions/${id}`, { method: "DELETE" });
      notify("Session revoked", "The session has been signed out.", "ui_notice");
      loadSessions();
    } catch (err) {
      notify("Revoke failed", err.message, "ui_error");
    } finally {
      setLoadingRevoke((prev) => ({ ...prev, [id]: false }));
    }
  }

  const onlineCount = (sessions || []).filter((s) => s.online).length;
  return (
    <section className="settings-section sessions-panel">
      <button type="button" className="sessions-tree-header" onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        <h2>All sessions</h2>
        <span className="muted-label">{onlineCount} online · {sessions ? sessions.length : 0} total</span>
      </button>
      {open &&
        (sessions === null ? (
          <p className="muted-label">Loading…</p>
        ) : sessions.length === 0 ? (
          <p className="muted-label">No active sessions found.</p>
        ) : (
          <div className="security-list sessions-list">
            {sessions.map((session) => (
              <div key={session.id} className="security-row session-row">
                <div className="security-row-info">
                  <span className="security-row-label">
                    <span
                      title={session.online ? "Online" : "Offline"}
                      style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", marginRight: 6, background: session.online ? "#37c871" : "#9aa0a6" }}
                    />
                    {session.user_name || session.username || "User"}
                    {" — "}
                    {session.device_label || "Unknown device"}
                  </span>
                  <small className="muted-label">
                    Last used: {session.last_used_at ? new Date(session.last_used_at).toLocaleString() : "never"}
                    {" · "}
                    Expires: {session.expires_at ? new Date(session.expires_at).toLocaleString() : "never"}
                  </small>
                </div>
                <div className="session-row-actions">
                  <button
                    className="icon-button session-revoke"
                    title="Revoke session"
                    disabled={loadingRevoke[session.id]}
                    onClick={() => revokeSession(session.id)}
                  >
                    <X size={15} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        ))}
    </section>
  );
}

function SessionsPanel({ api, notify }) {
  const [sessions, setSessions] = useState(null);
  const [open, setOpen] = useState(false); // collapsed by default
  const [loadingRevoke, setLoadingRevoke] = useState({});

  async function loadSessions() {
    try {
      setSessions(await api("/me/sessions"));
    } catch (err) {
      notify("Sessions error", err.message, "ui_error");
    }
  }

  useEffect(() => {
    loadSessions();
  }, []);

  async function revokeSession(id) {
    setLoadingRevoke((prev) => ({ ...prev, [id]: true }));
    try {
      await api(`/me/sessions/${id}`, { method: "DELETE" });
      notify("Session revoked", "The session has been signed out.", "ui_notice");
      loadSessions();
    } catch (err) {
      notify("Revoke failed", err.message, "ui_error");
    } finally {
      setLoadingRevoke((prev) => ({ ...prev, [id]: false }));
    }
  }

  async function renameSession(s) {
    const next = window.prompt("Name this session", s.device_label || "");
    if (next == null) return;
    const label = next.trim();
    if (!label || label === s.device_label) return;
    try {
      await api(`/me/sessions/${s.id}`, { method: "PATCH", body: JSON.stringify({ device_label: label }) });
      loadSessions();
    } catch (err) {
      notify("Rename failed", err.message, "ui_error");
    }
  }

  return (
    <section className="settings-section sessions-panel">
      <button type="button" className="sessions-tree-header" onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        <h2>Sessions</h2>
        <span className="muted-label">{sessions ? sessions.length : 0}</span>
      </button>
      {open &&
        (sessions === null ? (
          <p className="muted-label">Loading…</p>
        ) : sessions.length === 0 ? (
          <p className="muted-label">No active sessions found.</p>
        ) : (
          <div className="security-list sessions-list">
            {sessions.map((session) => (
              <div key={session.id} className="security-row session-row">
                <div className="security-row-info">
                  <span className="security-row-label">
                    {session.device_label || "Unknown device"}
                    {session.current && <span className="security-badge current-badge">This device</span>}
                  </span>
                  <small className="muted-label">
                    Last used: {session.last_used_at ? new Date(session.last_used_at).toLocaleString() : "never"}
                    {" · "}
                    Expires: {session.expires_at ? new Date(session.expires_at).toLocaleString() : "never"}
                  </small>
                </div>
                <div className="session-row-actions">
                  <button
                    className="icon-button"
                    title="Rename session"
                    onClick={() => renameSession(session)}
                  >
                    <Pencil size={14} />
                  </button>
                  {!session.current && (
                    <button
                      className="icon-button session-revoke"
                      title="Revoke session"
                      disabled={loadingRevoke[session.id]}
                      onClick={() => revokeSession(session.id)}
                    >
                      <X size={15} />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        ))}
    </section>
  );
}

function SecuritySettings({ api, notify }) {
  const [apiKeys, setApiKeys] = useState(null);
  const [newKeyName, setNewKeyName] = useState("");
  const [createdSecret, setCreatedSecret] = useState(null); // { id, name, api_key }
  const [loadingRevoke, setLoadingRevoke] = useState({});
  const [creatingKey, setCreatingKey] = useState(false);

  async function loadApiKeys() {
    try {
      setApiKeys(await api("/me/api-keys"));
    } catch (err) {
      notify("API keys error", err.message, "ui_error");
    }
  }

  useEffect(() => {
    loadApiKeys();
  }, []);

  async function createApiKey() {
    if (!newKeyName.trim()) return;
    setCreatingKey(true);
    try {
      const created = await api("/me/api-keys", {
        method: "POST",
        body: JSON.stringify({ name: newKeyName.trim() }),
      });
      setCreatedSecret(created);
      setNewKeyName("");
      loadApiKeys();
    } catch (err) {
      notify("Create key failed", err.message, "ui_error");
    } finally {
      setCreatingKey(false);
    }
  }

  async function revokeApiKey(id) {
    setLoadingRevoke((prev) => ({ ...prev, [`key-${id}`]: true }));
    try {
      await api(`/me/api-keys/${id}`, { method: "DELETE" });
      notify("API key revoked", "The key can no longer be used.", "ui_notice");
      if (createdSecret?.id === id) setCreatedSecret(null);
      loadApiKeys();
    } catch (err) {
      notify("Revoke failed", err.message, "ui_error");
    } finally {
      setLoadingRevoke((prev) => ({ ...prev, [`key-${id}`]: false }));
    }
  }

  const activeKeys = apiKeys ? apiKeys.filter((k) => !k.revoked) : null;

  return (
      <section className="settings-section">
        <h2>API keys</h2>
        <div className="security-create-row">
          <input
            type="text"
            className="security-key-input"
            placeholder="Key name (e.g. Home server)"
            value={newKeyName}
            onChange={(e) => setNewKeyName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createApiKey()}
          />
          <button
            className="primary compact-button"
            disabled={creatingKey || !newKeyName.trim()}
            onClick={createApiKey}
          >
            <Plus size={14} />
            Create key
          </button>
        </div>

        {createdSecret && (
          <div className="security-new-key-reveal">
            <div className="security-new-key-warning">
              <Shield size={15} />
              Copy this now — it won&apos;t be shown again.
            </div>
            <div className="security-new-key-row">
              <input
                readOnly
                type="text"
                className="security-secret-input"
                value={createdSecret.api_key}
                onFocus={(e) => e.target.select()}
              />
              <button
                className="secondary compact"
                onClick={() => {
                  navigator.clipboard.writeText(createdSecret.api_key).catch(() => {});
                  notify("Copied", "API key copied to clipboard.", "ui_notice");
                }}
              >
                <Check size={14} />
                Copy
              </button>
            </div>
            <small className="muted-label">Key name: {createdSecret.name}</small>
          </div>
        )}

        {activeKeys === null ? (
          <p className="muted-label">Loading…</p>
        ) : activeKeys.length === 0 ? (
          <p className="muted-label">No API keys yet.</p>
        ) : (
          <div className="security-list">
            {activeKeys.map((key) => (
              <div key={key.id} className="security-row">
                <div className="security-row-info">
                  <span className="security-row-label">{key.name}</span>
                  <small className="muted-label">
                    Prefix: {key.prefix}
                    {" · "}
                    Created: {new Date(key.created_at).toLocaleString()}
                    {" · "}
                    Last used: {key.last_used_at ? new Date(key.last_used_at).toLocaleString() : "never"}
                  </small>
                </div>
                <button
                  className="icon-button"
                  title="Revoke key"
                  disabled={loadingRevoke[`key-${key.id}`]}
                  onClick={() => revokeApiKey(key.id)}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
  );
}

function SettingsPanel({
  accentColor,
  setAccentColor,
  backgroundTint,
  setBackgroundTint,
  dark,
  setDark,
  crossfadeDuration,
  setCrossfadeDuration,
  onSaveSearchThreshold,
  user,
  apiKey,
  playlists,
  integrationSettings,
  onSaveIntegrations,
  onUploadYoutubeCookies,
  api,
  notify,
  playerDiagnostics,
  onTogglePlayerDiagnostics,
}) {
  const [searchThreshold, setSearchThreshold] = useState(() => (user && user.search_min_confidence != null ? user.search_min_confidence : 0.4));
  // Resync if the user object loads/changes after mount.
  useEffect(() => {
    if (user && user.search_min_confidence != null) setSearchThreshold(user.search_min_confidence);
  }, [user?.search_min_confidence]);
  const [shownIntegrationKeys, setShownIntegrationKeys] = useState({});
  const [integrationDraft, setIntegrationDraft] = useState(integrationSettings || {});
  const cookiesUploadRef = useRef(null);
  const canViewApiKey =
    user?.is_admin || user?.permissions?.includes("settings:manage") || user?.permissions?.includes("users:manage");

  useEffect(() => {
    setIntegrationDraft(integrationSettings || {});
  }, [integrationSettings]);

  return (
    <div className="settings-grid">
      <section className="settings-section">
        <h2>Appearance</h2>
        <label className="setting-row">
          <span>
            Theme
            <small>Switch between light and dark interface colors.</small>
          </span>
          <button className="secondary compact" onClick={() => setDark((value) => !value)}>
            {dark ? <Sun size={15} /> : <Moon size={15} />}
            {dark ? "Light mode" : "Dark mode"}
          </button>
        </label>
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
        <label className="setting-row crossfade-row">
          <span>
            Crossfade
            <small>Fade between tracks. {crossfadeDuration === 0 ? "Off" : `${crossfadeDuration.toFixed(1)}s`}</small>
          </span>
          <input
            className="crossfade-slider"
            type="range"
            min="0"
            max="15"
            step="0.5"
            value={crossfadeDuration}
            style={{ "--progress": `${(crossfadeDuration / 15) * 100}%` }}
            onChange={(event) => setCrossfadeDuration(Number(event.target.value))}
          />
        </label>
        <label className="setting-row crossfade-row">
          <span>
            Min match
            <small>Library search confidence threshold. {Math.round(searchThreshold * 100)}%</small>
          </span>
          <input
            className="crossfade-slider"
            type="range"
            min="0"
            max="100"
            value={Math.round(searchThreshold * 100)}
            style={{ "--progress": `${Math.round(searchThreshold * 100)}%` }}
            onChange={(event) => setSearchThreshold(Number(event.target.value) / 100)}
            onMouseUp={() => onSaveSearchThreshold && onSaveSearchThreshold(searchThreshold)}
            onTouchEnd={() => onSaveSearchThreshold && onSaveSearchThreshold(searchThreshold)}
          />
        </label>
        {user?.is_admin && (
          <label className="setting-row">
            <span>
              Player diagnostics
              <small>Live performance + network overlay on the player to diagnose buffering. Admin only.</small>
            </span>
            <input type="checkbox" checked={!!playerDiagnostics} onChange={(event) => onTogglePlayerDiagnostics?.(event.target.checked)} />
          </label>
        )}
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
      {canManageSettings(user) && (
        <section className="settings-section">
          <h2>Integrations</h2>
          {[
            ["jellyfin_url", "Jellyfin URL"],
            ["jellyfin_api_key", "Jellyfin API key"],
            ["slskd_url", "slskd URL"],
            ["slskd_api_key", "slskd API key"],
            ["acoustid_api_key", "AcoustID API key"],
            ["slskd_album_match_threshold", "slskd album match confidence"],
            ["slskd_album_folder_tries", "Album folder tries"],
            ["youtube_cookies_browser", "YouTube cookies browser"],
          ].map(([key, label]) => (
            <label className="setting-row integration-row" key={key}>
              <span>{label}</span>
              {key === "youtube_cookies_browser" ? (
                <select
                  value={integrationDraft[key] || ""}
                  onChange={(event) => setIntegrationDraft((current) => ({ ...current, [key]: event.target.value }))}
                >
                  <option value="">Browser</option>
                  {["Chrome", "Firefox", "Safari", "Edge", "Brave", "Other"].map((browser) => (
                    <option key={browser} value={browser.toLowerCase()}>
                      {browser}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  readOnly={key === "youtube_cookies_path"}
                  type={["slskd_album_match_threshold", "slskd_album_folder_tries", "slskd_concurrent_downloads"].includes(key) ? "number" : key.endsWith("api_key") && !shownIntegrationKeys[key] ? "password" : "text"}
                  min={key === "slskd_album_match_threshold" ? "50" : ["slskd_album_folder_tries", "slskd_concurrent_downloads"].includes(key) ? "1" : undefined}
                  max={key === "slskd_album_match_threshold" ? "95" : ["slskd_album_folder_tries", "slskd_concurrent_downloads"].includes(key) ? "12" : undefined}
                  step={["slskd_album_match_threshold", "slskd_album_folder_tries", "slskd_concurrent_downloads"].includes(key) ? "1" : undefined}
                  value={integrationDraft[key] || ""}
                  onChange={(event) => setIntegrationDraft((current) => ({ ...current, [key]: event.target.value }))}
                />
              )}
              {key.endsWith("api_key") && (
                <button className="secondary compact" type="button" onClick={() => setShownIntegrationKeys((current) => ({ ...current, [key]: !current[key] }))}>
                  {shownIntegrationKeys[key] ? "Hide" : "Show"}
                </button>
              )}
            </label>
          ))}
          <label className="setting-row integration-row">
            <span>YouTube cookies file</span>
            <span className="integration-status">
              {integrationDraft.youtube_cookies_uploaded ? "Uploaded" : "None"}
            </span>
            <button
              className="row-icon-button"
              type="button"
              onClick={() => cookiesUploadRef.current?.click()}
              title="Upload cookies.txt"
            >
              <Upload size={14} />
            </button>
            <input
              ref={cookiesUploadRef}
              type="file"
              accept=".txt,text/plain"
              style={{ display: "none" }}
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) onUploadYoutubeCookies?.(integrationDraft.youtube_cookies_browser || "", file);
              }}
            />
          </label>
          <button className="primary compact-button" onClick={() => onSaveIntegrations(integrationDraft)}>
            Save integrations
          </button>
        </section>
      )}
      {canManageSettings(user) && <MatchTuningSettings api={api} notify={notify} />}
      <SessionsPanel api={api} notify={notify} />
      {user?.is_admin && <SecuritySettings api={api} notify={notify} />}
      <footer className="settings-footer">
        Made by Poplel | <a href="https://poplel.xyz" target="_blank" rel="noreferrer">poplel.xyz</a>
      </footer>
    </div>
  );
}

function MatchTuningSettings({ api, notify }) {
  const [schema, setSchema] = useState([]);
  const [draft, setDraft] = useState({});
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let active = true;
    api("/settings/match-tuning")
      .then((data) => {
        if (!active || !data) return;
        setSchema(data.schema || []);
        setDraft(data.values || {});
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, []);

  async function save() {
    setSaving(true);
    try {
      const values = {};
      for (const field of schema) {
        const raw = draft[field.name];
        const num = Number(raw);
        values[field.name] = Number.isFinite(num) ? num : field.default;
      }
      const data = await api("/settings/match-tuning", { method: "PUT", body: JSON.stringify({ values }) });
      if (data) {
        setSchema(data.schema || schema);
        setDraft(data.values || values);
      }
      notify?.("Matching saved", "Download matching settings updated.");
    } catch (error) {
      notify?.("Matching failed", error?.message || "Could not save matching settings", "ui_error");
    } finally {
      setSaving(false);
    }
  }

  function resetDefaults() {
    setDraft((current) => {
      const next = { ...current };
      for (const field of schema) next[field.name] = field.default;
      return next;
    });
  }

  return (
    <section className="settings-section">
      <h2 className="settings-collapse-header" onClick={() => setOpen((value) => !value)} style={{ cursor: "pointer" }}>
        {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />} Download matching
      </h2>
      {open && (
        <>
          <p className="settings-hint">
            How Soulseek results are scored and ranked. Higher recall surfaces more candidates for review; everything still goes through the
            approval queue before downloading. Leave at defaults unless you know what you're tuning.
          </p>
          {schema.map((field) => (
            <label className="setting-row integration-row" key={field.name} title={field.help}>
              <span>{field.label}</span>
              <input
                type="number"
                min={field.min}
                max={field.max}
                step={field.step}
                value={draft[field.name] ?? field.default}
                onChange={(event) => setDraft((current) => ({ ...current, [field.name]: event.target.value }))}
              />
            </label>
          ))}
          <div className="settings-button-row">
            <button className="secondary compact-button" type="button" onClick={resetDefaults} disabled={saving}>
              Reset to defaults
            </button>
            <button className="primary compact-button" type="button" onClick={save} disabled={saving || !schema.length}>
              {saving ? "Saving…" : "Save matching"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function fmtTimeAgo(isoString) {
  if (!isoString) return "never";
  const diff = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function AlbumCard({ album, apiKey, onPlay, onQueue, onOpen, pinned, onTogglePin }) {
  const cover = albumCoverUrl(album, apiKey);
  const subtitle = album.artist || album.artist_name || "";
  return (
    <div className="album-card" title={`${album.title} — ${subtitle}`}>
      <div className="album-card-art" onClick={() => onOpen?.(album)} role="button" tabIndex={0}>
        {cover ? <img src={cover} alt="" loading="lazy" /> : <Music size={24} />}
        <span className="album-card-hover">
          {onTogglePin && (
            <button className={`album-card-pin${pinned ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); onTogglePin(album); }} title={pinned ? "Unpin from Home" : "Pin to Home"}>
              <Pin size={15} />
            </button>
          )}
          {onQueue && (
            <QueueButton className="album-card-queue" size={15} onClick={() => onQueue(album)} />
          )}
          {onPlay && (
            <button className="album-card-play" onClick={(e) => { e.stopPropagation(); onPlay(album); }} title="Play">
              <Play size={20} />
            </button>
          )}
        </span>
      </div>
      <div className="album-card-meta" onClick={() => onOpen?.(album)}>
        <span className="album-card-title">{album.title}</span>
        <span className="album-card-artist">{subtitle}</span>
      </div>
    </div>
  );
}

function ArtistCard({ artist, apiKey, onPlay, onQueue, onOpen, pinned, onTogglePin }) {
  const cover = artistCoverUrl(artist, apiKey);
  return (
    <div className="album-card artist-card" title={artist.name}>
      <div className="album-card-art" onClick={() => (onOpen ? onOpen(artist) : onPlay?.(artist))} role="button" tabIndex={0}>
        {cover ? <img src={cover} alt="" loading="lazy" /> : <Music size={24} />}
        <span className="album-card-hover">
          {onTogglePin && (
            <button className={`album-card-pin${pinned ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); onTogglePin(artist); }} title={pinned ? "Unpin from Home" : "Pin to Home"}>
              <Pin size={15} />
            </button>
          )}
          {onQueue && (
            <QueueButton className="album-card-queue" size={15} onClick={() => onQueue(artist)} />
          )}
          {onPlay && (
            <button className="album-card-play" onClick={(e) => { e.stopPropagation(); onPlay(artist); }} title="Play">
              <Play size={20} />
            </button>
          )}
        </span>
      </div>
      <div className="album-card-meta" onClick={() => (onOpen ? onOpen(artist) : onPlay?.(artist))}>
        <span className="album-card-title">{artist.name}</span>
      </div>
    </div>
  );
}

function AlbumDetailPage({ detail, api, apiKey, onBack, onPlayAlbum, onQueueAlbum, onPlayTracks, onQueueTracks, pinned, onTogglePin }) {
  const [tracks, setTracks] = useState(null);
  useEffect(() => {
    let active = true;
    setTracks(null);
    api(`/library/tracks?album_id=${encodeURIComponent(detail.id)}&page_size=500`)
      .then((d) => { if (active) setTracks(d?.items || []); })
      .catch(() => { if (active) setTracks([]); });
    return () => { active = false; };
  }, [api, detail.id]);
  const cover = albumCoverUrl({ id: detail.id, cover_path: detail.cover_path }, apiKey)
    || `${API_BASE}/library/albums/${encodeURIComponent(detail.id)}/cover?api_key=${encodeURIComponent(apiKey)}`;
  const viewCtx = { onPlay: onPlayTracks, onQueue: onQueueTracks, canEditMetadata: false, canRemoveLibrary: false, canUsePlaylists: false };
  const albumObj = { id: detail.id, title: detail.title, _coverUrl: cover, tracks: tracks || [] };
  const artistObj = { name: detail.artist_name };
  return (
    <div className="album-detail-overlay">
      <div className="album-detail-head">
        <button className="secondary compact" onClick={onBack}><ArrowLeft size={16} /> Back</button>
      </div>
      <div className="album-detail-hero">
        <div className="album-detail-cover">{cover ? <img src={cover} alt="" /> : <Music size={48} />}</div>
        <div className="album-detail-info">
          <h1>{detail.title}</h1>
          <p className="muted">{detail.artist_name}</p>
          <p className="muted">{tracks ? `${tracks.length} track${tracks.length === 1 ? "" : "s"}` : ""}</p>
          <div className="album-detail-actions">
            <button onClick={() => onPlayAlbum(detail)}><Play size={15} /> Play</button>
            <button className="secondary" onClick={() => onQueueAlbum(detail)}><ListPlus size={15} /> Queue</button>
            {onTogglePin && (
              <button className={`secondary${pinned ? " active" : ""}`} onClick={() => onTogglePin(detail)}>
                <Pin size={15} /> {pinned ? "Pinned" : "Pin"}
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="album-detail-tracks">
        {tracks === null ? (
          <p className="muted">Loading…</p>
        ) : tracks.length === 0 ? (
          <p className="muted">No tracks.</p>
        ) : (
          tracks.map((t) => (
            <LibraryTrackBranch key={t.id} ctx={viewCtx} artist={artistObj} album={albumObj} track={t} depth={0} />
          ))
        )}
      </div>
    </div>
  );
}

function ArtistDetailPage({ detail, api, apiKey, onBack, onPlayArtist, onQueueArtist, onPlayTracks, onQueueTracks, onOpenAlbum, pinned, onTogglePin, library }) {
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const node = useMemo(() => (library || []).find((a) => a.id === detail.id), [library, detail.id]);
  const albums = useMemo(() => {
    if (!node) return [];
    return node.albums
      .filter((al) => (al.tracks?.length || 0) > 0)
      .map((al) => ({ ...al, _coverUrl: albumCoverUrl(al, apiKey) }))
      .sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  }, [node, apiKey]);
  const cover = artistCoverUrl({ id: detail.id, cover_path: detail.cover_path }, apiKey)
    || `${API_BASE}/library/artists/${encodeURIComponent(detail.id)}/cover?api_key=${encodeURIComponent(apiKey)}`;
  const viewCtx = {
    onPlay: onPlayTracks, onQueue: onQueueTracks,
    canEditMetadata: false, canRemoveLibrary: false, canUsePlaylists: false,
    openAlbums, setOpenAlbums,
    onOpenAlbum,
  };
  return (
    <div className="album-detail-overlay">
      <div className="album-detail-head">
        <button className="secondary compact" onClick={onBack}><ArrowLeft size={16} /> Back</button>
      </div>
      <div className="album-detail-hero">
        <div className="album-detail-cover artist-detail-cover">{cover ? <img src={cover} alt="" /> : <Music size={48} />}</div>
        <div className="album-detail-info">
          <h1>{detail.name}</h1>
          <p className="muted">{`${albums.length} album${albums.length === 1 ? "" : "s"}`}</p>
          <div className="album-detail-actions">
            <button onClick={() => onPlayArtist(detail)}><Play size={15} /> Play</button>
            <button className="secondary" onClick={() => onQueueArtist(detail)}><ListPlus size={15} /> Queue</button>
            {onTogglePin && (
              <button className={`secondary${pinned ? " active" : ""}`} onClick={() => onTogglePin(detail)}>
                <Pin size={15} /> {pinned ? "Pinned" : "Pin"}
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="album-detail-tracks tree">
        {albums.length === 0 ? (
          <p className="muted">No albums.</p>
        ) : (
          albums.map((al) => (
            <LibraryAlbumBranch key={al.id} ctx={viewCtx} artist={node} album={al} depth={0} />
          ))
        )}
      </div>
    </div>
  );
}

function HomeView({ api, apiKey, onPlayAlbum, onQueueAlbum, onPlayPlaylist, onOpenAlbum, onPlayArtist, onOpenArtist, onQueueArtist, onPlayTracks, onQueueTracks, pinnedAlbumIds, onTogglePinAlbum, pinnedArtistIds, onTogglePinArtist, homeVersion, onUnpinPlaylist, onPlayAll, onShuffleAll }) {
  const [home, setHome] = useState(null);
  const recentToTrack = (p) => ({
    id: p.track_id,
    title: p.title,
    _artist: p.artist,
    _album: p.album,
    album_id: p.album_id,
    _coverUrl: p.album_id ? `${API_BASE}/library/albums/${encodeURIComponent(p.album_id)}/cover?api_key=${encodeURIComponent(apiKey)}` : undefined,
  });
  useEffect(() => {
    let active = true;
    api("/me/home")
      .then((data) => { if (active) setHome(data); })
      .catch(() => { if (active) setHome({ recently_added: [], recently_approved: [], recent_plays: [], favorites: null, pinned_playlists: [], pinned_albums: [], pinned_artists: [] }); });
    return () => { active = false; };
  }, [api, homeVersion]);

  if (!home) return <div className="home-view"><p className="muted">Loading…</p></div>;

  const fmt = (iso) => (iso ? new Date(iso).toLocaleDateString() : "");

  return (
    <div className="home-view">
      <section className="home-section">
        <h2>Favorites &amp; pinned</h2>
        <div className="home-pin-row">
          {onPlayAll && (
            <button className="home-pin-card" onClick={() => onPlayAll()}>
              <Play size={16} />
              <span className="home-list-main">Play library</span>
              <span className="home-list-sub">Whole library</span>
            </button>
          )}
          {onShuffleAll && (
            <button className="home-pin-card" onClick={() => onShuffleAll()}>
              <Shuffle size={16} />
              <span className="home-list-main">Shuffle library</span>
              <span className="home-list-sub">Whole library</span>
            </button>
          )}
          <button className="home-pin-card" onClick={() => onPlayPlaylist("favorites")}>
            <Heart size={16} />
            <span className="home-list-main">Favorites</span>
            <span className="home-list-sub">{home.favorites ? `${home.favorites.track_count} tracks` : "—"}</span>
          </button>
          {home.pinned_playlists.map((p) => (
            <div key={p.playlist_id} className="home-pin-card-wrap">
              <button className="home-pin-card" onClick={() => onPlayPlaylist(p.playlist_id)}>
                <Pin size={15} />
                <span className="home-list-main">{p.name}</span>
                <span className="home-list-sub">{p.track_count != null ? `${p.track_count} tracks` : ""}</span>
              </button>
              {onUnpinPlaylist && (
                <button className="home-pin-unpin icon-button" title="Unpin" onClick={() => onUnpinPlaylist(p.playlist_id)}>
                  <X size={14} />
                </button>
              )}
            </div>
          ))}
          {home.pinned_playlists.length === 0 && <p className="muted">Pin playlists from the Playlists page.</p>}
        </div>
        {home.pinned_artists?.length > 0 && (
          <div className="home-album-grid home-pinned-grid">
            {home.pinned_artists.map((ar) => (
              <ArtistCard key={ar.id} artist={ar} apiKey={apiKey} onPlay={onPlayArtist} onQueue={onQueueArtist} onOpen={onOpenArtist} pinned={pinnedArtistIds?.has(ar.id)} onTogglePin={onTogglePinArtist} />
            ))}
          </div>
        )}
        {home.pinned_albums?.length > 0 && (
          <div className="home-album-grid home-pinned-grid">
            {home.pinned_albums.map((al) => (
              <AlbumCard key={al.id} album={al} apiKey={apiKey} onPlay={onPlayAlbum} onQueue={onQueueAlbum} onOpen={onOpenAlbum} pinned={pinnedAlbumIds?.has(al.id)} onTogglePin={onTogglePinAlbum} />
            ))}
          </div>
        )}
      </section>

      <section className="home-section">
        <h2>Recently added</h2>
        {home.recently_added.length === 0 ? (
          <p className="muted">Nothing added yet.</p>
        ) : (
          <div className="home-album-grid">
            {home.recently_added.map((al) => (
              <AlbumCard key={al.id} album={al} apiKey={apiKey} onPlay={onPlayAlbum} onQueue={onQueueAlbum} onOpen={onOpenAlbum} pinned={pinnedAlbumIds?.has(al.id)} onTogglePin={onTogglePinAlbum} />
            ))}
          </div>
        )}
      </section>

      <div className="home-columns">
        <section className="home-section">
          <h2>Recent plays</h2>
          {home.recent_plays.length === 0 ? (
            <p className="muted">No plays yet.</p>
          ) : (
            <ul className="home-list">
              {home.recent_plays.map((p, i) => (
                <li key={`${p.track_id}-${i}`} className="home-list-row">
                  <div
                    className={`home-list-text${p.track_id && onPlayTracks ? " home-list-text-play" : ""}`}
                    onClick={p.track_id && onPlayTracks ? () => onPlayTracks([recentToTrack(p)]) : undefined}
                    role={p.track_id && onPlayTracks ? "button" : undefined}
                    title={p.track_id && onPlayTracks ? "Play" : undefined}
                  >
                    <span className="home-list-main">{p.title || "Unknown"}</span>
                    <span className="home-list-sub">{p.artist || ""}</span>
                  </div>
                  {p.track_id && (onPlayTracks || onQueueTracks) && (
                    <div className="home-list-actions">
                      {onPlayTracks && (
                        <button className="row-icon-button" title="Play" onClick={() => onPlayTracks([recentToTrack(p)])}>
                          <Play size={14} />
                        </button>
                      )}
                      {onQueueTracks && (
                        <QueueButton onClick={() => onQueueTracks([recentToTrack(p)])} />
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="home-section">
          <h2>Recently approved</h2>
          {home.recently_approved.length === 0 ? (
            <p className="muted">No approved wishlist items yet.</p>
          ) : (
            <ul className="home-list">
              {home.recently_approved.map((w) => (
                <li key={w.id}>
                  <span className="home-list-main">{w.track || w.album || w.artist}</span>
                  <span className="home-list-sub">{w.track || w.album ? w.artist : ""}{w.approved_at ? ` · ${fmt(w.approved_at)}` : ""}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

function AutomationsInspector({ api }) {
  const [automations, setAutomations] = useState(null);
  useEffect(() => {
    let active = true;
    const load = () => api("/automations").then((d) => { if (active) setAutomations(d || []); }).catch(() => {});
    load();
    const t = setInterval(load, 10000);
    return () => { active = false; clearInterval(t); };
  }, [api]);
  if (!automations) return null;
  const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : "Never");
  const enabled = automations.filter((a) => a.enabled).length;
  const lastRun = automations.map((a) => a.last_run_at).filter(Boolean).sort().slice(-1)[0];
  const now = Date.now();
  const nextRun = automations.map((a) => a.next_run_at).filter((d) => d && new Date(d).getTime() >= now).sort()[0];
  const rows = [
    ["Automations", `${automations.length}${automations.length ? ` · ${enabled} enabled` : ""}`],
    ["Last run", fmt(lastRun)],
    ["Next run", fmt(nextRun)],
  ];
  return (
    <div className="inspector-section">
      <div className="inspector-section-label">Automations</div>
      <dl className="library-top-list">
        {rows.map(([label, value]) => (
          <div key={label} className="library-top-row">
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function LibraryTopStats({ api }) {
  const [top, setTop] = useState(null);
  useEffect(() => {
    let active = true;
    api("/library/top?days=30")
      .then((data) => { if (active) setTop(data); })
      .catch(() => { if (active) setTop({ artist: null, album: null, track: null }); });
    return () => { active = false; };
  }, [api]);
  if (!top) return null;
  const rows = [
    ["Top artist", top.artist && `${top.artist.name} · ${top.artist.plays} play${top.artist.plays === 1 ? "" : "s"}`],
    ["Top album", top.album && `${top.album.title} · ${top.album.plays} play${top.album.plays === 1 ? "" : "s"}`],
    ["Top track", top.track && `${top.track.title} · ${top.track.plays} play${top.track.plays === 1 ? "" : "s"}`],
  ];
  return (
    <div className="inspector-section library-top">
      <div className="inspector-section-label">Last 30 days</div>
      {rows.every(([, v]) => !v) ? (
        <p className="inspector-hint">No plays recorded yet.</p>
      ) : (
        <dl className="library-top-list">
          {rows.map(([label, value]) => (
            <div key={label} className="library-top-row">
              <dt>{label}</dt>
              <dd>{value || "—"}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

// Recursively read a webkit FileSystemEntry into [{ file, path }], preserving folder
// structure (path stays relative to the dropped item).
function readFsEntry(entry, base = "") {
  return new Promise((resolve) => {
    if (!entry) return resolve([]);
    if (entry.isFile) {
      entry.file(
        (file) => resolve([{ file, path: base + entry.name }]),
        () => resolve([]),
      );
      return;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const acc = [];
      const readBatch = () => {
        reader.readEntries(
          (batch) => {
            if (!batch.length) {
              Promise.all(acc.map((child) => readFsEntry(child, `${base}${entry.name}/`))).then((nested) => resolve(nested.flat()));
            } else {
              acc.push(...batch);
              readBatch(); // readEntries yields ≤100 entries per call — keep going until empty
            }
          },
          () => resolve([]),
        );
      };
      readBatch();
      return;
    }
    resolve([]);
  });
}

// Pull a dropped mix of files and folders into [{ file, path }]. The DataTransfer
// item list and webkitGetAsEntry() must be read synchronously, so grab the entries
// before the first await.
async function collectDroppedItems(dataTransfer) {
  const items = dataTransfer?.items ? Array.from(dataTransfer.items) : [];
  const entries = items.map((it) => it.webkitGetAsEntry?.()).filter(Boolean);
  if (entries.length) {
    const groups = await Promise.all(entries.map((entry) => readFsEntry(entry)));
    return groups.flat();
  }
  return Array.from(dataTransfer?.files || []).map((file) => ({ file, path: file.name }));
}

function Inspector({
  page,
  api,
  library,
  importFiles,
  importDownloadRequests,
  approvals,
  wishlist,
  playlists,
  queueItemCount,
  queueSelectionCount,
  tasks,
  downloadProgress,
  importActions,
  wishlistActions,
  playlistActions,
  mappingSyncStats,
  playlistImportActions,
}) {
  const importUploadRef = useRef(null);
  const importFolderRef = useRef(null);
  const [confirmClearImport, setConfirmClearImport] = useState(false);
  const [importDragOver, setImportDragOver] = useState(false);
  const stats = inspectorStats({
    page,
    library,
    importFiles,
    importDownloadRequests,
    approvals,
    wishlist,
    playlists,
    queueItemCount,
    queueSelectionCount,
    tasks,
    mappingSyncStats,
  });
  return (
    <aside className="panel inspector">
      <h2>Inspector</h2>
      {page === "Library" && <LibraryTopStats api={api} />}
      {page === "Automations" && <AutomationsInspector api={api} />}
      {page === "Import/Add" && importActions && (
        <div className="inspector-actions">
          <button className="primary" onClick={importActions.onScan} disabled={importActions.loading}>
            <RefreshCw size={16} />
            Scan import folder
          </button>
          <div
            className={`import-dropzone${importDragOver ? " dragover" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setImportDragOver(true); }}
            onDragLeave={() => setImportDragOver(false)}
            onDrop={async (event) => {
              event.preventDefault();
              setImportDragOver(false);
              if (importActions.uploadProgress != null) return;
              const collected = await collectDroppedItems(event.dataTransfer);
              if (collected.length) importActions.onUpload?.(collected);
            }}
          >
            <Upload size={18} />
            <span>Drop files or folders here</span>
          </div>
          <button className="secondary" type="button" disabled={importActions.uploadProgress != null} onClick={() => importUploadRef.current?.click()}>
            <Upload size={16} />
            Upload files
          </button>
          <button className="secondary" type="button" disabled={importActions.uploadProgress != null} onClick={() => importFolderRef.current?.click()}>
            <Folder size={16} />
            Upload folder
          </button>
          <input
            ref={importFolderRef}
            type="file"
            webkitdirectory=""
            directory=""
            multiple
            style={{ display: "none" }}
            onChange={(event) => {
              const picked = Array.from(event.target.files || []);
              event.target.value = "";
              importActions.onUpload?.(picked);
            }}
          />
          {importActions.uploadProgress != null && (
            <>
              <InlineProgress value={importActions.uploadProgress * 100} label="Uploading" />
              <button className="secondary" type="button" onClick={importActions.onCancelUpload}>
                <X size={16} />
                Cancel upload
              </button>
            </>
          )}
          <input
            ref={importUploadRef}
            type="file"
            accept="audio/*,.flac,.alac,.m4a,.wav,.aiff,.aif,.mp3,.ogg,.opus"
            multiple
            style={{ display: "none" }}
            onChange={(event) => {
              // Snapshot before clearing the input — event.target.files is a live
              // FileList, so resetting value first would empty it.
              const picked = Array.from(event.target.files || []);
              event.target.value = "";
              importActions.onUpload?.(picked);
            }}
          />
          <button className="secondary" onClick={importActions.onToggleAlbumSearch}>
            <Plus size={16} />
            Add album
          </button>
          <button
            className={`secondary${!importActions.disabled && !importActions.activeImportTask ? " action-ready" : ""}`}
            onClick={importActions.onPropose}
            disabled={importActions.disabled}
          >
            {importActions.activeImportTask
              ? "Import review running"
              : !importActions.downloadCount && !importActions.hasFiles && importActions.hasPendingPlaylist
              ? "Create/Update playlist"
              : `Add to task queue${importActions.downloadCount ? ` (${importActions.downloadCount} downloads)` : ""}`}
          </button>
          {confirmClearImport ? (
            <button className="secondary danger" type="button" disabled={importActions.loading} onClick={() => { setConfirmClearImport(false); importActions.onClearFolder?.(); }}>
              <Trash2 size={16} />
              Confirm: delete all import files
            </button>
          ) : (
            <button className="secondary" type="button" disabled={importActions.loading || !importActions.hasFiles} onClick={() => setConfirmClearImport(true)}>
              <Trash2 size={16} />
              Clear import folder
            </button>
          )}
        </div>
      )}
      {page === "Wishlist" && wishlistActions && (
        <div className="inspector-actions">
          <button className="secondary" onClick={wishlistActions.onToggleAlbumSearch}>
            <Plus size={16} />
            Add album
          </button>
          {wishlistActions.canApproveAll && (
            <button className="primary" onClick={wishlistActions.onSubmitSelected} disabled={wishlistActions.selectedCount === 0}>
              <ListChecks size={16} />
              Add selected to task queue
            </button>
          )}
        </div>
      )}
      {page === "Import/Add" && playlistImportActions && (
        <div className="inspector-section">
          <button
            className="secondary inspector-section-toggle"
            onClick={() => playlistImportActions.setOpen((o) => !o)}
          >
            <Music size={15} />
            Import from playlist
            {playlistImportActions.open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
          {playlistImportActions.open && (
            <div className="inspector-section-content">
              <input
                className="playlist-import-url"
                placeholder="Spotify or Apple Music playlist URL"
                value={playlistImportActions.url}
                onChange={(e) => playlistImportActions.setUrl(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && playlistImportActions.url.trim())
                    playlistImportActions.onImport(playlistImportActions.url.trim(), playlistImportActions.mode);
                }}
              />
              <div className="mode-toggle">
                <button
                  className={playlistImportActions.mode === "songs" ? "active" : ""}
                  onClick={() => playlistImportActions.setMode("songs")}
                >
                  Songs
                </button>
                <button
                  className={playlistImportActions.mode === "albums" ? "active" : ""}
                  onClick={() => playlistImportActions.setMode("albums")}
                >
                  Albums
                </button>
              </div>
              <button
                className="primary"
                onClick={() => playlistImportActions.onImport(playlistImportActions.url.trim(), playlistImportActions.mode)}
                disabled={!playlistImportActions.url.trim() || playlistImportActions.loading}
              >
                {playlistImportActions.loading
                  ? (playlistImportActions.mode === "albums" ? "Looking up albums…" : "Importing…")
                  : "Import playlist"}
              </button>
            </div>
          )}
        </div>
      )}
      {page === "Playlists" && playlistActions && (
        <div className="inspector-actions">
          <input
            value={playlistActions.playlistName}
            onChange={(event) => playlistActions.onPlaylistNameChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") playlistActions.onCreate();
            }}
            placeholder="New playlist"
          />
          <button className="secondary" onClick={playlistActions.onCreate} disabled={!playlistActions.playlistName.trim()}>
            <Plus size={16} />
            Create playlist
          </button>
        </div>
      )}
      {downloadProgress && (
        <div className="inspector-progress-card">
          <strong>Downloads</strong>
          <InlineProgress value={downloadProgress.percent} label={downloadProgress.label} indeterminate={downloadProgress.indeterminate} />
          <small>{downloadProgress.detail}</small>
        </div>
      )}
      <ActiveWorkBar tasks={tasks} />
      {stats.rows.length > 0 && (
        <div className="metadata-grid inspector-stats">
          {stats.summary && (
            <>
              <label>Selection</label>
              <strong>{stats.summary}</strong>
            </>
          )}
          {stats.rows.map(([label, value]) => (
            <React.Fragment key={label}>
              <label>{label}</label>
              <strong>{value}</strong>
            </React.Fragment>
          ))}
        </div>
      )}
    </aside>
  );
}

function inspectorStats({
  page,
  library = [],
  importFiles = [],
  importDownloadRequests = [],
  approvals = [],
  wishlist = [],
  playlists = [],
  queueItemCount = 0,
  queueSelectionCount = 0,
  tasks = [],
  mappingSyncStats = null,
}) {
  if (page === "Library") {
    return { summary: "", rows: musicStatRows(countLibraryMusic(library)) };
  }
  if (page === "Import/Add") {
    const stats = countImportMusic(importFiles, importDownloadRequests);
    const selected = stats.tracks;
    const ready = importDownloadRequests.length;
    return { summary: `${selected} selected · ${ready} ready`, rows: musicStatRows(stats) };
  }
  if (page === "Task Queue") {
    const stats = countApprovalMusic(approvals.filter((batch) => batch.status !== "executing"));
    return {
      summary: `${queueSelectionCount} selected · ${queueItemCount} ready`,
      rows: musicStatRows(stats),
    };
  }
  if (page === "Wishlist") {
    return { summary: "", rows: musicStatRows(countWishlistMusic(wishlist)) };
  }
  if (page === "Playlists") {
    const stats = countPlaylistMusic(playlists);
    return { summary: "", rows: [["Playlists", playlists.length], ...musicStatRows(stats)] };
  }
  if (page === "Activity") {
    const queued = tasks.filter((task) => task.status === "queued").length;
    const running = tasks.filter((task) => task.status === "running").length;
    const failed = tasks.filter((task) => task.status === "failed").length;
    return { summary: "", rows: [["Running", running], ["Queued", queued], ["Failed", failed]] };
  }
  if (page === "Tools") {
    const rows = [];
    if (mappingSyncStats) {
      const lastRun = mappingSyncStats.last_run_at ? fmtTimeAgo(mappingSyncStats.last_run_at) : "never";
      rows.push(["Track remap", lastRun], ["Remap runs", mappingSyncStats.run_count]);
    }
    return { summary: "", rows };
  }
  return { summary: "", rows: [] };
}

function musicStatRows(stats) {
  return [
    ["Artists", stats.artists || 0],
    ["Albums", stats.albums || 0],
    ["Tracks", stats.tracks || 0],
  ];
}

function countLibraryMusic(artists = []) {
  const albumCount = artists.reduce((total, artist) => total + (artist.albums || []).length, 0);
  const trackCount = artists.reduce(
    (total, artist) => total + (artist.albums || []).reduce((albumTotal, album) => albumTotal + (album.tracks || []).length, 0),
    0,
  );
  return { artists: artists.length, albums: albumCount, tracks: trackCount };
}

function countImportMusic(files = [], requests = []) {
  const refs = [
    ...files.map((file) => ({
      artist: file.metadata?.artist || "Unknown Artist",
      album: file.metadata?.album || "Unknown Album",
      track: file.metadata?.title || file.name || file.path,
    })),
    ...requests.map((request) => ({
      artist: request.artist || "Unknown Artist",
      album: request.album || "Unknown Album",
      track: request.track || request.title,
    })),
  ];
  return countMusicRefs(refs);
}

function countApprovalMusic(batches = [], downloadsOnly = false) {
  const items = downloadsOnly ? visibleDownloadItems(batches) : batches.flatMap((batch) => batch.items || []);
  const leaves = lowestLevelItems(items);
  const actionLeaves = leaves.filter((item) => !["artist", "album"].includes(item.kind));
  const selected = actionLeaves.filter((item) => item.selected).length;
  const ready = actionLeaves.filter((item) => item.selected && isReadyApprovalItem(item)).length;
  return { ...countMusicRefs(actionLeaves.map(itemMusicRef)), selected, ready };
}

function countWishlistMusic(items = []) {
  return countMusicRefs(
    items
      .filter((item) => item.status !== "removed")
      .map((item) => ({ artist: item.artist, album: item.album, track: item.track || item.title })),
  );
}

function countPlaylistMusic(playlists = []) {
  return countMusicRefs(
    playlists.flatMap((playlist) =>
      (playlist.tracks || []).map((track) => ({ artist: track.artist, album: track.album, track: track.title || track.name })),
    ),
  );
}

function countMusicRefs(refs = []) {
  const artists = new Set();
  const albums = new Set();
  let tracks = 0;
  refs.forEach((ref) => {
    if (ref.artist) artists.add(normalizeName(ref.artist));
    if (ref.artist || ref.album) albums.add(`${normalizeName(ref.artist)}::${normalizeName(ref.album)}`);
    if (ref.track) tracks += 1;
  });
  return { artists: artists.size, albums: albums.size, tracks };
}

function itemMusicRef(item) {
  const payload = parseJsonObject(item.payload_json);
  const request = payload.request || payload;
  return {
    artist: request.artist || payload.artist,
    album: request.album || payload.album,
    track: request.track || request.title || payload.track || payload.title || item.title,
  };
}

function isReadyApprovalItem(item) {
  const status = String(itemStatusMeta(item) || item.status || "").toLowerCase();
  return ["pending", "approved"].includes(item.status) || /candidate ready|pending|approved|ready/.test(status);
}

function isExecutableApprovalItem(item) {
  if (["executing", "completed", "rejected"].includes(item.status)) return false;
  const payload = parseJsonObject(item.payload_json);
  if (item.kind === "import_files") return Boolean(item.old_value && item.new_value);
  if (item.kind === "metadata") return Boolean(payload.target_type);
  if (["delete", "file_move", "playlist", "download", "lyrics"].includes(item.kind)) return Boolean(payload.action);
  return false;
}

function isCandidateSearchItem(item) {
  const payload = parseJsonObject(item.payload_json);
  const status = String(payload.status || item.status || "").toLowerCase();
  if (!status) return false;
  if (/candidate ready|review ready|ready|approved|completed|done|failed|needs attention|rejected/.test(status)) return false;
  return /searching|preparing/.test(status) && /candidate|download|slskd|track/.test(status);
}

function Toast({ title, body, onClose }) {
  return (
    <button className="toast" onClick={onClose}>
      <strong>{title}</strong>
      <span>{body}</span>
    </button>
  );
}

function parseLrc(text) {
  if (!text) return [];
  const lines = [];
  for (const raw of text.split("\n")) {
    const m = raw.match(/^\[(\d+):(\d+(?:\.\d+)?)\](.*)/);
    if (m) {
      const time = parseInt(m[1]) * 60 + parseFloat(m[2]);
      const lineText = m[3].trim();
      if (lineText) lines.push({ time, text: lineText });
    }
  }
  return lines.sort((a, b) => a.time - b.time);
}

// ReplayGain as a linear multiplier (can exceed 1 to boost quiet tracks). NULL = 1 (no change).
// A master limiter downstream catches any clipping the boost would cause.
function replayGainLinear(track) {
  const gain = track?.replaygain_track_gain;
  if (gain == null || Number.isNaN(Number(gain))) return 1;
  return Math.pow(10, Number(gain) / 20);
}

const DIAG_READY_STATES = ["HAVE_NOTHING", "HAVE_METADATA", "HAVE_CURRENT_DATA", "HAVE_FUTURE_DATA", "HAVE_ENOUGH_DATA"];
const DIAG_NETWORK_STATES = ["EMPTY", "IDLE", "LOADING", "NO_SOURCE"];

function diagBufferedAhead(el) {
  if (!el || !el.buffered || el.buffered.length === 0) return 0;
  const t = el.currentTime;
  for (let i = 0; i < el.buffered.length; i++) {
    if (t >= el.buffered.start(i) - 0.25 && t <= el.buffered.end(i) + 0.25) return Math.max(0, el.buffered.end(i) - t);
  }
  return 0;
}
function diagBufferedTotal(el) {
  if (!el || !el.buffered) return 0;
  let s = 0;
  for (let i = 0; i < el.buffered.length; i++) s += el.buffered.end(i) - el.buffered.start(i);
  return s;
}
function diagTail(url) {
  if (!url) return "—";
  return url.split("?")[0].split("/").slice(-2).join("/");
}

function PlayerDiagnostics({ audioARef, audioBRef, activeKeyRef, audioCtxRef, gainNodesRef, limiterRef, loadedUrlRef, crossfadingRef, currentTrack, audioUrl, nextAudioUrl, crossfadeDuration }) {
  const [m, setM] = useState({});
  const [collapsed, setCollapsed] = useState(false);
  const stallsRef = useRef({ count: 0, totalMs: 0, lastStallStart: 0, inStall: false, startupMs: null, loadStart: 0, lastUrl: null });
  const fpsRef = useRef({ frames: 0, last: performance.now(), fps: 0 });

  useEffect(() => {
    let raf;
    const tick = () => {
      const f = fpsRef.current;
      f.frames += 1;
      const now = performance.now();
      if (now - f.last >= 1000) { f.fps = Math.round((f.frames * 1000) / (now - f.last)); f.frames = 0; f.last = now; }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);

  useEffect(() => {
    const els = [audioARef.current, audioBRef.current].filter(Boolean);
    const onWaiting = () => { const s = stallsRef.current; if (!s.inStall) { s.inStall = true; s.lastStallStart = performance.now(); s.count += 1; } };
    const onPlaying = () => {
      const s = stallsRef.current;
      if (s.inStall) { s.totalMs += performance.now() - s.lastStallStart; s.inStall = false; }
      if (s.startupMs == null && s.loadStart) s.startupMs = performance.now() - s.loadStart;
    };
    const onLoadStart = () => { const s = stallsRef.current; s.loadStart = performance.now(); s.startupMs = null; };
    for (const el of els) {
      el.addEventListener("waiting", onWaiting);
      el.addEventListener("stalled", onWaiting);
      el.addEventListener("playing", onPlaying);
      el.addEventListener("loadstart", onLoadStart);
    }
    return () => {
      for (const el of els) {
        el.removeEventListener("waiting", onWaiting);
        el.removeEventListener("stalled", onWaiting);
        el.removeEventListener("playing", onPlaying);
        el.removeEventListener("loadstart", onLoadStart);
      }
    };
  }, [audioARef, audioBRef]);

  useEffect(() => {
    const s = stallsRef.current;
    if (audioUrl && audioUrl !== s.lastUrl) { s.lastUrl = audioUrl; s.loadStart = performance.now(); s.startupMs = null; }
  }, [audioUrl]);

  useEffect(() => {
    const id = setInterval(() => {
      const activeKey = activeKeyRef.current;
      const el = activeKey === "a" ? audioARef.current : audioBRef.current;
      const ctx = audioCtxRef.current;
      const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection || null;
      const s = stallsRef.current;

      let kbps = null, bytes = 0, ttfb = null, reqCount = 0;
      try {
        const base = (audioUrl || "").split("?")[0];
        if (base) {
          const ents = performance.getEntriesByType("resource").filter((e) => e.name.split("?")[0] === base);
          reqCount = ents.length;
          let totDur = 0;
          for (const e of ents) { bytes += e.transferSize || e.encodedBodySize || 0; totDur += e.duration || 0; }
          if (totDur > 0 && bytes > 0) kbps = Math.round((bytes * 8) / totDur);
          const last = ents[ents.length - 1];
          if (last && last.responseStart && last.requestStart) ttfb = Math.round(last.responseStart - last.requestStart);
        }
      } catch { /* ignore */ }

      const gainNode = gainNodesRef.current ? gainNodesRef.current[activeKey] : null;
      const playTime = el ? el.currentTime : 0;
      let decoded = null;
      try { if (el && typeof el.webkitAudioDecodedByteCount === "number") decoded = el.webkitAudioDecodedByteCount; } catch { /* ignore */ }
      setM({
        playing: el ? !el.paused : false,
        readyState: el ? DIAG_READY_STATES[el.readyState] : "—",
        networkState: el ? DIAG_NETWORK_STATES[el.networkState] : "—",
        currentTime: playTime,
        duration: el && isFinite(el.duration) ? el.duration : 0,
        playbackRate: el ? el.playbackRate : 1,
        bufferAhead: diagBufferedAhead(el),
        bufferedTotal: diagBufferedTotal(el),
        bufferedRanges: el && el.buffered ? el.buffered.length : 0,
        seekableEnd: el && el.seekable && el.seekable.length ? el.seekable.end(el.seekable.length - 1) : 0,
        volume: el ? el.volume : 0,
        mediaError: el && el.error ? `code ${el.error.code}` : "none",
        rebuffers: s.count,
        stalledMs: Math.round(s.totalMs + (s.inStall ? performance.now() - s.lastStallStart : 0)),
        inStall: s.inStall,
        startupMs: s.startupMs != null ? Math.round(s.startupMs) : null,
        underrun: playTime > 0 ? (s.totalMs / 1000) / playTime : 0,
        kbps, bytes, ttfb, reqCount,
        connType: conn && conn.effectiveType ? conn.effectiveType : "—",
        downlink: conn && conn.downlink != null ? conn.downlink : null,
        rtt: conn && conn.rtt != null ? conn.rtt : null,
        saveData: !!(conn && conn.saveData),
        online: navigator.onLine,
        ctxState: ctx ? ctx.state : "—",
        sampleRate: ctx ? ctx.sampleRate : null,
        baseLatency: ctx && ctx.baseLatency != null ? ctx.baseLatency : null,
        outputLatency: ctx && ctx.outputLatency != null ? ctx.outputLatency : null,
        gain: gainNode ? gainNode.gain.value : null,
        limiterDb: limiterRef.current ? limiterRef.current.reduction : null,
        replayGainDb: currentTrack && currentTrack.replaygain_track_gain != null ? currentTrack.replaygain_track_gain : null,
        activeKey,
        crossfading: !!(crossfadingRef && crossfadingRef.current),
        nextPreloaded: !!nextAudioUrl,
        crossfadeDuration,
        format: (currentTrack && currentTrack.format) || "—",
        bitrate: currentTrack && currentTrack.bitrate ? currentTrack.bitrate : null,
        lossless: currentTrack ? !!currentTrack.is_lossless : false,
        trackId: (currentTrack && currentTrack.id) || "—",
        decodedBytes: decoded,
        heapMB: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : null,
        fps: fpsRef.current.fps,
        dpr: window.devicePixelRatio,
      });
    }, 250);
    return () => clearInterval(id);
  }, [audioUrl, currentTrack, nextAudioUrl, crossfadeDuration]);

  const num = (v, d = 1) => (typeof v === "number" && isFinite(v) ? v.toFixed(d) : "—");
  const sections = [
    ["Playback", [
      ["State", m.inStall ? "STALLING" : (m.playing ? "playing" : "paused"), m.inStall ? "#ff5a5a" : (m.playing ? "#37c871" : undefined)],
      ["Ready", m.readyState],
      ["Network", m.networkState],
      ["Buffer ahead", `${num(m.bufferAhead)}s`, m.bufferAhead < 2 ? "#ff5a5a" : m.bufferAhead < 5 ? "#ffb454" : "#37c871"],
      ["Buffered", `${num(m.bufferedTotal)}s · ${m.bufferedRanges || 0} rng`],
      ["Position", `${num(m.currentTime)} / ${num(m.duration)}s`],
      ["Rate", `${m.playbackRate || 1}x`],
      ["Seekable", `${num(m.seekableEnd, 0)}s`],
      ["Media error", m.mediaError, m.mediaError && m.mediaError !== "none" ? "#ff5a5a" : undefined],
    ]],
    ["Rebuffering", [
      ["Rebuffers", String(m.rebuffers ?? 0), m.rebuffers > 0 ? "#ffb454" : "#37c871"],
      ["Stalled time", `${num((m.stalledMs || 0) / 1000)}s`, m.stalledMs > 0 ? "#ffb454" : undefined],
      ["Underrun", `${num((m.underrun || 0) * 100)}%`],
      ["Startup", m.startupMs != null ? `${m.startupMs}ms` : "—"],
    ]],
    ["Network", [
      ["Throughput", m.kbps != null ? `${m.kbps} kbps` : "—"],
      ["Downloaded", m.bytes ? `${num(m.bytes / 1048576, 2)} MB` : "—"],
      ["TTFB", m.ttfb != null ? `${m.ttfb}ms` : "—"],
      ["Requests", String(m.reqCount ?? 0)],
      ["Conn type", m.connType],
      ["Downlink", m.downlink != null ? `${m.downlink} Mbps` : "—"],
      ["RTT", m.rtt != null ? `${m.rtt}ms` : "—"],
      ["Save-Data", m.saveData ? "ON" : "off"],
      ["Online", m.online ? "yes" : "no", m.online ? undefined : "#ff5a5a"],
    ]],
    ["Web Audio", [
      ["Context", m.ctxState, m.ctxState === "running" ? "#37c871" : "#ffb454"],
      ["Sample rate", m.sampleRate ? `${m.sampleRate} Hz` : "—"],
      ["Base latency", m.baseLatency != null ? `${num(m.baseLatency * 1000)}ms` : "—"],
      ["Output latency", m.outputLatency != null ? `${num(m.outputLatency * 1000)}ms` : "—"],
      ["Gain", m.gain != null ? `${num(m.gain, 3)}x` : "—"],
      ["ReplayGain", m.replayGainDb != null ? `${m.replayGainDb} dB` : "—"],
      ["Limiter", m.limiterDb != null ? `${num(m.limiterDb)} dB` : "—"],
      ["Active buffer", String(m.activeKey || "").toUpperCase() || "—"],
      ["Crossfade", `${m.crossfading ? "active" : "idle"} (${num(m.crossfadeDuration, 1)}s)`],
      ["Next preloaded", m.nextPreloaded ? "yes" : "no"],
    ]],
    ["Track", [
      ["Format", m.format],
      ["Bitrate", m.bitrate ? `${m.bitrate} kbps` : "—"],
      ["Lossless", m.lossless ? "yes" : "no"],
      ["Decoded", m.decodedBytes != null ? `${num(m.decodedBytes / 1048576)} MB` : "n/a"],
      ["Track id", String(m.trackId || "—")],
    ]],
    ["Runtime", [
      ["JS heap", m.heapMB != null ? `${m.heapMB} MB` : "n/a"],
      ["FPS", String(m.fps ?? 0)],
      ["DPR", String(m.dpr ?? 1)],
    ]],
  ];

  return (
    <div style={{ position: "fixed", top: 64, right: 12, width: 286, maxHeight: "76vh", overflowY: "auto", zIndex: 9998, background: "rgba(12,14,18,0.92)", color: "#e8eaed", border: "1px solid rgba(255,255,255,0.14)", borderRadius: 10, font: "11px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace", boxShadow: "0 8px 28px rgba(0,0,0,0.45)", backdropFilter: "blur(6px)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 10px", borderBottom: collapsed ? "none" : "1px solid rgba(255,255,255,0.12)", position: "sticky", top: 0, background: "rgba(12,14,18,0.96)", borderRadius: "10px 10px 0 0" }}>
        <strong style={{ fontSize: 11, letterSpacing: 0.3, flex: 1 }}>Player diagnostics</strong>
        <button title="Reset counters" onClick={() => { stallsRef.current = { count: 0, totalMs: 0, lastStallStart: 0, inStall: false, startupMs: null, loadStart: performance.now(), lastUrl: stallsRef.current.lastUrl }; }} style={{ cursor: "pointer", background: "transparent", color: "#9aa0a6", border: "1px solid rgba(255,255,255,0.18)", borderRadius: 5, padding: "1px 6px", font: "inherit" }}>reset</button>
        <button title={collapsed ? "Expand" : "Collapse"} onClick={() => setCollapsed((c) => !c)} style={{ cursor: "pointer", background: "transparent", color: "#9aa0a6", border: "1px solid rgba(255,255,255,0.18)", borderRadius: 5, padding: "1px 6px", font: "inherit" }}>{collapsed ? "+" : "–"}</button>
      </div>
      {!collapsed && (
        <div style={{ padding: "4px 10px 10px" }}>
          {sections.map(([heading, rows]) => (
            <div key={heading} style={{ marginTop: 8 }}>
              <div style={{ color: "#8ab4f8", textTransform: "uppercase", fontSize: 9.5, letterSpacing: 0.6, marginBottom: 3 }}>{heading}</div>
              {rows.map(([label, value, color]) => (
                <div key={label} style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "1px 0" }}>
                  <span style={{ color: "#9aa0a6" }}>{label}</span>
                  <span style={{ color: color || "#e8eaed", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{value}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AudioPlayer({
  controlRef,
  currentTrack,
  audioUrl,
  nextAudioUrl,
  lyricsUrl,
  queue,
  currentIndex,
  queueOpen,
  setQueueOpen,
  onPlayTrack,
  onRemoveFromQueue,
  onEnded,
  onSkipBack,
  onSkipForward,
  shuffle = false,
  repeat = "off",
  onToggleShuffle,
  onCycleRepeat,
  onFavorite,
  favoriteTrackIds,
  onPlaybackState,
  onDockChange,
  onClose,
  crossfadeDuration = 0.5,
  apiKey,
  diagnostics = false,
}) {
  // Double-buffer: two audio elements. One is "active" (audible); the other
  // preloads the upcoming track so the next song is already buffered and we can
  // swap to it with no reload — gapless on track-end, and the handoff target for
  // crossfade. src is managed imperatively (see effects below), never via React.
  const audioARef = useRef(null);
  const audioBRef = useRef(null);
  const [activeKey, setActiveKey] = useState("a");
  const activeKeyRef = useRef("a");
  activeKeyRef.current = activeKey;
  const loadedUrlRef = useRef({ a: null, b: null });
  const activeAudio = () => (activeKeyRef.current === "a" ? audioARef.current : audioBRef.current);
  const inactiveAudio = () => (activeKeyRef.current === "a" ? audioBRef.current : audioARef.current);

  // Build the Web Audio graph once (both <audio> elements → per-element GainNode →
  // master limiter → output). Must run under user activation so the context can resume —
  // a one-time gesture listener (below) drives this; resume() on later gestures too.
  function ensureAudioGraph() {
    if (audioGraphReadyRef.current) {
      const ctx = audioCtxRef.current;
      if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
      return;
    }
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const elA = audioARef.current;
      const elB = audioBRef.current;
      if (!Ctx || !elA || !elB) return;
      const ctx = new Ctx();
      const limiter = ctx.createDynamicsCompressor();
      // Brickwall-ish limiter so boosting quiet tracks never hard-clips the output.
      limiter.threshold.value = -1.0;
      limiter.knee.value = 0;
      limiter.ratio.value = 20;
      limiter.attack.value = 0.003;
      limiter.release.value = 0.1;
      limiter.connect(ctx.destination);
      limiterRef.current = limiter;
      for (const [key, el] of [["a", elA], ["b", elB]]) {
        const source = ctx.createMediaElementSource(el);
        const gain = ctx.createGain();
        gain.gain.value = 1;
        source.connect(gain);
        gain.connect(limiter);
        gainNodesRef.current[key] = gain;
      }
      audioCtxRef.current = ctx;
      audioGraphReadyRef.current = true;
      ctx.resume().catch(() => {});
    } catch {
      audioGraphReadyRef.current = false;
    }
  }

  // Apply a track's ReplayGain to its element's GainNode (boost or attenuate; 1 = no change).
  function applyReplayGain(key, track) {
    const node = gainNodesRef.current[key];
    if (!node) return;
    const value = replayGainLinear(track);
    const ctx = audioCtxRef.current;
    try {
      if (ctx) node.gain.setTargetAtTime(value, ctx.currentTime, 0.01);
      else node.gain.value = value;
    } catch {
      try { node.gain.value = value; } catch { /* ignore */ }
    }
  }
  // ReplayGain is applied via the Web Audio graph (per-element GainNode → master limiter)
  // so quiet tracks can be BOOSTED above 1.0 to a consistent loudness without clipping.
  // element.volume is left to the crossfade; the GainNode carries the per-track gain.
  const audioCtxRef = useRef(null);
  const gainNodesRef = useRef({ a: null, b: null });
  const audioGraphReadyRef = useRef(false);
  const limiterRef = useRef(null);
  const dockRef = useRef(null);
  const coreRef = useRef(null);
  const trackCopyRef = useRef(null);
  const pipTrackCopyRef = useRef(null);
  const pipWindowRef = useRef(null);
  const playerContainerRef = useRef(null);
  const reopenPipAfterFullscreen = useRef(false);
  const lastPlaybackReportSecond = useRef(-1);
  const crossfading = useRef(false);
  const crossfadeIntervalRef = useRef(null);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  const [lyricsData, setLyricsData] = useState(null);
  const lyricsPanelRef = useRef(null);
  const fsCoreRef = useRef(null);
  const fsArtRef = useRef(null);
  const fsControlsRef = useRef(null);
  const fsScrollRef = useRef(null);
  const fsPlayerRef = useRef(null);
  const upNextRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [pipContainer, setPipContainer] = useState(null);
  const [fullscreenPlayer, setFullscreenPlayer] = useState(false);
  const upcomingQueue = queue.slice(Math.max(currentIndex + 1, 0));
  const nextTrack = upcomingQueue[0];
  const progress = duration ? (currentTime / duration) * 100 : 0;
  const isFavorite = currentTrack?.id ? favoriteTrackIds.has(currentTrack.id) : false;
  const nearEndThreshold = duration ? Math.min(30, Math.max(8, duration * 0.15)) : 0;
  const showUpNext = Boolean(nextTrack && duration && duration - currentTime <= nearEndThreshold);
  const cover = playerCoverUrl(currentTrack, apiKey);
  const canSkipForward = currentIndex >= 0 && (currentIndex < queue.length - 1 || repeat === "all");
  const renderShuffle = (size) => (onToggleShuffle ? (
    <button className={`player-icon-button${shuffle ? " active" : ""}`} onClick={onToggleShuffle} title={shuffle ? "Shuffle on" : "Shuffle off"}>
      <Shuffle size={size} />
    </button>
  ) : null);
  const renderRepeat = (size) => (onCycleRepeat ? (
    <button className={`player-icon-button${repeat !== "off" ? " active" : ""}`} onClick={onCycleRepeat} title={repeat === "one" ? "Repeat one" : repeat === "all" ? "Repeat all" : "Repeat off"}>
      {repeat === "one" ? <Repeat1 size={size} /> : <Repeat size={size} />}
    </button>
  ) : null);

  // Load + play the current track on the active element. If the upcoming track was
  // already preloaded on the OTHER element, swap to it instead of reloading (gapless).
  useEffect(() => {
    if (crossfadeIntervalRef.current) {
      clearInterval(crossfadeIntervalRef.current);
      crossfadeIntervalRef.current = null;
    }
    crossfading.current = false;
    setCurrentTime(0);
    if (!audioUrl) { setPlaying(false); return; }
    const loaded = loadedUrlRef.current;
    const key = activeKeyRef.current;
    const otherKey = key === "a" ? "b" : "a";
    if (loaded[key] !== audioUrl && loaded[otherKey] === audioUrl) {
      // The preloaded element already has this track — promote it (no reload).
      setActiveKey(otherKey);
      return;
    }
    const el = activeAudio();
    if (!el) return;
    const other = inactiveAudio();
    if (other && other !== el) { other.pause(); other.volume = 0; }
    if (loaded[key] !== audioUrl) {
      el.src = audioUrl;
      loaded[key] = audioUrl;
      try { el.currentTime = 0; } catch { /* not seekable yet */ }
    }
    el.volume = 1;
    if (el.duration) setDuration(el.duration);
    ensureAudioGraph();
    applyReplayGain(key, currentTrack);
    el.play?.().catch(() => {});
  }, [audioUrl, activeKey]);

  // After a gapless promotion (activeKey swap on track advance) the newly-active element
  // is often already playing — e.g. crossfade started it early — so it emits no `play`
  // event and the `playing` state (which drives the play/pause icon) would stay stuck on
  // "paused" while audio keeps going. Re-sync `playing` to the active element's real state.
  useEffect(() => {
    const audio = activeAudio();
    if (audio) setPlaying(!audio.paused);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey]);

  // Keep the inactive element preloading the upcoming track.
  useEffect(() => {
    const inactive = inactiveAudio();
    if (!inactive) return;
    const otherKey = activeKeyRef.current === "a" ? "b" : "a";
    const loaded = loadedUrlRef.current;
    // During an advance there's a render where audioUrl is already the new track but
    // activeKey hasn't flipped yet — the inactive element is the one about to be
    // promoted (and may be mid-crossfade). Don't clobber its src, or it reloads from 0.
    if (loaded[otherKey] === audioUrl) return;
    if (nextAudioUrl) {
      if (loaded[otherKey] !== nextAudioUrl) {
        inactive.src = nextAudioUrl;
        loaded[otherKey] = nextAudioUrl;
        inactive.volume = 0;
        applyReplayGain(otherKey, nextTrack);
        try { inactive.load(); } catch { /* ignore */ }
      }
    } else if (loaded[otherKey] !== null) {
      inactive.removeAttribute("src");
      try { inactive.load(); } catch { /* ignore */ }
      loaded[otherKey] = null;
    }
  }, [nextAudioUrl, activeKey]);

  // Browser/OS media widget (Media Session API): metadata + hardware/lock-screen controls.
  useEffect(() => {
    if (!("mediaSession" in navigator)) return undefined;
    const ms = navigator.mediaSession;
    ms.setActionHandler("play", () => togglePlayback());
    ms.setActionHandler("pause", () => togglePlayback());
    ms.setActionHandler("previoustrack", () => handleSkipBack());
    ms.setActionHandler("nexttrack", () => onSkipForward?.());
    try { ms.setActionHandler("stop", () => onClose?.()); } catch { /* unsupported */ }
    return () => {
      ms.setActionHandler("play", null);
      ms.setActionHandler("pause", null);
      ms.setActionHandler("previoustrack", null);
      ms.setActionHandler("nexttrack", null);
      try { ms.setActionHandler("stop", null); } catch { /* unsupported */ }
    };
  }, [onSkipBack, onSkipForward, onClose]);

  useEffect(() => {
    if (!("mediaSession" in navigator) || typeof MediaMetadata === "undefined") return;
    if (!currentTrack) { navigator.mediaSession.metadata = null; return; }
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: currentTrack.title || "",
        artist: currentTrack._artist || "",
        album: currentTrack._album || "",
        artwork: cover ? [{ src: cover, sizes: "512x512", type: "image/jpeg" }] : [],
      });
    } catch { /* ignore */ }
  }, [currentTrack, cover]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.playbackState = playing ? "playing" : "paused";
  }, [playing]);

  useEffect(() => {
    const container = trackCopyRef.current;
    if (!container) return;
    const update = () => {
      const strong = container.querySelector("strong");
      const small = container.querySelector("small");
      if (strong) strong.style.setProperty("--overflow-width", `${Math.max(0, strong.scrollWidth - container.clientWidth)}px`);
      if (small) small.style.setProperty("--overflow-width", `${Math.max(0, small.scrollWidth - container.clientWidth)}px`);
    };
    const ro = new ResizeObserver(update);
    ro.observe(container);
    update();
    return () => ro.disconnect();
  }, [currentTrack?.id]);

  useEffect(() => {
    const container = pipTrackCopyRef.current;
    if (!container) return;
    const update = () => {
      const strong = container.querySelector("strong");
      const small = container.querySelector("small");
      if (strong) strong.style.setProperty("--overflow-width", `${Math.max(0, strong.scrollWidth - container.clientWidth)}px`);
      if (small) small.style.setProperty("--overflow-width", `${Math.max(0, small.scrollWidth - container.clientWidth)}px`);
    };
    const ro = new ResizeObserver(update);
    ro.observe(container);
    update();
    return () => ro.disconnect();
  }, [currentTrack?.id, pipContainer, lyricsOpen]);

  useEffect(() => {
    const container = upNextRef.current?.querySelector('.up-next-text');
    if (!container || !nextTrack) return;
    const update = () => {
      const strong = container.querySelector("strong");
      const small = container.querySelector("small");
      if (strong) strong.style.setProperty("--overflow-width", `${Math.max(0, strong.scrollWidth - container.clientWidth)}px`);
      if (small) small.style.setProperty("--overflow-width", `${Math.max(0, small.scrollWidth - container.clientWidth)}px`);
    };
    const ro = new ResizeObserver(update);
    ro.observe(container);
    update();
    return () => ro.disconnect();
  }, [nextTrack?.id, lyricsOpen]);

  useEffect(() => {
    setLyricsData(null);
    if (!lyricsUrl) return;
    let cancelled = false;
    fetch(lyricsUrl)
      .then((r) => r.json())
      .then((data) => {
        if (cancelled) return;
        const parsed = data.format === "lrc" ? parseLrc(data.lyrics) : [];
        setLyricsData({ raw: data.lyrics, format: data.format, parsed });
      })
      .catch(() => { if (!cancelled) setLyricsData({ raw: null, format: null, parsed: [] }); });
    return () => { cancelled = true; };
  }, [lyricsUrl]);

  const currentLyricIndex = useMemo(() => {
    const parsed = lyricsData?.parsed;
    if (!parsed?.length) return -1;
    let idx = -1;
    for (let i = 0; i < parsed.length; i++) {
      if (parsed[i].time <= currentTime) idx = i;
      else break;
    }
    return idx;
  }, [lyricsData?.parsed, currentTime]);

  useEffect(() => {
    if (!lyricsOpen || !lyricsPanelRef.current || currentLyricIndex < 0) return;
    const el = lyricsPanelRef.current.children[currentLyricIndex];
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [currentLyricIndex, lyricsOpen]);

  // Slide the media controls up with the queue (between the bottom of the page
  // and just under the album art), and grow the album art to fill the space
  // above the controls — both driven by the queue's scroll position.
  useEffect(() => {
    const core = fsCoreRef.current;
    const art = fsArtRef.current;
    const controls = fsControlsRef.current;
    const scroll = fsScrollRef.current;
    const pip = fsPlayerRef.current;
    if (!core || !art || !controls) return undefined;
    const ART_BASE = 100;  // smaller base keeps art squished so the queue is reachable sooner
    const CLAMP_GAP = 16; // gap between the art bottom and the controls when clamped
    const BOTTOM_PAD = 10;
    const update = () => {
      const coreRect = core.getBoundingClientRect();
      const controlsH = controls.offsetHeight || 90;
      const sTop = scroll ? scroll.offsetTop : 0; // scroll area top, relative to core
      // In has-lyrics layout sTop is large (3fr row); keep controls within one
      // controls-height of the scroll area so they don't visually detach from the queue
      const minY = scroll ? Math.max(ART_BASE + CLAMP_GAP, sTop - controlsH) : ART_BASE + CLAMP_GAP;
      const maxY = Math.max(minY, coreRect.height - controlsH - BOTTOM_PAD); // at the bottom
      const scrollTop = scroll ? scroll.scrollTop : 0;
      const controlsY = Math.max(minY, Math.min(maxY, maxY - scrollTop));
      core.style.setProperty("--controls-y", `${controlsY}px`);
      // queue starts just below the controls' resting (bottom) position so it
      // travels up together with the controls
      const padTop = Math.max(0, Math.round(maxY + controlsH + 8 - sTop));
      core.style.setProperty("--queue-pad-top", `${padTop}px`);
      // mask the queue above the bottom edge of the controls box
      const maskCut = Math.max(0, Math.round(controlsY + controlsH - sTop + 6));
      core.style.setProperty("--mask-cut", `${maskCut}px`);
      // art fills from its top down to just above the controls; never so wide
      // that the track info / actions get squeezed out
      const maxByWidth = coreRect.width - 64 - 24 - 240;
      const maxArt = Math.max(ART_BASE, Math.min(coreRect.height * 0.5, 420, maxByWidth));
      const artSize = Math.max(ART_BASE, Math.min(maxArt, Math.round(controlsY - CLAMP_GAP)));
      core.style.setProperty("--art-size", `${artSize}px`);
      // Compact/micro modes: hysteresis prevents rapid toggling.
      // Thresholds are in CSS pixels — on 2x Retina a 300px CSS window is 600 physical px.
      if (pip) {
        const pipH = pip.offsetHeight;
        const pipW = pip.offsetWidth;
        const wasCompact = pip.classList.contains("is-compact");
        const wasMicro = pip.classList.contains("is-micro");
        // Enter compact at 250px (scrolled) / 200px (any); exit at 290px / 235px
        const compact = wasCompact
          ? pipH < 290 && (scrollTop > 0 || pipH < 235)
          : pipH < 250 && (scrollTop > 0 || pipH < 200);
        // Micro: 20% larger than original 270×340 → 324×408; exit at 370×470
        const micro = !compact && (wasMicro
          ? pipH < 370 && pipW < 470
          : pipH < 324 && pipW < 408);
        pip.classList.toggle("is-compact", compact);
        pip.classList.toggle("is-micro", micro);
      }
    };
    update();
    // Use the element's own window for observer/resize so PiP cross-window works
    const observerWin = core.ownerDocument?.defaultView ?? window;
    const onScrollOrResize = () => observerWin.requestAnimationFrame(update);
    scroll?.addEventListener("scroll", onScrollOrResize, { passive: true });
    observerWin.addEventListener("resize", onScrollOrResize);
    const RO = observerWin.ResizeObserver ?? (typeof ResizeObserver !== "undefined" ? ResizeObserver : null);
    const ro = RO ? new RO(onScrollOrResize) : null;
    ro?.observe(core);
    ro?.observe(controls);
    if (scroll) ro?.observe(scroll);
    if (pip) ro?.observe(pip);
    // Forward wheel events on header and controls areas to the queue scroller
    const headerEl = core.querySelector(".audio-header");
    const controlsEl = core.querySelector(".pip-controls-sticky");
    const handleWheel = (e) => {
      if (!scroll) return;
      if (e.target.closest && e.target.closest(".pip-header-lyrics")) return;
      e.preventDefault();
      scroll.scrollTop += e.deltaY;
    };
    headerEl?.addEventListener("wheel", handleWheel, { passive: false });
    controlsEl?.addEventListener("wheel", handleWheel, { passive: false });
    return () => {
      scroll?.removeEventListener("scroll", onScrollOrResize);
      observerWin.removeEventListener("resize", onScrollOrResize);
      ro?.disconnect();
      headerEl?.removeEventListener("wheel", handleWheel);
      controlsEl?.removeEventListener("wheel", handleWheel);
    };
  }, [currentTrack, queue, lyricsOpen, fullscreenPlayer, pipContainer, showUpNext]);

  useEffect(() => () => {
    pipWindowRef.current?.close?.();
  }, []);

  useEffect(() => {
    if (!pipContainer && !document.fullscreenElement) {
      setFullscreenPlayer(false);
    }
  }, [pipContainer]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      const active = Boolean(document.fullscreenElement);
      setFullscreenPlayer(active);
      if (!active && reopenPipAfterFullscreen.current) {
        reopenPipAfterFullscreen.current = false;
        window.setTimeout(() => {
          openPictureInPicture().catch(() => {});
        }, 50);
      }
    };
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, [queue.length]);

  useEffect(() => {
    if (!queueOpen) return;
    const handleClickOutside = (e) => {
      if (playerContainerRef.current && !playerContainerRef.current.contains(e.target)) {
        setQueueOpen(false);
      }
    };
    document.addEventListener("pointerdown", handleClickOutside);
    return () => document.removeEventListener("pointerdown", handleClickOutside);
  }, [queueOpen, setQueueOpen]);

  // Build/resume the Web Audio graph on the first user gesture (autoplay policy needs a
  // gesture to resume the context; building it here avoids ever routing audio into a
  // suspended graph, which would silence playback).
  useEffect(() => {
    const handler = () => ensureAudioGraph();
    document.addEventListener("pointerdown", handler);
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("pointerdown", handler);
      document.removeEventListener("keydown", handler);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onEndedRef = useRef(onEnded);
  onEndedRef.current = onEnded;

  useEffect(() => {
    const fadeSec = crossfadeDuration > 0 ? crossfadeDuration : 0;
    if (!fadeSec || !duration || !nextAudioUrl || crossfading.current) return;
    if (repeat === "one") return; // repeating the same track: nothing to fade into
    const remaining = duration - currentTime;
    if (remaining > fadeSec || remaining <= 0) return;
    const otherKey = activeKeyRef.current === "a" ? "b" : "a";
    if (loadedUrlRef.current[otherKey] !== nextAudioUrl) return; // next not preloaded yet
    const active = activeAudio();
    const next = inactiveAudio();
    if (!next) return;
    crossfading.current = true;
    try { next.currentTime = 0; } catch { /* ignore */ }
    next.volume = 0;
    applyReplayGain(otherKey, nextTrack);
    next.play().catch(() => {});
    const startTime = performance.now();
    crossfadeIntervalRef.current = setInterval(() => {
      const elapsed = (performance.now() - startTime) / 1000;
      const frac = Math.min(elapsed / fadeSec, 1);
      if (active) active.volume = Math.max(0, 1 - frac);
      if (next) next.volume = Math.min(1, frac);
      if (frac >= 1) {
        clearInterval(crossfadeIntervalRef.current);
        crossfadeIntervalRef.current = null;
        // Advance: App updates audioUrl → the loader effect promotes `next` (already
        // playing at full volume) to active with no reload.
        onEndedRef.current?.();
      }
    }, 30);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentTime]);

  useEffect(() => {
    const measureCompactHeight = () => (coreRef.current ? coreRef.current.offsetHeight + 36 : dockRef.current?.offsetHeight || 0);
    const measureFullHeight = () => dockRef.current?.offsetHeight || measureCompactHeight();
    const reportDock = () =>
      onDockChange?.({
        popped: Boolean(pipContainer),
        compactHeight: pipContainer ? 0 : measureCompactHeight(),
        fullHeight: pipContainer ? 0 : measureFullHeight(),
      });
    reportDock();
    if (pipContainer || !coreRef.current || !dockRef.current) return undefined;
    const observer = new ResizeObserver(reportDock);
    observer.observe(coreRef.current);
    observer.observe(dockRef.current);
    return () => {
      observer.disconnect();
      onDockChange?.({ popped: false, compactHeight: 0, fullHeight: 0 });
    };
  }, [onDockChange, pipContainer, currentTrack?.id]);

  function togglePlayback() {
    const audio = activeAudio();
    if (!audio) return;
    if (audio.paused) {
      audio.play().catch(() => {});
    } else {
      audio.pause();
    }
  }

  // Skip-back restarts the current track; pressing again while still near the
  // start (< 1s in) jumps to the previous track. Playback position doubles as
  // the "pressed again quickly" timer.
  function handleSkipBack() {
    const audio = activeAudio();
    if (audio && audio.currentTime > 1) {
      try { audio.currentTime = 0; } catch { /* not seekable yet */ }
      setCurrentTime(0);
      return;
    }
    onSkipBack?.();
  }

  // Expose imperative transport controls for the remote-command consumer.
  useEffect(() => {
    if (!controlRef) return undefined;
    controlRef.current = {
      pause: () => activeAudio()?.pause(),
      resume: () => activeAudio()?.play().catch(() => {}),
      stop: () => {
        const a = activeAudio();
        if (a) { a.pause(); try { a.currentTime = 0; } catch { /* ignore */ } }
      },
    };
    return () => { if (controlRef) controlRef.current = null; };
  }, [controlRef]);

  function notifyPlaybackState(status, event) {
    onPlaybackState?.(status, {
      position_seconds: Math.round(event?.currentTarget?.currentTime || currentTime || 0),
      duration_seconds: Math.round(event?.currentTarget?.duration || duration || 0) || null,
    });
  }

  function handleTrackEnded(key) {
    if (key !== activeKeyRef.current) return; // an old (now-inactive) element finishing — ignore
    if (crossfading.current) return; // the crossfade is driving the advance
    if (repeat === "one") {
      const a = activeAudio();
      if (a) { a.currentTime = 0; a.play().catch(() => {}); }
      return;
    }
    onEnded?.();
  }

  // Both buffers render the same element; handlers no-op unless this is the active one.
  function renderAudioElement(key, ref) {
    return (
      <audio
        key={key}
        ref={ref}
        preload="auto"
        style={{ display: "none" }}
        onPlay={(event) => { if (key !== activeKeyRef.current) return; setPlaying(true); notifyPlaybackState("playing", event); }}
        onPause={(event) => { if (key !== activeKeyRef.current) return; setPlaying(false); notifyPlaybackState("paused", event); }}
        onTimeUpdate={(event) => {
          if (key !== activeKeyRef.current) return;
          const second = Math.round(event.currentTarget.currentTime);
          setCurrentTime(event.currentTarget.currentTime);
          if (second !== lastPlaybackReportSecond.current && second % 15 === 0) {
            lastPlaybackReportSecond.current = second;
            notifyPlaybackState(playing ? "playing" : "paused", event);
          }
        }}
        onLoadedMetadata={(event) => { if (key !== activeKeyRef.current) return; setDuration(event.currentTarget.duration || 0); }}
        onEnded={() => handleTrackEnded(key)}
      />
    );
  }

  function seek(event) {
    const audio = activeAudio();
    if (!audio || !duration) return;
    const nextTime = Number(event.target.value);
    audio.currentTime = nextTime;
    setCurrentTime(nextTime);
  }

  async function openPictureInPicture() {
    const width = 980;
    const height = 486;
    const pipWindow =
      "documentPictureInPicture" in window
        ? await window.documentPictureInPicture.requestWindow({ width, height })
        : window.open("", "nudibranch-player", `width=${width},height=${height},popup`);
    if (!pipWindow) return;
    pipWindowRef.current = pipWindow;
    pipWindow.document.body.innerHTML = "";
    pipWindow.document.body.style.margin = "0";
    pipWindow.document.body.style.padding = "0";
    pipWindow.document.body.style.width = "100vw";
    pipWindow.document.body.style.height = "100vh";
    pipWindow.document.body.style.minHeight = "100vh";
    pipWindow.document.body.style.minWidth = "250px";
    pipWindow.document.body.style.overflow = "hidden";
    pipWindow.document.body.style.background = "transparent";
    pipWindow.document.documentElement.style.margin = "0";
    pipWindow.document.documentElement.style.padding = "0";
    pipWindow.document.documentElement.style.width = "100vw";
    pipWindow.document.documentElement.style.height = "100vh";
    pipWindow.document.documentElement.style.minHeight = "100vh";
    pipWindow.document.documentElement.style.minWidth = "250px";
    pipWindow.document.documentElement.style.overflow = "hidden";
    pipWindow.document.documentElement.style.background = "transparent";
    copyStylesToWindow(pipWindow);
    const container = pipWindow.document.createElement("div");
    container.className = `${document.querySelector("main")?.className || "app"} pip-root`;
    container.style.width = "100vw";
    container.style.height = "100vh";
    container.style.minHeight = "100vh";
    container.style.minWidth = "250px";
    container.style.overflow = "hidden";
    container.style.display = "block";
    const mainEl = document.querySelector("main");
    if (mainEl) {
      for (const prop of mainEl.style) {
        if (prop.startsWith("--")) container.style.setProperty(prop, mainEl.style.getPropertyValue(prop));
      }
    }
    pipWindow.document.body.appendChild(container);
    const handleFullscreenChange = () => setFullscreenPlayer(Boolean(pipWindow.document.fullscreenElement));
    const closePip = () => {
      if (!document.fullscreenElement) {
        setFullscreenPlayer(false);
      }
      pipWindowRef.current = null;
      pipWindow.document.removeEventListener("fullscreenchange", handleFullscreenChange);
      setPipContainer(null);
    };
    pipWindow.document.addEventListener("fullscreenchange", handleFullscreenChange);
    pipWindow.addEventListener("pagehide", closePip, { once: true });
    pipWindow.addEventListener("beforeunload", closePip, { once: true });
    setPipContainer(container);
  }

  async function toggleFullscreenPlayer() {
    const pipWindow = pipWindowRef.current;
    const targetDocument = pipWindow?.document || document;
    try {
      if (targetDocument.fullscreenElement || document.fullscreenElement) {
        await (targetDocument.fullscreenElement ? targetDocument : document).exitFullscreen?.();
        setFullscreenPlayer(false);
        return;
      }
      if (pipWindow) {
        try {
          await targetDocument.documentElement.requestFullscreen?.();
          if (targetDocument.fullscreenElement) {
            setFullscreenPlayer(true);
            return;
          }
        } catch {
          // Fall back to fullscreening the main window below.
        }
        reopenPipAfterFullscreen.current = true;
        setFullscreenPlayer(true);
        pipWindow.close?.();
        setPipContainer(null);
        await document.documentElement.requestFullscreen?.();
        return;
      }
      await document.documentElement.requestFullscreen?.();
      setFullscreenPlayer(Boolean(document.fullscreenElement));
    } catch {
      setFullscreenPlayer(Boolean(targetDocument.fullscreenElement || document.fullscreenElement));
    }
  }

  function queueList() {
    return upcomingQueue.map((track, index) => (
      <div className="queue-entry" key={`${track.id}:${index}`}>
        <button className={`queue-play-btn${track.id === currentTrack?.id ? " active" : ""}`} onClick={() => onPlayTrack(track)}>
          <strong>{track.title}</strong>
          <small>{track._artist || ""}</small>
        </button>
        {onRemoveFromQueue && (
          <button className="queue-remove-btn" onClick={(e) => { e.stopPropagation(); onRemoveFromQueue(index); }} title="Remove from queue">
            <X size={12} />
          </button>
        )}
      </div>
    ));
  }

  function surface({ popped = false } = {}) {
    const pipLayout = popped || fullscreenPlayer;
    const docked = !pipLayout;

    if (docked) {
      return (
        <div className="audio-player topbar" ref={(el) => { dockRef.current = el; playerContainerRef.current = el; }}>
          <div className="topbar-player-row" ref={coreRef}>
            <div className="player-art">
              {cover ? <img src={cover} alt="" /> : <Music size={18} />}
            </div>
            <div className="topbar-track-copy" ref={trackCopyRef}>
              <strong>{currentTrack?.title || "Local player"}</strong>
              <small>{[currentTrack?._artist, currentTrack?._album].filter(Boolean).join(" / ") || "Ready"}</small>
            </div>
            <div className="topbar-controls">
              {renderShuffle(16)}
              <button className="player-icon-button" onClick={handleSkipBack} disabled={!currentTrack} title="Previous">
                <SkipBack size={16} />
              </button>
              <button className="player-play-button compact" onClick={togglePlayback} title={playing ? "Pause" : "Play"}>
                {playing ? <Pause size={17} /> : <Play size={17} />}
              </button>
              <button className="player-icon-button" onClick={onSkipForward} disabled={!canSkipForward} title="Next">
                <SkipForward size={16} />
              </button>
              {renderRepeat(16)}
              <button className={isFavorite ? "player-icon-button active" : "player-icon-button"} onClick={() => onFavorite(currentTrack)} disabled={!currentTrack} title="Favorite">
                <Heart size={16} />
              </button>
            </div>
            <input className="player-progress topbar-progress" type="range" min="0" max={duration || 0} value={currentTime} onChange={seek} style={{ "--progress": `${progress}%` }} />
            <div className="topbar-actions">
              <button className="player-icon-button" onClick={() => setQueueOpen((v) => !v)} title="Queue">
                <Menu size={16} />
              </button>
              <button className="row-icon-button" onClick={openPictureInPicture} disabled={queue.length === 0} title={queue.length === 0 ? "Queue is empty" : "Pop out"}>
                <PictureInPicture2 size={14} />
              </button>
              <button className="row-icon-button" onClick={onClose} title="Close player">
                <X size={14} />
              </button>
            </div>
          </div>
          {queueOpen && (
            <div className="local-queue topbar-queue">
              {queueList()}
            </div>
          )}
        </div>
      );
    }

    const closeAction = popped ? () => pipWindowRef.current?.close?.() : toggleFullscreenPlayer;
    const closeTitle = popped ? "Return to page" : "Exit fullscreen";

    const lyricsContent = (() => {
      if (!lyricsData) return <div className="lyrics-loading">Loading…</div>;
      const parsed = lyricsData.parsed;
      if (parsed?.length) {
        return parsed.map((line, i) => {
          const dist = i - currentLyricIndex;
          const isCurrent = dist === 0;
          const opacity = isCurrent ? 1 : Math.max(0.12, 1 - Math.abs(dist) * 0.14);
          return (
            <div
              key={i}
              className={`lyric-line${isCurrent ? " current" : ""}`}
              style={{ opacity }}
            >
              {line.text}
            </div>
          );
        });
      }
      if (lyricsData.raw) {
        return lyricsData.raw.split("\n").filter(Boolean).map((line, i) => (
          <div key={i} className="lyric-line plain">{line}</div>
        ));
      }
      return <div className="lyrics-empty">No lyrics available</div>;
    })();

    const upNextWidget = nextTrack ? (() => {
      const upNextCover = playerCoverUrl(nextTrack, apiKey);
      return (
      <div className={`fullscreen-next${showUpNext ? " is-visible" : ""}`} ref={upNextRef}>
        <div className="up-next-art">{upNextCover ? <img src={upNextCover} alt="" /> : <Music size={18} />}</div>
        <div className="up-next-text">
          <span>Up next</span>
          <strong>{nextTrack.title}</strong>
          <small>{[nextTrack._artist, nextTrack._album].filter(Boolean).join(" / ") || "Library queue"}</small>
        </div>
      </div>
      );
    })() : null;

    return (
      <div
        className={`${popped ? "audio-player popped pip-player" : "audio-player pip-player main-fullscreen-player"}${fullscreenPlayer ? " is-window-fullscreen" : ""}${nextTrack ? " has-up-next" : ""}${lyricsOpen ? " has-lyrics" : ""}`}
        ref={(el) => { fsPlayerRef.current = el; if (!popped) dockRef.current = el; }}
        style={cover ? { "--fullscreen-art": `url(${cover})` } : undefined}
      >
        <div className="player-core" ref={(el) => { fsCoreRef.current = el; if (!popped) coreRef.current = el; }}>
          <div className="audio-header">
            <div className="player-art" ref={fsArtRef}>{cover ? <img src={cover} alt="" /> : <Music size={34} />}</div>
            <div className="audio-track-copy" ref={pipTrackCopyRef}>
              <span className="playing-from">Playing from library</span>
              <strong>{currentTrack?.title || "Local player"}</strong>
              <small>{[currentTrack?._artist, currentTrack?._album].filter(Boolean).join(" / ") || currentTrack?.path || "Ready"}</small>
            </div>
            {lyricsOpen ? (
              <div className="lyrics-next-stack">
                {showUpNext ? upNextWidget : null}
                <div className="pip-header-lyrics" ref={lyricsPanelRef}>
                  {lyricsContent}
                </div>
              </div>
            ) : upNextWidget}
            <div className="player-window-actions">
              <button className="row-icon-button" onClick={toggleFullscreenPlayer} title={fullscreenPlayer ? "Exit fullscreen" : "Fullscreen"}>
                {fullscreenPlayer ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
              </button>
              <button className="row-icon-button" onClick={closeAction} title={closeTitle}>
                <X size={14} />
              </button>
            </div>
          </div>
          <div className="pip-scroll-area" ref={fsScrollRef}>
            <div className="local-queue pip-queue">
              {queueList()}
            </div>
          </div>
          <div className="fullscreen-controls pip-controls-sticky" ref={fsControlsRef}>
            <input className="player-progress" type="range" min="0" max={duration || 0} value={currentTime} onChange={seek} style={{ "--progress": `${progress}%` }} />
            <div className="player-controls">
              <button className={`player-icon-button${lyricsOpen ? " active" : ""}`} onClick={() => setLyricsOpen((v) => !v)} title="Lyrics">
                <Mic2 size={19} className="lyric-icon-on" />
                <Ban size={19} className="lyric-icon-off" />
              </button>
              {renderShuffle(18)}
              <button className="player-icon-button" onClick={handleSkipBack} disabled={!currentTrack} title="Previous">
                <SkipBack size={18} />
              </button>
              <button className="player-play-button" onClick={togglePlayback} title={playing ? "Pause" : "Play"}>
                {playing ? <Pause size={21} /> : <Play size={21} />}
              </button>
              <button className="player-icon-button" onClick={onSkipForward} disabled={!canSkipForward} title="Next">
                <SkipForward size={18} />
              </button>
              {renderRepeat(18)}
              <button className={isFavorite ? "player-icon-button active" : "player-icon-button"} onClick={() => onFavorite(currentTrack)} title="Favorite">
                <Heart size={19} />
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      {!pipContainer ? surface() : null}
      {renderAudioElement("a", audioARef)}
      {renderAudioElement("b", audioBRef)}
      {diagnostics && (
        <PlayerDiagnostics
          audioARef={audioARef}
          audioBRef={audioBRef}
          activeKeyRef={activeKeyRef}
          audioCtxRef={audioCtxRef}
          gainNodesRef={gainNodesRef}
          limiterRef={limiterRef}
          loadedUrlRef={loadedUrlRef}
          crossfadingRef={crossfading}
          currentTrack={currentTrack}
          audioUrl={audioUrl}
          nextAudioUrl={nextAudioUrl}
          crossfadeDuration={crossfadeDuration}
        />
      )}
      {pipContainer ? createPortal(surface({ popped: true }), pipContainer) : null}
    </>
  );
}

function TreeToolbar({ expanded, onExpand, onCollapse, children }) {
  const nextExpanded = !expanded;
  return (
    <div className="tree-toolbar">
      <div className="tree-toolbar-actions">{children}</div>
      <button className="secondary compact" onClick={nextExpanded ? onExpand : onCollapse}>
        {nextExpanded ? "Expand all" : "Collapse all"}
      </button>
    </div>
  );
}

function TreeRow({ depth = 0, icon: Icon, open, title, meta, warning = false, onToggle, onActivate }) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <button className="tree-row" style={{ "--depth": depth }} onClick={onActivate || onToggle}>
      <span className="chevron">{onToggle ? <Chevron size={15} /> : null}</span>
      <Icon size={17} />
      <span className="tree-title">{title}</span>
      <small className={warning ? "warning" : ""}>{meta}</small>
    </button>
  );
}

function SelectableTreeRow({ depth = 0, icon: Icon, open, title, meta, warning = false, onToggle, control }) {
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="tree-row selectable-tree-row" style={{ "--depth": depth }}>
      <button className="selectable-tree-main" onClick={onToggle}>
        <span className="chevron">{onToggle ? <Chevron size={15} /> : null}</span>
        <Icon size={17} />
      </button>
      {control}
      <button className="selectable-tree-title" onClick={onToggle}>
        <span className="tree-title">{title}</span>
        <small className={warning ? "warning" : ""}>{meta}</small>
      </button>
    </div>
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
  // All items across every batch, by id — used to recover structural ancestors whose own
  // status no longer passes the pending/approved/failed filter below.
  const allById = new Map();
  batches.forEach((batch) => batch.items.forEach((item) => allById.set(item.id, item)));
  batches.forEach((batch) => {
    const batchGroupKind = batch.kind === "import_files" ? "import_files" : null;
    batch.items.forEach((item) => {
      // "executing" stays in the Task Queue so in-progress downloads/lyrics remain
      // visible (with live progress) until they complete — there's no separate Downloads tab.
      if (!["pending", "approved", "failed", "executing"].includes(item.status)) return;
      const groupKind = batchGroupKind || item.kind;
      // Download items must not be deduped by title — two artists can share a song name
      // and dropping one item breaks its parent's child count (missing chevron).
      const key = item.kind === "download"
        ? `${item.batch_id}:${item.id}`
        : `${groupKind}:${item.kind}:${item.title}:${item.old_value || ""}:${item.new_value || ""}`;
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

  // Pull structural ancestor nodes (artist/album/track) back into the download group even
  // when their own status has advanced to executing/completed. Otherwise, once a batch is
  // approved and downloading, the still-pending alternate candidates lose their parents and
  // dump flat at the top of the Task Queue instead of nesting under Artist>Album>Track.
  const downloadGroup = groups.get("download");
  if (downloadGroup) {
    const present = new Set(downloadGroup.items.map((item) => item.id));
    for (const item of [...downloadGroup.items]) {
      let parentId = item.parent_id;
      while (parentId && allById.has(parentId) && !present.has(parentId)) {
        const ancestor = allById.get(parentId);
        present.add(parentId);
        downloadGroup.items.push(ancestor);
        parentId = ancestor.parent_id;
      }
    }
  }

  // Merge root items with the same title within each group so the same artist
  // doesn't appear twice when multiple batches exist for that artist.
  for (const group of groups.values()) {
    const itemIdSet = new Set(group.items.map((i) => i.id));
    const titleToId = new Map();
    const idRemap = new Map();
    for (const item of group.items) {
      if (item.parent_id && itemIdSet.has(item.parent_id)) continue; // not a root
      if (!titleToId.has(item.title)) {
        titleToId.set(item.title, item.id);
      } else {
        idRemap.set(item.id, titleToId.get(item.title));
      }
    }
    if (idRemap.size > 0) {
      group.items = group.items
        .filter((i) => !idRemap.has(i.id))
        .map((i) => (idRemap.has(i.parent_id) ? { ...i, parent_id: idRemap.get(i.parent_id) } : i));
    }

    // Prune empty grouping branches for the DOWNLOAD group only: an album whose
    // candidates have all been consumed would otherwise leave its grouping rows
    // lingering here empty. Keep only download items that are actionable themselves (have a
    // payload action or are failed) or are ancestors of such items. Other kinds (metadata,
    // import_files, lyrics, artwork) carry no "action" and must NOT be pruned.
    if (group.id === "type:download") {
      const byId = new Map(group.items.map((i) => [i.id, i]));
      const actionableIds = new Set(
        group.items
          .filter((i) => Boolean(parseJsonObject(i.payload_json).action) || i.status === "failed")
          .map((i) => i.id)
      );
      const keepIds = new Set();
      for (const id of actionableIds) {
        keepIds.add(id);
        let cur = byId.get(id);
        while (cur && cur.parent_id) {
          keepIds.add(cur.parent_id);
          cur = byId.get(cur.parent_id);
        }
      }
      group.items = group.items.filter((i) => keepIds.has(i.id));
    }
  }

  return [...groups.values()];
}

function collectItemIds(item, childrenById) {
  const children = childrenById.get(item.id) || [];
  return [item.id, ...children.flatMap((child) => collectItemIds(child, childrenById))];
}

function siblingItems(item, childrenById) {
  for (const siblings of childrenById.values()) {
    if (siblings.some((sibling) => sibling.id === item.id)) {
      return siblings;
    }
  }
  return [item];
}

function visibleDownloadItems(batches) {
  return batches.flatMap((batch) => {
    const tree = buildItemTree(batch.items);
    const candidateIds = new Set();
    for (const siblings of tree.childrenById.values()) {
      const candidates = siblings.filter((item) => item.kind === "download" && (item.old_value || item.new_value));
      if (candidates.length === 0) continue;
      const selected = candidates.find((item) => item.selected);
      candidateIds.add((selected || candidates[0]).id);
    }
    return batch.items.filter((item) => {
      const leafCandidate = item.kind === "download" && !(tree.childrenById.get(item.id) || []).length && (item.old_value || item.new_value);
      return !leafCandidate || candidateIds.has(item.id);
    });
  });
}

function isDownloadActionItem(item) {
  if (item.kind !== "download") return false;
  const payload = parseJsonObject(item.payload_json);
  return ["queue_download", "queue_ytdlp_download", "wishlist_request"].includes(payload.action);
}

function lowestLevelItems(items) {
  const parentIds = new Set(items.map((item) => item.parent_id).filter(Boolean));
  return items.filter((item) => !parentIds.has(item.id));
}

// Last two path segments — enough to show a file move's source/destination folder
// without overflowing the row; the full paths are in the row's title (hover).
function shortPath(value) {
  if (!value) return "?";
  const parts = String(value).split("/").filter(Boolean);
  return parts.slice(-2).join("/") || String(value);
}

function candidateMeta(item) {
  const status = itemStatusMeta(item);
  const source = item.new_value ? ` · ${item.new_value}` : "";
  if (["working", "done", "needs attention", "pending"].includes(status)) return `candidate${source}`;
  return `${status}${source}`;
}

function itemStatusMeta(item) {
  const payload = parseJsonObject(item.payload_json);
  if (payload.status) return payload.status;
  if (item.status === "executing") return "working";
  if (item.status === "completed") return "done";
  if (item.status === "failed") return "needs attention";
  if (item.status === "rejected") return "rejected";
  return item.kind;
}

function downloadProgressSummary(approvals) {
  const batches = approvals.filter((batch) => batch.kind === "download" && batch.tree_path === "/downloads");
  const leaves = lowestLevelItems(visibleDownloadItems(batches)).filter((item) => item.selected && isDownloadActionItem(item));
  if (leaves.length === 0) return null;
  let downloading = 0;
  let retried = 0;
  let finished = 0;
  let failed = 0;
  let selected = 0;
  let waiting = 0;
  let queued = 0;
  let staging = 0;
  let verifying = 0;
  let verified = 0;
  let partial = 0;
  for (const item of leaves) {
    const status = itemStatusMeta(item);
    const lower = String(status || "").toLowerCase();
    const payload = parseJsonObject(item.payload_json);
    const structuredProgress = downloadStatusProgressForItem(item);
    const hasRetried = /retry|retried|replacement|stalled/.test(lower) || (payload.failed_candidates || []).length > 0;
    if (hasRetried) retried += 1;
    if (item.status === "failed" || /need attention|failed|mismatch|could not be verified/.test(lower)) {
      failed += 1;
      continue;
    }
    if (item.status === "completed" || /verified|importing/.test(lower)) {
      finished += 1;
      verified += 1;
      partial += 100;
      continue;
    }
    if (/downloaded|staged|verifying/.test(lower)) {
      finished += 1;
      if (/verifying/.test(lower)) verifying += 1;
      partial += 100;
      continue;
    }
    if (/candidate ready|candidate|pending/.test(lower) || item.status === "pending" || item.status === "approved") {
      selected += 1;
      continue;
    }
    const progress = structuredProgress || downloadStatusProgress(status);
    if (progress) {
      if (progress.stage === "downloading" || /downloading\s+\d+(?:\.\d+)?%/.test(lower)) downloading += 1;
      else if (["staging", "transferring", "importing"].includes(progress.stage)) staging += 1;
      else if (progress.stage === "verifying") verifying += 1;
      else if (progress.stage === "queued") queued += 1;
      else waiting += 1;
      partial += progress.indeterminate ? 0 : progress.value;
      continue;
    }
    selected += 1;
  }
  const total = leaves.length;
  if (verified === total && failed === 0) return null;
  const percent = total ? partial / total : 0;
  const notStarted = selected === total && downloading === 0 && finished === 0 && failed === 0;
  const verificationPending = finished === total && verified < total && failed === 0;
  const waitingForDownload = (waiting > 0 || queued > 0) && downloading === 0 && finished === 0 && failed === 0;
  const label = notStarted
    ? `${selected} selected candidates`
    : waitingForDownload
      ? `${queued || waiting}/${total} queued`
    : downloading > 0
      ? `${downloading}/${total} downloading`
    : staging > 0
      ? `${staging}/${total} staging`
    : verificationPending
      ? `Verification pending for ${total} downloads`
      : verifying > 0
        ? `Verifying ${finished}/${total}`
        : `${finished}/${total} finished`;
  return {
    percent,
    indeterminate: !notStarted && downloading > 0 && partial === 0,
    label,
    detail: `${queued} queued · ${downloading} downloading · ${staging} staging · ${retried} retried · ${finished} finished · ${failed} failed`,
  };
}

function downloadStatusProgress(status) {
  if (!status) return null;
  const text = String(status);
  const match = text.match(/downloading\s+(\d+(?:\.\d+)?)%/i);
  if (match) {
    return { value: Number(match[1]), label: text, indeterminate: false };
  }
  if (/verifying with musicbrainz/i.test(text)) {
    return { value: 0, label: text, indeterminate: true };
  }
  if (/downloaded|staged|verified|importing/i.test(text)) {
    return { value: 100, label: text, indeterminate: false };
  }
  const ratio = text.match(/(?:downloading|verifying)\s+(\d+(?:\.\d+)?)%/i);
  if (ratio) {
    return { value: Number(ratio[1]), label: text, indeterminate: false };
  }
  if (/download initialized|download queued|moving completed file|checking slskd|searching for slskd|slskd .*queued|slskd .*remote|reports complete/i.test(text)) {
    return { value: 0, label: text, indeterminate: false };
  }
  return null;
}

function downloadStatusProgressForItem(item) {
  const payload = parseJsonObject(item.payload_json);
  const progress = payload.download_progress;
  if (!progress || typeof progress !== "object") return downloadStatusProgress(payload.status);
  const value = Number(progress.value ?? progress.progress ?? 0);
  return {
    value: Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0,
    label: progress.label || payload.status || itemStatusMeta(item),
    indeterminate: Boolean(progress.indeterminate),
    stage: progress.stage || "queued",
  };
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

function moveTrackPaths(files, onFilesChange, paths, metadataPatch) {
  const pathSet = new Set(paths);
  onFilesChange(
    files.map((file) => {
      if (!pathSet.has(file.path)) return file;
      const metadata = { ...(file.metadata || {}), ...metadataPatch };
      return {
        ...file,
        metadata,
        suggested_library_path: suggestImportPath(file, metadata),
      };
    }),
  );
}

function mergeAlbumIntoAlbum(files, onFilesChange, sourceAlbum, targetAlbum) {
  const sourceFiles = files.filter((file) => {
    const metadata = file.metadata || {};
    const artist = metadata.albumartist || metadata.artist || "Unknown Artist";
    const album = metadata.album || "Unknown Album";
    return artist === sourceAlbum.artist && album === sourceAlbum.album;
  });
  const slotByTrack = new Map(targetAlbum.slots.map((slot) => [slot.track_number, slot]));
  onFilesChange(
    files.map((file) => {
      if (!sourceFiles.some((sourceFile) => sourceFile.path === file.path)) return file;
      const slot = slotByTrack.get(file.metadata?.track_number);
      const metadata = {
        ...(file.metadata || {}),
        artist: targetAlbum.artist,
        albumartist: targetAlbum.artist,
        album: targetAlbum.album,
        title: slot ? titleForDroppedSlot(slot, file) : file.metadata?.title,
      };
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

function removeImportArtist(files, onFilesChange, artistName) {
  onFilesChange(
    files.filter((file) => {
      const metadata = file.metadata || {};
      return (metadata.albumartist || metadata.artist || "Unknown Artist") !== artistName;
    }),
  );
}

function removeImportAlbum(files, onFilesChange, artistName, albumName) {
  onFilesChange(
    files.filter((file) => {
      const metadata = file.metadata || {};
      const currentArtist = metadata.albumartist || metadata.artist || "Unknown Artist";
      const currentAlbum = metadata.album || "Unknown Album";
      return currentArtist !== artistName || currentAlbum !== albumName;
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
      artist.albumMap.set(album.name, {
        name: album.name,
        files: [],
        expectedTracks: album.tracks,
        cover_art_url: album.cover_art_url,
        manual: true,
        playlistName: album.playlistName || null,
      });
    } else if (album.cover_art_url) {
      artist.albumMap.get(album.name).cover_art_url = album.cover_art_url;
    }
  });

  return [...artistMap.values()]
    .map((artist) => {
      const albums = [...artist.albumMap.values()].map((album) => buildImportAlbum(album, artist.name, library, albumRecords));
      const plNames = new Set(albums.map((a) => a.playlistName).filter(Boolean));
      return {
        name: artist.name,
        count: artist.count,
        albums,
        playlistName: plNames.size === 1 ? [...plNames][0] : null,
      };
    })
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
    const discNumber = file.metadata?.disc_number || 1;
    const key = `${discNumber}:${trackNumber}`;
    if (trackNumber && !trackMap.has(key)) trackMap.set(key, file);
  });
  const usedPaths = new Set();
  const libraryTrackTitles = new Set((libraryAlbum?.tracks || []).map((t) => normalizeName(t.title)));
  const slots = expectedTracks.map((track, index) => {
    const trackNumber = track.track_number || index + 1;
    const discNumber = track.disc_number || 1;
    const file = trackMap.get(`${discNumber}:${trackNumber}`);
    if (file) usedPaths.add(file.path);
    const inLibrary =
      (libraryAlbum != null && libraryTrackTitles.has(normalizeName(track.title))) ||
      (album.manual && libraryHasArtistTitle(library, artistName, track.title));
    return file
      ? { id: file.path, track_number: trackNumber, disc_number: discNumber, title: file.metadata?.title || track.title, file, in_library: inLibrary }
      : {
          id: `${artistName}:${album.name}:${discNumber}:${trackNumber}:${track.title}`,
          track_number: trackNumber,
          disc_number: discNumber,
          title: track.title || `Track ${trackNumber}`,
          reason: recordTracks ? "Missing from album record" : libraryAlbum ? "Missing from import" : "Album slot",
          in_library: inLibrary,
        };
  });
  files.forEach((file, index) => {
    if (usedPaths.has(file.path)) return;
    const trackNumber = file.metadata?.track_number || expectedTracks.length + index + 1;
    slots.push({
      id: file.path,
      track_number: trackNumber,
      disc_number: file.metadata?.disc_number || 1,
      title: file.metadata?.title || `Track ${trackNumber}`,
      file,
      unmatched: true,
    });
  });
  const matchedCount = slots.filter((slot) => slot.file).length;
  const matchStatus = libraryAlbum ? (matchedCount >= expectedTracks.length ? "full" : "partial") : "new";
  return {
    ...album,
    cover_art_url: album.cover_art_url || "",
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

// Album-agnostic: does the artist already own a track with this title under ANY album?
// Used for Singles/playlist imports whose library copy lives under a different album name.
function libraryHasArtistTitle(library, artistName, title) {
  const normalizedArtist = normalizeName(artistName);
  const normalizedTitle = normalizeName(title);
  if (!normalizedTitle) return false;
  const artist =
    library.find((entry) => normalizeName(entry.name) === normalizedArtist) ||
    library.find((entry) => normalizeName(entry.name).includes(normalizedArtist) || normalizedArtist.includes(normalizeName(entry.name)));
  if (!artist) return false;
  return (artist.albums || []).some((album) =>
    (album.tracks || []).some((t) => normalizeName(t.title) === normalizedTitle),
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

function toggleTrackSelection(setter, path, additive) {
  setter((current) => {
    const next = additive ? new Set(current) : new Set();
    if (additive && next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
}

function dragPathsForTrack(selectedTracks, path) {
  return selectedTracks.has(path) ? [...selectedTracks] : [path];
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

function upsertPlaylist(playlists, playlist) {
  const withoutPlaylist = playlists.filter((current) => current.id !== playlist.id);
  return [...withoutPlaylist, playlist].sort((a, b) => a.name.localeCompare(b.name));
}

function upsertUser(users, user) {
  const withoutUser = users.filter((current) => current.id !== user.id);
  return [...withoutUser, user].sort((a, b) => a.display_name.localeCompare(b.display_name));
}

function favoritePlaylistFrom(playlists) {
  return (
    playlists.find((playlist) => playlist.protected) ||
    playlists.find((playlist) => playlist.name === "Favorites") ||
    null
  );
}

function activePlaybackRows(playback) {
  return [...(playback?.app || []), ...(playback?.jellyfin || [])].filter((row) =>
    row?.title && ["playing", "paused"].includes(String(row.status || "").toLowerCase()),
  );
}

function toggleArrayValue(values, value) {
  return values.includes(value) ? values.filter((entry) => entry !== value) : [...values, value].sort();
}

function stablePermissionKey(values) {
  return [...values].sort().join("|");
}

function visibleTrayNotifications(notifications) {
  return notifications.filter((notification) => !["Favorites synced", "Playlists synced"].includes(notification.title));
}

function mergeTrayNotifications(serverNotifications, currentNotifications) {
  const localNotifications = currentNotifications.filter((notification) => String(notification.id).startsWith("local:"));
  const serverVisible = visibleTrayNotifications(serverNotifications);
  return [...localNotifications, ...serverVisible].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
}

function buildAppearanceVars(dark, accentColor, backgroundTint) {
  if (dark) {
    return {
      "--accent-color": accentColor,
      "--background-tint": backgroundTint,
      "--bg": `color-mix(in srgb, ${backgroundTint} 10%, #101216)`,
      "--panel": `color-mix(in srgb, ${backgroundTint} 8%, #181b20)`,
      "--panel-strong": `color-mix(in srgb, ${backgroundTint} 9%, #20242b)`,
      "--line": `color-mix(in srgb, ${backgroundTint} 13%, #30333a)`,
      "--accent": `color-mix(in srgb, ${accentColor} 82%, #ffffff)`,
      "--accent-strong": `color-mix(in srgb, ${accentColor} 70%, #ffffff)`,
      "--accent-soft": `color-mix(in srgb, ${accentColor} 21%, transparent)`,
      "--soft": `color-mix(in srgb, ${backgroundTint} 18%, #16191e)`,
    };
  }
  return {
    "--accent-color": accentColor,
    "--background-tint": backgroundTint,
    "--bg": `color-mix(in srgb, ${backgroundTint} 7%, #f1f2f4)`,
    "--panel": `color-mix(in srgb, ${backgroundTint} 4%, #fafafa)`,
    "--panel-strong": "#ffffff",
    "--line": `color-mix(in srgb, ${backgroundTint} 10%, #d6d8dc)`,
    "--accent": accentColor,
    "--accent-strong": `color-mix(in srgb, ${accentColor} 72%, #0d1b2a)`,
    "--accent-soft": `color-mix(in srgb, ${accentColor} 13%, transparent)`,
    "--soft": `color-mix(in srgb, ${backgroundTint} 11%, #ffffff)`,
  };
}

function readInitialAppearance() {
  try {
    const parsed = JSON.parse(localStorage.getItem(APPEARANCE_LAST_KEY) || "null");
    if (!parsed || typeof parsed !== "object") return DEFAULT_APPEARANCE;
    return {
      dark: Boolean(parsed.dark),
      accentColor: parsed.accentColor || DEFAULT_APPEARANCE.accentColor,
      backgroundTint: parsed.backgroundTint || DEFAULT_APPEARANCE.backgroundTint,
    };
  } catch {
    return DEFAULT_APPEARANCE;
  }
}

function metadataChangeRows(item) {
  if (item.kind !== "metadata") return [];
  const oldValues = parseJsonObject(item.old_value);
  const newValues = parseJsonObject(item.new_value);
  return Object.entries(newValues).map(([field, newValue]) => ({
    field,
    oldValue: formatMetadataValue(oldValues[field]),
    newValue: formatMetadataValue(newValue),
  }));
}

function parseJsonObject(value) {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function formatMetadataValue(value) {
  if (value === null || value === undefined || value === "") return "blank";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

function taskSummary(task) {
  if (task.error) return task.error;
  if (task.result?.errors?.length) return task.result.errors.join("; ");
  if (task.type === "check_files" && task.result) {
    const queued = (task.result.queued_missing_files || 0) + (task.result.queued_missing_records || 0);
    return `${queued} fixes added to task queue`;
  }
  if (task.type === "check_lyrics" && task.result) {
    return `${task.result.missing || 0} missing lyrics, ${task.result.existing || 0} already present`;
  }
  if (task.type === "execute_proposal_batch" && task.result) {
    return proposalTaskSummary(task.result);
  }
  if (task.result?.imported !== undefined) return `${task.result.imported} imported${task.result.skipped ? `, ${task.result.skipped} skipped` : ""}`;
  return new Date(task.created_at).toLocaleString();
}

function taskProgress(task) {
  const progress = task.result?.progress;
  if (!progress) return null;
  const total = Number(progress.total) || 0;
  const current = Number(progress.current) || 0;
  return {
    current,
    total,
    percent: Number(progress.percent ?? (total ? (current / total) * 100 : 0)),
    message: progress.message || taskSummary({ ...task, result: null }),
  };
}

function proposalTaskSummary(result) {
  if (result.progress?.message) return result.progress.message;
  const parts = [];
  if (result.imported) parts.push(`${result.imported} imported`);
  if (result.metadata_updated) parts.push(`${result.metadata_updated} metadata`);
  if (result.file_actions) parts.push(`${result.file_actions} files`);
  if (result.playlist_changes) parts.push(`${result.playlist_changes} playlists`);
  if (result.download_changes) parts.push(`${result.download_changes} downloads`);
  if (result.open_downloads) parts.push("downloads are still running");
  if (result.downloaded_import?.imported) parts.push(`${result.downloaded_import.imported} downloaded imports`);
  if (result.lyric_changes) parts.push(`${result.lyric_changes} lyrics`);
  if (result.skipped) parts.push(`${result.skipped} skipped`);
  return parts.length ? parts.join(", ") : "No changes applied";
}

function latestTaskResult(tasks, type) {
  return tasks.find((task) => task.type === type && task.status === "completed" && task.result) || null;
}

function buildLiveLog(tasks, appLogs) {
  const taskEntries = tasks.map((task) => ({
    id: `task:${task.id}`,
    level: task.status === "failed" || task.error || task.result?.errors?.length ? "error" : "info",
    createdAt: task.updated_at || task.created_at,
    text: `[${new Date(task.updated_at || task.created_at).toLocaleString()}] ${task.type} ${task.status}: ${taskSummary(task)}`,
  }));
  const appLogEntries = (appLogs || []).map((entry, index) => ({
    id: `app-log:${index}:${entry.created_at}`,
    level: entry.level || "info",
    createdAt: entry.created_at,
    text: `[${new Date(entry.created_at).toLocaleString()}] ${entry.message || ""}`,
  }));
  return [...taskEntries, ...appLogEntries].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
}

function notificationSeverity(notification) {
  const text = `${notification.title || ""} ${notification.body || ""} ${notification.event_type || ""}`.toLowerCase();
  if (text.includes("failed") || text.includes("first failure") || /[1-9]\d*\s+errors?/.test(text)) return "error";
  if (text.includes("warning") || text.includes("missing")) return "warning";
  if (notification.status === "unread") return "info";
  return "normal";
}

function maxSeverity(current, next) {
  const rank = { normal: 0, info: 1, warning: 2, error: 3 };
  return rank[next] > rank[current] ? next : current;
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

function groupRequestedAlbums(albums) {
  const artistMap = new Map();
  albums
    .filter((album) => album.tracks.length > 0)
    .forEach((album) => {
      if (!artistMap.has(album.artist)) {
        artistMap.set(album.artist, { name: album.artist, albums: [] });
      }
      artistMap.get(album.artist).albums.push(album);
    });
  return [...artistMap.values()].sort((a, b) => a.name.localeCompare(b.name));
}

function buildWishlistTree(items) {
  const artistMap = new Map();
  items.forEach((item) => {
    if (item.status === "removed") return;
    const artistName = item.artist || "Unknown Artist";
    const albumName = item.album || "Singles";
    if (!artistMap.has(artistName)) {
      artistMap.set(artistName, { name: artistName, albumMap: new Map(), itemIds: [] });
    }
    const artist = artistMap.get(artistName);
    if (!artist.albumMap.has(albumName)) {
      artist.albumMap.set(albumName, { name: albumName, request: null, tracks: [], itemIds: [] });
    }
    const album = artist.albumMap.get(albumName);
    artist.itemIds.push(item.id);
    album.itemIds.push(item.id);
    if (item.track) {
      album.tracks.push(item);
    } else {
      album.request = item;
    }
  });
  return [...artistMap.values()]
    .map((artist) => ({
      name: artist.name,
      itemIds: artist.itemIds,
      albums: [...artist.albumMap.values()]
        .filter((album) => album.itemIds.length > 0)
        .map((album) => ({
          ...album,
          tracks: [...album.tracks].sort((a, b) => (a.track || "").localeCompare(b.track || "")),
        }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .filter((artist) => artist.albums.length > 0)
    .sort((a, b) => a.name.localeCompare(b.name));
}

function buildWishlistOwnerTree(items) {
  const ownerMap = new Map();
  items.forEach((item) => {
    if (item.status === "removed") return;
    const ownerId = item.user_id || "unknown";
    if (!ownerMap.has(ownerId)) {
      ownerMap.set(ownerId, { id: ownerId, name: item.owner_name || "Unknown User", items: [] });
    }
    ownerMap.get(ownerId).items.push(item);
  });
  return [...ownerMap.values()]
    .map((owner) => ({
      ...owner,
      itemCount: owner.items.length,
      artists: buildWishlistTree(owner.items),
    }))
    .filter((owner) => owner.itemCount > 0)
    .sort((a, b) => a.name.localeCompare(b.name));
}

function wishlistStatusLabel(status) {
  if (status === "downloading") return "Downloading…";
  if (status === "approved") return "Awaiting Download";
  if (status === "completed") return "Completed";
  if (status === "rejected") return "Rejected";
  if (status === "review" || status === "wanted") return "Awaiting Approval";
  if (status === "removed") return "Removed";
  return status || "Awaiting Approval";
}

function wishlistAlbumMeta(album) {
  const count = album.tracks.length || (album.request ? 1 : 0);
  const statuses = new Set(
    [...album.tracks.map((track) => track.status), album.request?.status].filter(Boolean).map(wishlistStatusLabel),
  );
  const label = count === 1 ? "request" : "requests";
  return `${count} ${label}${statuses.size ? ` · ${[...statuses].join(", ")}` : ""}`;
}

function toggleSet(setter, value) {
  setter((current) => {
    const next = new Set(current);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  });
}

function toggleWishlistItem(setter, id, checked) {
  setter((current) => {
    const next = new Set(current);
    if (checked) next.add(id);
    else next.delete(id);
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
