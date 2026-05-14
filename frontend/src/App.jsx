import React, { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  GripVertical,
  HardDriveUpload,
  Heart,
  ListChecks,
  ListPlus,
  LogOut,
  Maximize2,
  Menu,
  Minimize2,
  Moon,
  Music,
  Pencil,
  Pause,
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
  Sun,
  Trash2,
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
  ["Import/Add", HardDriveUpload],
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
  "Import/Add": "Scan new files, add album records, and prepare them for review.",
  Wishlist: "Prepare wishlist requests and submit them for approval.",
  "Wishlist Approvals": "Prepare wishlist requests and submit them for approval.",
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
  const [page, setPage] = useState("Import/Add");
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
  const [wishlist, setWishlist] = useState([]);
  const [wishlistApprovals, setWishlistApprovals] = useState([]);
  const [playlists, setPlaylists] = useState([]);
  const [favoritesPlaylist, setFavoritesPlaylist] = useState(null);
  const [favoriteTrackIds, setFavoriteTrackIds] = useState(() => new Set());
  const [integrationSettings, setIntegrationSettings] = useState(null);
  const [libraryAlbumChecks, setLibraryAlbumChecks] = useState({});
  const [playerQueue, setPlayerQueue] = useState([]);
  const [currentTrack, setCurrentTrack] = useState(null);
  const [audioUrl, setAudioUrl] = useState("");
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
  const localNotificationCounter = useRef(0);

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
  const activeImportTask = tasks.some((task) => task.type === "propose_import" && ["queued", "running"].includes(task.status));
  const unreadNotifications = useMemo(() => notifications.filter((notification) => notification.status === "unread"), [notifications]);
  const activeSeverity = useMemo(
    () => unreadNotifications.reduce((highest, notification) => maxSeverity(highest, notificationSeverity(notification)), "info"),
    [unreadNotifications],
  );
  const currentTrackIndex = playerQueue.findIndex((track) => track.id === currentTrack?.id);
  const playerDocked = playerOpen && !playerPopped;
  const appearanceVars = useMemo(() => buildAppearanceVars(dark, accentColor, backgroundTint), [dark, accentColor, backgroundTint]);

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
      refreshFavorites();
      refreshWishlistApprovals();
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
      const [me, libraryTree, taskData, notificationData, wishlistData, wishlistApprovalData] = await Promise.all([
        api("/me"),
        api("/library/tree"),
        api("/tasks"),
        api("/notifications"),
        api("/wishlist"),
        api("/wishlist/approvals"),
      ]);
      setUser(me);
      setLibrary(libraryTree);
      setTasks(taskData);
      taskData.forEach((task) => {
        if (task.type === "sync_favorites_jellyfin" && task.status === "completed") {
          syncToastTaskIds.current.add(task.id);
        }
      });
      setNotifications((current) => mergeTrayNotifications(notificationData, current));
      setWishlist(wishlistData);
      setWishlistApprovals(wishlistApprovalData);
      refreshApprovals();
      if (canManageSettings(me)) {
        refreshIntegrationSettings();
      }
      refreshFavorites();
    } catch (refreshError) {
      if (refreshError.message.includes("Invalid API key") || refreshError.message.includes("Missing API key")) {
        setError("");
        logout();
      } else {
        setError(refreshError.message);
      }
    } finally {
      setLoading(false);
    }
  }

  async function refreshTasks() {
    try {
      const taskData = await api("/tasks");
      showCompletedSyncToasts(taskData);
      setTasks(taskData);
    } catch {
      // Task polling should not disrupt the page the user is working in.
    }
  }

  function showCompletedSyncToasts(taskData) {
    taskData.forEach((task) => {
      if (task.type !== "sync_favorites_jellyfin") return;
      if (task.status !== "completed") return;
      if (syncToastTaskIds.current.has(task.id)) return;
      syncToastTaskIds.current.add(task.id);
      setToast({
        title: "Favorites synced",
        body: `${task.result?.synced || 0} tracks were sent to Jellyfin.`,
      });
    });
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

  async function saveIntegrationSettings(settings) {
    setLoading(true);
    setError("");
    try {
      const saved = await api("/settings/integrations", {
        method: "PUT",
        body: JSON.stringify(settings),
      });
      setIntegrationSettings(saved);
      setToast({ title: "Settings saved", body: "Integration settings were updated." });
    } catch (settingsError) {
      setError(settingsError.message);
      notify("Settings failed", settingsError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function refreshFavorites() {
    try {
      const playlistData = await api("/playlists");
      setPlaylists(playlistData);
      const favorites = playlistData.find((playlist) => playlist.name === "Favorites") || (await api("/playlists/favorites"));
      setFavoritesPlaylist(favorites);
      setFavoriteTrackIds(new Set(favorites.track_ids || []));
    } catch {
      // Favorites are optional for users without playlist permissions.
    }
  }

  async function createPlaylist(name) {
    setLoading(true);
    setError("");
    try {
      const playlist = await api("/playlists", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setPlaylists((current) => upsertPlaylist(current, playlist));
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
    setError("");
    try {
      const playlist = await api(`/playlists/${playlistId}/tracks`, {
        method: "POST",
        body: JSON.stringify({ track_ids: trackIds }),
      });
      setPlaylists((current) => upsertPlaylist(current, playlist));
      if (playlist.name === "Favorites") {
        setFavoritesPlaylist(playlist);
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
      const favorites = await api(`/playlists/favorites/tracks/${track.id}`, {
        method: wasFavorite ? "DELETE" : "POST",
      });
      setFavoritesPlaylist(favorites);
      setPlaylists((current) => upsertPlaylist(current, favorites));
      setFavoriteTrackIds(new Set(favorites.track_ids || []));
      setToast({
        title: wasFavorite ? "Removed from Favorites" : "Added to Favorites",
        body: track._artist ? `${track.title} by ${track._artist}` : track.title,
      });
    } catch (favoriteError) {
      setError(favoriteError.message);
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
      setError(clearError.message);
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

  async function createWishlistItem(item) {
    setLoading(true);
    setError("");
    try {
      const created = await api("/wishlist", {
        method: "POST",
        body: JSON.stringify(item),
      });
      setWishlist((current) => [created, ...current]);
      setToast({ title: "Wishlist updated", body: "The item was added to the wishlist." });
      return created;
    } catch (wishlistError) {
      setError(wishlistError.message);
      notify("Wishlist failed", wishlistError.message, "ui_error");
      throw wishlistError;
    } finally {
      setLoading(false);
    }
  }

  async function removeWishlistItem(itemId) {
    setLoading(true);
    setError("");
    try {
      const updated = await api(`/wishlist/${itemId}`, { method: "DELETE" });
      setWishlist((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setToast({ title: "Wishlist updated", body: "The track was removed." });
      return updated;
    } catch (wishlistError) {
      setError(wishlistError.message);
      notify("Wishlist failed", wishlistError.message, "ui_error");
      throw wishlistError;
    } finally {
      setLoading(false);
    }
  }

  async function submitWishlistApprovals() {
    setLoading(true);
    setError("");
    try {
      const batch = await api("/wishlist/approvals", { method: "POST" });
      setWishlistApprovals((current) => [batch, ...current.filter((item) => item.id !== batch.id)]);
      await refreshApprovals();
      const wishlistData = await api("/wishlist");
      setWishlist(wishlistData);
      setToast({ title: "Wishlist review queued", body: "Wishlist items were added for approval." });
      return batch;
    } catch (wishlistError) {
      notify("Wishlist review failed", wishlistError.message, "ui_error");
      throw wishlistError;
    } finally {
      setLoading(false);
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
      notify("Import scan failed", scanError.message, "ui_error");
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
      notify("Import review failed", proposeError.message, "ui_error");
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
      notify("Metadata lookup failed", lookupError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function recheckImportAlbum(album) {
    const albumFiles = album.files || [];
    if (albumFiles.length === 0) return;
    setLoading(true);
    setError("");
    let nextFiles = importFiles;
    let matched = 0;
    let changed = 0;
    let missing = 0;
    let failed = 0;
    try {
      for (const file of albumFiles) {
        try {
          const data = await api("/imports/acoustic-match", {
            method: "POST",
            body: JSON.stringify({ file }),
          });
          const candidate = data.candidates?.[0];
          if (!candidate) {
            missing += 1;
            nextFiles = patchImportFile(nextFiles, file.path, { acoustid_match: "no match" });
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
            acoustid_match: matchStatus,
            acoustid_score: Math.round((candidate.score || 0) * 100),
          });
        } catch {
          failed += 1;
        }
      }
      setImportFiles(nextFiles);
      setToast({
        title: "Album AcoustID check complete",
        body: `${matched} matched. ${changed} updated. ${missing} unmatched. ${failed} failed.`,
      });
    } finally {
      setLoading(false);
    }
  }

  async function checkLibraryAlbumAcoustID(album) {
    setLoading(true);
    setError("");
    try {
      const data = await api(`/library/albums/${album.id}/acoustic-match`, { method: "POST" });
      setLibraryAlbumChecks((current) => ({ ...current, [album.id]: data }));
      const counts = countAcousticStatuses(data.tracks || []);
      setToast({
        title: "Album AcoustID check complete",
        body: `${counts.matched} matched. ${counts.changed} changed. ${counts.unmatched} unmatched. ${counts.failed} failed.`,
      });
    } catch (lookupError) {
      notify("Album AcoustID check failed", lookupError.message, "ui_error");
    } finally {
      setLoading(false);
    }
  }

  async function lookupImportAlbum(artist, album, releaseId = null) {
    setLoading(true);
    setError("");
    try {
      const data = await api("/imports/album-lookup", {
        method: "POST",
        body: JSON.stringify({ artist, album, release_id: releaseId }),
      });
      setToast({ title: "Album checked", body: `${data.tracks?.length || 0} tracks found.` });
      return data;
    } catch (lookupError) {
      setError(lookupError.message);
      notify("Album lookup failed", lookupError.message, "ui_error");
      return null;
    } finally {
      setLoading(false);
    }
  }

  async function searchImportAlbums(artist, album) {
    setLoading(true);
    setError("");
    try {
      const data = await api("/imports/album-search", {
        method: "POST",
        body: JSON.stringify({ artist, album }),
      });
      return data.results || [];
    } catch (lookupError) {
      setError(lookupError.message);
      notify("Album search failed", lookupError.message, "ui_error");
      return [];
    } finally {
      setLoading(false);
    }
  }

  async function proposeLibraryMetadata(targetType, targetId, changes) {
    setLoading(true);
    setError("");
    try {
      const batch = await api("/library/metadata", {
        method: "POST",
        body: JSON.stringify({ target_type: targetType, target_id: targetId, changes }),
      });
      setApprovals((current) => [batch, ...current.filter((entry) => entry.id !== batch.id)]);
      setToast({ title: "Metadata queued", body: "The change was added to the task queue." });
      return batch;
    } catch (metadataError) {
      setError(metadataError.message);
      notify("Metadata queue failed", metadataError.message, "ui_error");
      throw metadataError;
    } finally {
      setLoading(false);
    }
  }

  async function proposeLibraryRemove(targetType, targetId, action) {
    setLoading(true);
    setError("");
    try {
      const batch = await api("/library/remove", {
        method: "POST",
        body: JSON.stringify({ target_type: targetType, target_id: targetId, action }),
      });
      setApprovals((current) => [batch, ...current.filter((entry) => entry.id !== batch.id)]);
      setToast({ title: "Library change queued", body: "The removal request was added to the task queue." });
      return batch;
    } catch (removeError) {
      setError(removeError.message);
      notify("Queue request failed", removeError.message, "ui_error");
      throw removeError;
    } finally {
      setLoading(false);
    }
  }

  async function proposePlaylistPosition(entryId, position) {
    setLoading(true);
    setError("");
    try {
      const batch = await api(`/playlists/entries/${entryId}/position`, {
        method: "POST",
        body: JSON.stringify({ position }),
      });
      setApprovals((current) => [batch, ...current.filter((entry) => entry.id !== batch.id)]);
      setToast({ title: "Playlist change queued", body: "The order change was added to the task queue." });
      return batch;
    } catch (playlistError) {
      setError(playlistError.message);
      notify("Playlist queue failed", playlistError.message, "ui_error");
      throw playlistError;
    } finally {
      setLoading(false);
    }
  }

  async function syncPlaylists() {
    setLoading(true);
    setError("");
    try {
      const task = await api("/playlists/sync", { method: "POST" });
      setTasks((current) => upsertTask(current, task));
      setToast({ title: "Playlist sync queued", body: "Favorites sync was added to activity." });
      return task;
    } catch (syncError) {
      setError(syncError.message);
      notify("Playlist sync failed", syncError.message, "ui_error");
      throw syncError;
    } finally {
      setLoading(false);
    }
  }

  async function playTracks(tracks) {
    const playable = tracks.filter((track) => track?.id);
    if (playable.length === 0) return;
    setPlayerQueue(playable);
    setPlayerOpen(true);
    setQueueOpen(false);
    await loadPlayerTrack(playable[0]);
  }

  function addTracksToPlayerQueue(tracks) {
    const playable = tracks.filter((track) => track?.id);
    if (playable.length === 0) return;
    setPlayerQueue((current) => [...current, ...playable]);
    setPlayerOpen(true);
    setToast({ title: "Queue updated", body: `${playable.length} track${playable.length === 1 ? "" : "s"} added locally.` });
  }

  async function loadPlayerTrack(track) {
    if (!track?.id) return;
    try {
      setAudioUrl(`${API_BASE}/library/tracks/${track.id}/stream?api_key=${encodeURIComponent(token)}`);
      setCurrentTrack(track);
    } catch (playError) {
      setError(`Playback failed: ${playError.message}`);
      notify("Playback failed", playError.message, "ui_error");
    }
  }

  async function playNextTrack() {
    if (playerQueue.length === 0) return;
    const index = currentTrackIndex < 0 ? -1 : currentTrackIndex;
    const nextTrack = playerQueue[index + 1];
    if (nextTrack) await loadPlayerTrack(nextTrack);
  }

  async function playPreviousTrack() {
    if (playerQueue.length === 0) return;
    const previousTrack = playerQueue[currentTrackIndex - 1] || playerQueue[0];
    if (previousTrack) await loadPlayerTrack(previousTrack);
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
          body: JSON.stringify({ item_ids: batchItems.map((item) => item.id), suppress_for: "week" }),
        });
      }
      setToast({ title: "Changes rejected", body: "Selected items were suppressed for one week." });
      await refreshApprovals();
    } catch (rejectError) {
      setError(rejectError.message);
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
          {navItems.map(([label, Icon]) => (
            <button className={page === label ? "active" : ""} key={label} onClick={() => setPage(label)}>
              <Icon size={17} />
              {label === "Wishlist" && !user?.is_admin ? "Wishlist Approvals" : label}
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
            <button className="icon-button" onClick={openNotificationTray} title="Notifications">
              <Bell size={18} />
              {unreadNotifications.length > 0 && <span className="badge">{unreadNotifications.length}</span>}
              {unreadNotifications.length > 0 && <span className={`severity-dot ${activeSeverity}`} />}
            </button>
            {trayOpen && <NotificationTray notifications={notifications} onClear={clearNotifications} />}
          </div>
          <button className="icon-button" onClick={logout} title="Sign out">
            <LogOut size={18} />
          </button>
        </header>

        <div className="content-grid">
          <section className="panel main-panel">
            <PanelHeader page={page === "Wishlist" && !user?.is_admin ? "Wishlist Approvals" : page} queueSummary={queueSummary} />
            {loading && <div className="loading-line">Working...</div>}
            {page === "Library" && (
              <LibraryTree
                artists={library}
                onCheckAlbum={lookupImportAlbum}
                onCheckAlbumAcoustID={checkLibraryAlbumAcoustID}
                albumChecks={libraryAlbumChecks}
                onSearchAlbums={searchImportAlbums}
                onQueueMetadata={proposeLibraryMetadata}
                onQueueRemove={proposeLibraryRemove}
                playlists={playlists}
                onAddToPlaylist={addTracksToPlaylist}
                onPlay={playTracks}
                onQueue={addTracksToPlayerQueue}
              />
            )}
            {page === "Task Queue" && (
              <Approvals
                approvals={approvals}
                onSelection={setApprovalSelection}
                onApprove={approveItems}
                onReject={rejectItems}
              />
            )}
            {page === "Import/Add" && (
              <ImportWizard
                files={importFiles}
                onScan={scanImportFolder}
                onPropose={proposeImport}
                onFilesChange={setImportFiles}
                library={library}
                onRecheckTrack={recheckImportTrack}
                onRecheckAlbum={recheckImportAlbum}
                onCheckAlbum={lookupImportAlbum}
                onSearchAlbums={searchImportAlbums}
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
                dark={dark}
                setDark={setDark}
                user={user}
                apiKey={token}
                integrationSettings={integrationSettings}
                onSaveIntegrations={saveIntegrationSettings}
              />
            )}
            {page === "Tools" && <ToolsView tasks={tasks} notifications={notifications} />}
            {page === "Wishlist" && (
              <WishlistView
                wishlist={wishlist}
                approvals={wishlistApprovals}
                onAdd={createWishlistItem}
                onRemove={removeWishlistItem}
                onSubmit={submitWishlistApprovals}
                onSearchAlbums={searchImportAlbums}
                onLookupAlbum={lookupImportAlbum}
              />
            )}
            {page === "Playlists" && (
              <PlaylistsView
                playlists={playlists.length ? playlists : favoritesPlaylist ? [favoritesPlaylist] : []}
                library={library}
                onCreatePlaylist={createPlaylist}
                onAddToPlaylist={addTracksToPlaylist}
                onQueuePosition={proposePlaylistPosition}
                onPlay={playTracks}
                onQueue={addTracksToPlayerQueue}
                onSync={syncPlaylists}
              />
            )}
            {!["Library", "Task Queue", "Import/Add", "Activity", "Settings", "Tools", "Wishlist", "Playlists"].includes(page) && <Placeholder page={page} />}
          </section>

          <Inspector page={page} importFiles={importFiles} queueItemCount={queueItemCount} queueSelectionCount={queueSelectionCount} tasks={tasks} />
        </div>
        {toast && <Toast title={toast.title} body={toast.body} onClose={() => setToast(null)} />}
        {playerOpen && (
          <AudioPlayer
            currentTrack={currentTrack}
            audioUrl={audioUrl}
            queue={playerQueue}
            currentIndex={currentTrackIndex}
            queueOpen={queueOpen}
            setQueueOpen={setQueueOpen}
            onPlayTrack={loadPlayerTrack}
            onEnded={playNextTrack}
            onSkipBack={playPreviousTrack}
            onSkipForward={playNextTrack}
            onFavorite={toggleFavoriteTrack}
            favoriteTrackIds={favoriteTrackIds}
            onDockChange={({ popped, compactHeight, fullHeight }) => {
              setPlayerPopped(popped);
              setPlayerDockHeight(compactHeight || 0);
              setPlayerToastHeight(fullHeight || compactHeight || 0);
            }}
            onClose={() => setPlayerOpen(false)}
          />
        )}
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

function PanelHeader({ page, queueSummary }) {
  const description = page === "Task Queue" ? queueSummary : pageDescriptions[page];

  return (
    <div className="panel-header">
      <div>
        <h1>{page}</h1>
        <p>{description ?? "Manage this section of Nudibranch."}</p>
      </div>
    </div>
  );
}

function LibraryTree({ artists, onCheckAlbum, onCheckAlbumAcoustID, albumChecks, onSearchAlbums, onQueueMetadata, onQueueRemove, playlists, onAddToPlaylist, onPlay, onQueue }) {
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
          albums: artist.albums.filter((album) => album.tracks.length > 0),
        }))
        .filter((artist) => artist.albums.length > 0),
    [artists],
  );
  return (
    <div className="library-view">
      {visibleArtists.length === 0 && (
        <EmptyState title="No library records" body="Import queued music to populate the managed library." />
      )}
      {visibleArtists.length > 0 && (
        <TreeToolbar
          expanded={openArtists.size > 0 || openAlbums.size > 0}
          onExpand={() => {
            setOpenArtists(new Set(visibleArtists.map((artist) => artist.id)));
            setOpenAlbums(new Set(visibleArtists.flatMap((artist) => artist.albums.map((album) => album.id))));
          }}
          onCollapse={() => {
            setOpenArtists(new Set());
            setOpenAlbums(new Set());
          }}
        />
      )}
      <div className="tree">
        {visibleArtists.map((artist) => (
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
                onPlay={() => onPlay(artistTracks(artist))}
                onQueue={() => onQueue(artistTracks(artist))}
                onRemove={() => setRemoveTarget(removeKey("artist", artist.id))}
              />
              <button className="row-icon-button" onClick={() => toggleSet(setOpenArtistDetails, artist.id)} title="Edit artist">
                <Pencil size={15} />
              </button>
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
                playlists={playlists}
                targetTrackIds={artistTracks(artist).map((track) => track.id)}
                onAddToPlaylist={onAddToPlaylist}
                onQueue={onQueueMetadata}
                onClose={() => toggleSet(setOpenArtistDetails, artist.id)}
              />
            )}
            {openArtists.has(artist.id) &&
              artist.albums.map((album) => (
                <div key={album.id}>
                  <div className="tree-action-row library-row-actions">
                    <TreeRow
                      depth={1}
                      icon={Folder}
                      open={openAlbums.has(album.id)}
                      title={album.title}
                      meta={`${album.tracks.length} tracks`}
                      onToggle={() => toggleSet(setOpenAlbums, album.id)}
                    />
                    <QuickLibraryActions
                      onPlay={() => onPlay(albumTracks(artist, album))}
                      onQueue={() => onQueue(albumTracks(artist, album))}
                    />
                    <button className="row-icon-button" onClick={() => toggleSet(setOpenAlbumDetails, album.id)} title="Edit album">
                      <Pencil size={15} />
                    </button>
                  </div>
                  {albumChecks[album.id] && (
                    <div className="album-acoustic-results">
                      {albumChecks[album.id].tracks.map((result) => (
                        <TreeRow
                          key={result.track_id}
                          depth={2}
                          icon={Search}
                          title={result.title}
                          meta={acousticResultMeta(result)}
                          warning={["changed", "unmatched", "missing_file", "error"].includes(result.status)}
                        />
                      ))}
                    </div>
                  )}
                  {removeTarget === removeKey("album", album.id) && (
                    <RemoveChoice
                      title={album.title}
                      onCancel={() => setRemoveTarget(null)}
                      onChoose={(action) => {
                        onQueueRemove("album", album.id, action);
                        setRemoveTarget(null);
                      }}
                    />
                  )}
                  {openAlbumDetails.has(album.id) && (
                    <LibraryMetadataEditor
                      targetType="album"
                      targetId={album.id}
                      title={album.title}
                      coverUrl={album.cover_path}
                      fields={albumFields(album)}
                      details={{ artist: artist.name, tracks: album.tracks.length }}
                      onAutoLookup={(field, draft) => albumAutoLookup(field, draft, artist.name, onCheckAlbum)}
                      onSearchAlbums={onSearchAlbums}
                      playlists={playlists}
                      targetTrackIds={albumTracks(artist, album).map((track) => track.id)}
                      onAddToPlaylist={onAddToPlaylist}
                      onAcousticCheck={() => onCheckAlbumAcoustID(album)}
                      onRemove={() => setRemoveTarget(removeKey("album", album.id))}
                      onQueue={onQueueMetadata}
                      onClose={() => toggleSet(setOpenAlbumDetails, album.id)}
                    />
                  )}
                  {openAlbums.has(album.id) &&
                    album.tracks.map((track) => (
                      <div key={track.id}>
                        <div className="tree-action-row library-row-actions">
                          <TreeRow
                            depth={2}
                            icon={FileAudio}
                            title={`${track.track_number ? String(track.track_number).padStart(2, "0") : "#"}-${track.title}`}
                            meta={track.format || "audio"}
                            warning={!track.is_lossless}
                          />
                          <QuickLibraryActions
                            onPlay={() => onPlay([hydrateTrack(track, artist, album)])}
                            onQueue={() => onQueue([hydrateTrack(track, artist, album)])}
                            onRemove={() => setRemoveTarget(removeKey("track", track.id))}
                          />
                          <button className="row-icon-button" onClick={() => toggleSet(setOpenTrackDetails, track.id)} title="Edit song">
                            <Pencil size={15} />
                          </button>
                        </div>
                        {removeTarget === removeKey("track", track.id) && (
                          <RemoveChoice
                            title={track.title}
                            onCancel={() => setRemoveTarget(null)}
                            onChoose={(action) => {
                              onQueueRemove("track", track.id, action);
                              setRemoveTarget(null);
                            }}
                          />
                        )}
                        {openTrackDetails.has(track.id) && (
                          <LibraryMetadataEditor
                            targetType="track"
                            targetId={track.id}
                            title={track.title}
                            fields={trackFields(track)}
                            details={{ artist: artist.name, album: album.title }}
                            onAutoLookup={(field, draft) => trackAutoLookup(field, draft, artist.name, album.title, onCheckAlbum)}
                            onSearchAlbums={onSearchAlbums}
                            playlists={playlists}
                            targetTrackIds={[track.id]}
                            onAddToPlaylist={onAddToPlaylist}
                            onQueue={onQueueMetadata}
                            onClose={() => toggleSet(setOpenTrackDetails, track.id)}
                          />
                        )}
                      </div>
                    ))}
                </div>
              ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function QuickLibraryActions({ onPlay, onQueue, onRemove }) {
  return (
    <div className="quick-library-actions">
      <button className="row-icon-button" onClick={onPlay} title="Play">
        <Play size={14} />
      </button>
      <button className="row-icon-button" onClick={onQueue} title="Add to local queue">
        <ListPlus size={14} />
      </button>
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
          key={item.id}
        />
      ))}
    </section>
  );
}

function ApprovalNode({ item, childrenById, openItems, setOpenItems, onSelection, depth = 0 }) {
  const children = childrenById.get(item.id) || [];
  const metadataChanges = metadataChangeRows(item);
  const hasChildren = children.length > 0 || metadataChanges.length > 0;
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
        <small>{metadataChanges.length > 0 ? `${metadataChanges.length} changes` : item.kind}</small>
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
            depth={depth + 1}
            key={child.id}
          />
        ))}
    </>
  );
}

function ImportWizard({
  files,
  onScan,
  onPropose,
  onFilesChange,
  library,
  onRecheckTrack,
  onRecheckAlbum,
  onCheckAlbum,
  onSearchAlbums,
  loading,
  activeImportTask,
}) {
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
    setResults(await onSearch(artist.trim(), album.trim()));
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
                <img src={result.cover_art_url || ""} alt="" />
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

function WishlistView({ wishlist, approvals, onAdd, onRemove, onSubmit, onSearchAlbums, onLookupAlbum }) {
  const [albumSearchOpen, setAlbumSearchOpen] = useState(false);
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const tree = useMemo(() => buildWishlistTree(wishlist), [wishlist]);
  const treeKey = useMemo(
    () => tree.map((artist) => `${artist.name}:${artist.albums.map((album) => album.name).join(",")}`).join("|"),
    [tree],
  );

  useEffect(() => {
    setOpenArtists(new Set(tree.map((artist) => artist.name)));
    setOpenAlbums(new Set(tree.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
  }, [treeKey]);

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

  return (
    <div className="wishlist-view">
      <div className="action-bar">
        <button className="secondary" onClick={() => setAlbumSearchOpen((value) => !value)}>
          <Plus size={16} />
          Add album
        </button>
        <button className="primary" onClick={onSubmit} disabled={wishlist.filter((item) => item.status === "wanted").length === 0}>
          <ListChecks size={16} />
          Add to approvals
        </button>
      </div>
      {albumSearchOpen && <AlbumSearchPanel onAdd={addAlbumToWishlist} onLookup={onLookupAlbum} onSearch={onSearchAlbums} />}
      {wishlist.length === 0 ? (
        <EmptyState title="No wishlist items" body="Add music here before sending wishlist work to the task queue." />
      ) : (
        <div className="tree">
          <TreeToolbar
            expanded={openArtists.size > 0 || openAlbums.size > 0}
            onExpand={() => {
              setOpenArtists(new Set(tree.map((artist) => artist.name)));
              setOpenAlbums(new Set(tree.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
            }}
            onCollapse={() => {
              setOpenArtists(new Set());
              setOpenAlbums(new Set());
            }}
          />
          {tree.map((artist) => (
            <div key={artist.name}>
              <TreeRow
                icon={Sparkles}
                open={openArtists.has(artist.name)}
                title={artist.name}
                meta={`${artist.albums.length} albums`}
                onToggle={() => toggleSet(setOpenArtists, artist.name)}
              />
              {openArtists.has(artist.name) &&
                artist.albums.map((album) => {
                  const albumId = `${artist.name}/${album.name}`;
                  return (
                    <div key={albumId}>
                      <TreeRow
                        depth={1}
                        icon={Folder}
                        open={openAlbums.has(albumId)}
                        title={album.name}
                        meta={`${album.tracks.length || 1} requested`}
                        onToggle={() => toggleSet(setOpenAlbums, albumId)}
                      />
                      {openAlbums.has(albumId) &&
                        (album.tracks.length > 0 ? (
                          album.tracks.map((track) => (
                            <div className={track.status === "removed" ? "tree-action-row wishlist-row removed" : "tree-action-row wishlist-row"} key={track.id}>
                              <TreeRow depth={2} icon={FileAudio} title={track.track || "Track"} meta={track.status} />
                              {track.status !== "removed" && (
                                <button className="row-icon-button" onClick={() => onRemove(track.id)} title="Remove track">
                                  <X size={15} />
                                </button>
                              )}
                            </div>
                          ))
                        ) : (
                          <div className={album.request?.status === "removed" ? "tree-action-row wishlist-row removed" : "tree-action-row wishlist-row"}>
                            <TreeRow depth={2} icon={FileAudio} title={album.request?.album || "Full album"} meta={album.request?.status || "wanted"} />
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
          ))}
        </div>
      )}
      {approvals.length > 0 && (
        <div className="wishlist-approval-list">
          <h2>Submitted approvals</h2>
          {approvals.map((batch) => (
            <div className="task-row" key={batch.id}>
              <strong>{batch.title}</strong>
              <span>{batch.status} · {batch.items.length} items</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function PlaylistsView({ playlists, library, onCreatePlaylist, onAddToPlaylist, onQueuePosition, onPlay, onQueue, onSync }) {
  const [openPlaylists, setOpenPlaylists] = useState(() => new Set(["Favorites"]));
  const [addOpen, setAddOpen] = useState(null);
  const [playlistName, setPlaylistName] = useState("");
  const [playlistSearch, setPlaylistSearch] = useState("");
  const [draftPositions, setDraftPositions] = useState({});

  useEffect(() => {
    setDraftPositions(
      Object.fromEntries(playlists.flatMap((playlist) => playlist.tracks.map((track) => [track.id, String(track.position || "")]))),
    );
  }, [playlists.map((playlist) => `${playlist.id}:${playlist.track_count}`).join("|")]);

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

  return (
    <div className="playlist-view">
      <TreeToolbar
        expanded={openPlaylists.size > 0}
        onExpand={() => setOpenPlaylists(new Set(playlists.map((playlist) => playlist.name)))}
        onCollapse={() => setOpenPlaylists(new Set())}
      >
        <form
          className="playlist-create"
          onSubmit={(event) => {
            event.preventDefault();
            if (!playlistName.trim()) return;
            onCreatePlaylist(playlistName.trim()).then(() => setPlaylistName(""));
          }}
        >
          <input value={playlistName} onChange={(event) => setPlaylistName(event.target.value)} placeholder="New playlist" />
          <button className="secondary compact">
            <Plus size={15} />
            Create
          </button>
        </form>
        <button className="secondary compact" onClick={onSync}>
          <RefreshCw size={15} />
          Sync
        </button>
      </TreeToolbar>
      {playlists.map((playlist) => {
        const tracks = playlist.tracks || [];
        const playableTracks = tracks.map(playlistPlayableTrack);
        return (
          <div key={playlist.id}>
            <div className="tree-action-row library-row-actions">
              <TreeRow
                icon={playlist.name === "Favorites" ? Heart : FileAudio}
                open={openPlaylists.has(playlist.name)}
                title={playlist.name}
                meta={`${playlist.track_count || 0} tracks`}
                onToggle={() => toggleSet(setOpenPlaylists, playlist.name)}
              />
              <PlaylistPlayActions
                disabled={playableTracks.length === 0}
                onPlay={() => onPlay(playableTracks)}
                onQueue={() => onQueue(playableTracks)}
              />
              <button className="row-icon-button" onClick={() => setAddOpen(addOpen === playlist.id ? null : playlist.id)} title="Add music">
                <Plus size={14} />
              </button>
            </div>
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
                      <label className="playlist-order-field">
                        <span>Order</span>
                        <input
                          value={draftPositions[track.id] ?? String(track.position || "")}
                          inputMode="numeric"
                          onChange={(event) => updateDraft(track.id, event.target.value)}
                          onBlur={() => submitPosition(track)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") event.currentTarget.blur();
                            if (event.key === "Escape") {
                              updateDraft(track.id, String(track.position || ""));
                              event.currentTarget.blur();
                            }
                          }}
                        />
                      </label>
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
      <button className="row-icon-button" onClick={onQueue} disabled={disabled} title="Add to local queue">
        <ListPlus size={14} />
      </button>
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

  useEffect(() => {
    setOpenArtists(new Set(grouped.map((artist) => artist.name)));
    setOpenAlbums(new Set(grouped.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
  }, [files.length, manualAlbums.length]);

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
      {grouped.map((artist) => {
        const visibleAlbums = artist.albums.filter((album) =>
          album.slots.some((slot) => slot.file || !dismissedGhosts.has(slot.id)),
        );
        if (visibleAlbums.length === 0) return null;
        return (
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
              <div className="tree-action-row one-action">
                <TreeRow
                  icon={Folder}
                  open={openArtists.has(artist.name)}
                  title={artist.name}
                  meta={`${artist.count} files`}
                  onToggle={() => toggleSet(setOpenArtists, artist.name)}
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
              const visibleSlots = albumSlots.filter((slot) => slot.file || !dismissedGhosts.has(slot.id));
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
                      <button className="row-icon-button" onClick={() => onRecheckAlbum(album)} title="Check album tracks with AcoustID">
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
                      ) : (
                        <GhostTrackRow
                          key={`${albumId}:${slot.track_number}:${slot.title}`}
                          slot={slot}
                          checked={downloadSelections.has(slot.id)}
                          onChecked={(checked) => toggleDownloadSelection(setDownloadSelections, slot.id, checked)}
                          onDismiss={() => toggleDownloadSelection(setDismissedGhosts, slot.id, true)}
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
          </div>
        );
      })}
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
        <small>{metadata.acoustid_match ? `AcoustID ${metadata.acoustid_match}${metadata.acoustid_score ? ` ${metadata.acoustid_score}%` : ""}` : album?.matchStatus === "full" ? "In library" : formatBytes(file.size_bytes)}</small>
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
  return (
    <div className="album-details">
      <div className="album-art">{coverUrl ? <img src={coverUrl} alt="" /> : <Music size={24} />}</div>
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
  playlists = [],
  targetTrackIds = [],
  onAddToPlaylist,
  onAcousticCheck,
  onRemove,
  onQueue,
  onClose,
}) {
  const initialValues = useMemo(() => initialFieldValues(fields), [targetId]);
  const [draft, setDraft] = useState(() => initialFieldValues(fields));
  const [lookupOpen, setLookupOpen] = useState(false);
  const changed = Object.fromEntries(
    Object.entries(draft).filter(([key, value]) => String(value ?? "") !== String(initialValues[key] ?? "")),
  );
  const hasChanges = Object.keys(changed).length > 0;

  useEffect(() => {
    setDraft(initialValues);
  }, [targetId]);

  async function autoLookup(field) {
    if (!onAutoLookup && !onSearchAlbums) return;
    const patch = onAutoLookup ? await onAutoLookup(field.key, draft) : null;
    if (patch && Object.prototype.hasOwnProperty.call(patch, field.key)) {
      setDraft((current) => ({ ...current, [field.key]: patch[field.key] ?? "" }));
    } else if (onSearchAlbums) {
      setLookupOpen(true);
    }
  }

  async function queueChanges() {
    if (!hasChanges) return;
    await onQueue(targetType, targetId, normalizeEntityChanges(changed, fields));
    onClose?.();
  }

  async function applyLookupAlbum(album) {
    setDraft((current) => ({
      ...current,
      ...metadataPatchFromAlbum(targetType, current, album),
    }));
    setLookupOpen(false);
  }

  return (
    <div className="album-details metadata-panel">
      {coverUrl !== undefined && <div className="album-art">{coverUrl ? <img src={coverUrl} alt="" /> : <Music size={24} />}</div>}
      <div className="library-metadata-form">
        <strong>{title}</strong>
        {Object.entries(details).map(([key, value]) => (
          <small key={key}>
            {key}: {String(value ?? "")}
          </small>
        ))}
        <div className="metadata-field-grid">
          {fields.map((field) => {
            const isChanged = String(draft[field.key] ?? "") !== String(initialValues[field.key] ?? "");
            return (
              <label className={isChanged ? "changed" : ""} key={field.key}>
                <span>{field.label}</span>
                <div className="metadata-input-action">
                  {field.type === "boolean" ? (
                    <input
                      type="checkbox"
                      checked={Boolean(draft[field.key])}
                      onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.checked }))}
                    />
                  ) : (
                    <input
                      type={field.type === "number" ? "number" : "text"}
                      value={draft[field.key] ?? ""}
                      onChange={(event) => setDraft((current) => ({ ...current, [field.key]: event.target.value }))}
                    />
                  )}
                  {(onAutoLookup || onSearchAlbums) && (
                    <button className="row-icon-button" onClick={() => autoLookup(field)} title="Auto lookup">
                      <Search size={14} />
                    </button>
                  )}
                </div>
              </label>
            );
          })}
        </div>
        {lookupOpen && (
          <AlbumSearchPanel
            initialArtist={String(details.artist || draft.artist || title || "")}
            initialAlbum={String(details.album || draft.title || draft.release_title || title || "")}
            onAdd={applyLookupAlbum}
            onLookup={async (artist, album, releaseId) => ({ artist, album, musicbrainz_album_id: releaseId, tracks: [] })}
            onSearch={onSearchAlbums}
          />
        )}
        {(onAcousticCheck || onRemove || (playlists.length > 0 && targetTrackIds.length > 0)) && (
          <div className="metadata-menu-actions">
            {onAcousticCheck && (
              <button className="secondary compact" onClick={onAcousticCheck}>
                <Sparkles size={15} />
                Check AcoustID
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
      <button className="primary compact-button" onClick={queueChanges} disabled={!hasChanges}>
        <ListChecks size={15} />
        Add to task queue
      </button>
    </div>
  );
}

function initialFieldValues(fields) {
  return Object.fromEntries(fields.map((field) => [field.key, field.value ?? ""]));
}

function artistFields(artist) {
  return [
    { key: "name", label: "Name", value: artist.name },
    { key: "sort_name", label: "Sort name", value: artist.sort_name },
    { key: "musicbrainz_id", label: "MusicBrainz ID", value: artist.musicbrainz_id },
  ];
}

function albumFields(album) {
  return [
    { key: "title", label: "Album", value: album.title },
    { key: "release_title", label: "Release title", value: album.release_title },
    { key: "musicbrainz_release_id", label: "MusicBrainz release ID", value: album.musicbrainz_release_id },
    { key: "musicbrainz_release_group_id", label: "MusicBrainz release group ID", value: album.musicbrainz_release_group_id },
    { key: "cover_path", label: "Cover art", value: album.cover_path },
    { key: "path", label: "Path", value: album.path },
  ];
}

function trackFields(track) {
  return [
    { key: "title", label: "Title", value: track.title },
    { key: "track_number", label: "Track number", value: track.track_number, type: "number" },
    { key: "disc_number", label: "Disc number", value: track.disc_number, type: "number" },
    { key: "duration_ms", label: "Duration ms", value: track.duration_ms, type: "number" },
    { key: "format", label: "Format", value: track.format },
    { key: "bitrate", label: "Bitrate", value: track.bitrate, type: "number" },
    { key: "path", label: "Path", value: track.path },
    { key: "musicbrainz_recording_id", label: "MusicBrainz recording ID", value: track.musicbrainz_recording_id },
    { key: "explicit", label: "Explicit", value: track.explicit, type: "boolean" },
    { key: "is_lossless", label: "Lossless", value: track.is_lossless, type: "boolean" },
    { key: "metadata_locked", label: "Metadata locked", value: track.metadata_locked, type: "boolean" },
    { key: "artwork_locked", label: "Artwork locked", value: track.artwork_locked, type: "boolean" },
    { key: "filename_locked", label: "Filename locked", value: track.filename_locked, type: "boolean" },
  ];
}

async function albumAutoLookup(field, draft, artistName, onCheckAlbum) {
  const releaseId = draft.musicbrainz_release_id || null;
  if (field === "cover_path" && releaseId) {
    return { cover_path: `https://coverartarchive.org/release/${releaseId}/front-250` };
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
    _artist: artist.name,
    _album: album.title,
    _coverUrl: album.cover_path,
  };
}

function playlistPlayableTrack(track) {
  return {
    id: track.track_id,
    title: track.title,
    format: track.format,
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

function SettingsPanel({
  accentColor,
  setAccentColor,
  backgroundTint,
  setBackgroundTint,
  dark,
  setDark,
  user,
  apiKey,
  integrationSettings,
  onSaveIntegrations,
}) {
  const [showApiKey, setShowApiKey] = useState(false);
  const [shownIntegrationKeys, setShownIntegrationKeys] = useState({});
  const [integrationDraft, setIntegrationDraft] = useState(integrationSettings || {});
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
      {canManageSettings(user) && (
        <section className="settings-section">
          <h2>Integrations</h2>
          {[
            ["acoustid_api_key", "AcoustID API key"],
            ["jellyfin_url", "Jellyfin URL"],
            ["jellyfin_api_key", "Jellyfin API key"],
            ["slskd_url", "slskd URL"],
            ["slskd_api_key", "slskd API key"],
          ].map(([key, label]) => (
            <label className="setting-row integration-row" key={key}>
              <span>{label}</span>
              <input
                type={key.endsWith("api_key") && !shownIntegrationKeys[key] ? "password" : "text"}
                value={integrationDraft[key] || ""}
                onChange={(event) => setIntegrationDraft((current) => ({ ...current, [key]: event.target.value }))}
              />
              {key.endsWith("api_key") && (
                <button className="secondary compact" type="button" onClick={() => setShownIntegrationKeys((current) => ({ ...current, [key]: !current[key] }))}>
                  {shownIntegrationKeys[key] ? "Hide" : "Show"}
                </button>
              )}
            </label>
          ))}
          <button className="primary compact-button" onClick={() => onSaveIntegrations(integrationDraft)}>
            Save integrations
          </button>
        </section>
      )}
      <footer className="settings-footer">
        Made by Poplel | <a href="https://poplel.xyz" target="_blank" rel="noreferrer">poplel.xyz</a>
      </footer>
    </div>
  );
}

function Inspector({ page, importFiles, queueItemCount, queueSelectionCount, tasks }) {
  return (
    <aside className="panel inspector">
      <h2>Inspector</h2>
      <div className="metadata-grid">
        <label>Page</label>
        <strong>{page}</strong>
        <label>Imports</label>
        <strong>{importFiles.length}</strong>
        <label>Queue</label>
        <strong>{queueSelectionCount} / {queueItemCount} selected</strong>
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

function AudioPlayer({
  currentTrack,
  audioUrl,
  queue,
  currentIndex,
  queueOpen,
  setQueueOpen,
  onPlayTrack,
  onEnded,
  onSkipBack,
  onSkipForward,
  onFavorite,
  favoriteTrackIds,
  onDockChange,
  onClose,
}) {
  const audioRef = useRef(null);
  const dockRef = useRef(null);
  const coreRef = useRef(null);
  const pipWindowRef = useRef(null);
  const reopenPipAfterFullscreen = useRef(false);
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

  useEffect(() => {
    setPlaying(false);
    setCurrentTime(0);
  }, [audioUrl]);

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
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      audio.play();
    } else {
      audio.pause();
    }
  }

  function seek(event) {
    const audio = audioRef.current;
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
    pipWindow.document.body.style.overflow = "hidden";
    pipWindow.document.body.style.background = "transparent";
    pipWindow.document.documentElement.style.margin = "0";
    pipWindow.document.documentElement.style.padding = "0";
    pipWindow.document.documentElement.style.width = "100vw";
    pipWindow.document.documentElement.style.height = "100vh";
    pipWindow.document.documentElement.style.minHeight = "100vh";
    pipWindow.document.documentElement.style.overflow = "hidden";
    pipWindow.document.documentElement.style.background = "transparent";
    copyStylesToWindow(pipWindow);
    const container = pipWindow.document.createElement("div");
    container.className = `${document.querySelector("main")?.className || "app"} pip-root`;
    container.style.width = "100vw";
    container.style.height = "100vh";
    container.style.minHeight = "100vh";
    container.style.overflow = "hidden";
    container.style.display = "block";
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

  function surface({ popped = false } = {}) {
    const pipLayout = popped || fullscreenPlayer;
    return (
      <div
        className={`${popped ? "audio-player popped pip-player" : fullscreenPlayer ? "audio-player pip-player main-fullscreen-player" : "audio-player"}${fullscreenPlayer ? " is-window-fullscreen" : ""}${pipLayout && showUpNext ? " has-up-next" : ""}`}
        ref={popped ? null : dockRef}
        style={pipLayout && currentTrack?._coverUrl ? { "--fullscreen-art": `url(${currentTrack._coverUrl})` } : undefined}
      >
        <div className="player-core" ref={popped ? null : coreRef}>
          <div className="audio-header">
            <div className="player-art">{currentTrack?._coverUrl ? <img src={currentTrack._coverUrl} alt="" /> : <Music size={34} />}</div>
            <div className="audio-track-copy">
              {pipLayout && <span className="playing-from">Playing from library</span>}
              <strong>{currentTrack?.title || "Local player"}</strong>
              <small>{[currentTrack?._artist, currentTrack?._album].filter(Boolean).join(" / ") || currentTrack?.path || "Ready"}</small>
            </div>
            {pipLayout && showUpNext && (
              <div className="fullscreen-next">
                <div className="up-next-art">{nextTrack._coverUrl ? <img src={nextTrack._coverUrl} alt="" /> : <Music size={18} />}</div>
                <div>
                  <span>Up next</span>
                  <strong>{nextTrack.title}</strong>
                  <small>{[nextTrack._artist, nextTrack._album].filter(Boolean).join(" / ") || "Library queue"}</small>
                </div>
              </div>
            )}
            <div className="player-window-actions">
              {popped || fullscreenPlayer ? (
                <button className="row-icon-button" onClick={toggleFullscreenPlayer} title={fullscreenPlayer ? "Exit fullscreen" : "Fullscreen"}>
                  {fullscreenPlayer ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
                </button>
              ) : (
                <button className="row-icon-button" onClick={openPictureInPicture} disabled={queue.length === 0} title={queue.length === 0 ? "Queue is empty" : "Pop out"}>
                  <PictureInPicture2 size={14} />
                </button>
              )}
              <button className="row-icon-button" onClick={onClose} title="Close player">
                <X size={14} />
              </button>
            </div>
          </div>
          <div className="fullscreen-controls">
            <input className="player-progress" type="range" min="0" max={duration || 0} value={currentTime} onChange={seek} style={{ "--progress": `${progress}%` }} />
            <div className="player-controls">
              <button className="player-icon-button" onClick={() => setQueueOpen((value) => !value)} title="Queue">
                <Menu size={19} />
              </button>
              <button className="player-icon-button" onClick={onSkipBack} disabled={currentIndex <= 0} title="Previous">
                <SkipBack size={18} />
              </button>
              <button className="player-play-button" onClick={togglePlayback} title={playing ? "Pause" : "Play"}>
                {playing ? <Pause size={21} /> : <Play size={21} />}
              </button>
              <button className="player-icon-button" onClick={onSkipForward} disabled={currentIndex < 0 || currentIndex >= queue.length - 1} title="Next">
                <SkipForward size={18} />
              </button>
              <button className={isFavorite ? "player-icon-button active" : "player-icon-button"} onClick={() => onFavorite(currentTrack)} title="Favorite">
                <Heart size={19} />
              </button>
            </div>
          </div>
        </div>
        {queueOpen && (
          <div className="local-queue">
            {upcomingQueue.map((track, index) => (
              <button className={track.id === currentTrack?.id ? "active" : ""} key={`${track.id}:${index}`} onClick={() => onPlayTrack(track)}>
                <span>{track.track_number ? String(track.track_number).padStart(2, "0") : "#"}</span>
                <strong>{track.title}</strong>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <>
      {!pipContainer ? surface() : null}
      <audio
        ref={audioRef}
        autoPlay
        src={audioUrl}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)}
        onEnded={onEnded}
      />
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

function visibleTrayNotifications(notifications) {
  return notifications.filter((notification) => notification.title !== "Favorites synced");
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
    const artistName = item.artist || "Unknown Artist";
    const albumName = item.album || "Singles";
    if (!artistMap.has(artistName)) {
      artistMap.set(artistName, { name: artistName, albumMap: new Map() });
    }
    const artist = artistMap.get(artistName);
    if (!artist.albumMap.has(albumName)) {
      artist.albumMap.set(albumName, { name: albumName, request: null, tracks: [] });
    }
    const album = artist.albumMap.get(albumName);
    if (item.track) {
      album.tracks.push(item);
    } else {
      album.request = item;
    }
  });
  return [...artistMap.values()]
    .map((artist) => ({
      name: artist.name,
      albums: [...artist.albumMap.values()].sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function countAcousticStatuses(results) {
  return results.reduce(
    (counts, result) => {
      if (result.status === "matched") counts.matched += 1;
      else if (result.status === "changed") counts.changed += 1;
      else if (result.status === "unmatched") counts.unmatched += 1;
      else counts.failed += 1;
      return counts;
    },
    { matched: 0, changed: 0, unmatched: 0, failed: 0 },
  );
}

function acousticResultMeta(result) {
  if (result.status === "matched") return `Matched${result.score ? ` ${result.score}%` : ""}`;
  if (result.status === "changed") {
    const candidateTitle = result.candidate?.title || "different recording";
    return `Changed: ${candidateTitle}${result.score ? ` ${result.score}%` : ""}`;
  }
  if (result.status === "missing_file") return "Missing file";
  if (result.status === "error") return result.error || "Lookup failed";
  return "No match";
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
