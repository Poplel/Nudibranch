import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { createRoot } from "react-dom/client";
import {
  ArrowLeft,
  Bell,
  Info,
  Check,
  CheckCircle,
  ChevronDown,
  ChevronLeft,
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
  MonitorSpeaker,
  MoreHorizontal,
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
  Share2,
  Shield,
  Sparkles,
  Laptop,
  Globe,
  Smartphone,
  SkipBack,
  SkipForward,
  Shuffle,
  Repeat,
  Repeat1,
  Sun,
  Trash2,
  Upload,
  UserCheck,
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

// Nav order mirrors the iOS app's: the four things you reach for constantly first, then the
// management pages. "Discover" is not its own page any more — searching for music and tracking
// what you asked for are one flow, so both live under Wishlist (see WishlistWorkspace).
const navItems = [
  ["Home", House],
  ["Library", Music],
  ["Wishlist", Sparkles],
  ["Podcasts", Mic2],
  ["Playlists", FileAudio],
  ["Import/Add", HardDriveUpload],
  ["Approvals", UserCheck],
  ["Task Queue", ListChecks],
  ["Activity", Database],
  ["Tools", Wrench],
  ["Automations", Zap],
  ["Users", Users],
  ["Settings", Settings],
];

const pageDescriptions = {
  Home: "Your library at a glance.",
  Library: "Browse artists, albums, and tracks in the library.",
  "Import/Add": "Scan new files, add album records, and prepare them for review.",
  Wishlist: "Search for music and track what you have requested.",
  Approvals: "Review other users' wishlist requests.",
  "Task Queue": "Review requested changes before they run.",
  Playlists: "Create, import, and manage playlists.",
  Podcasts: "Subscribe to podcasts and play episodes.",
  Activity: "Track queued, running, completed, and failed work.",
  Tools: "Run tools to manage your library.",
  Automations: "Run tools or other actions automatically when triggered or on a schedule.",
  Users: "Manage users, passwords, API keys, and permissions.",
  Settings: "Manage settings.",
};

// Pages that render full-width with no Inspector aside.
const NO_INSPECTOR_PAGES = new Set(["Home", "Settings"]);

// ---------------------------------------------------------------------------
// Equalizer
//
// Ten-band graphic EQ on the standard ISO octave centres, deliberately identical to the iOS
// app's (Nudibranch/Sources/Player/Equalizer.swift) — same frequencies, same Q, same ±12 dB
// limit, same built-in curves — so a preset means the same thing on both clients.
//
// Settings are DEVICE-LOCAL (localStorage), matching iOS, where the EQ lives in UserDefaults and
// is not synced per-host. Like iOS there is no automatic preamp: the master limiter already in
// the audio graph catches the clipping that boosting bands would otherwise cause.
// ---------------------------------------------------------------------------
const EQ_FREQUENCIES = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
const EQ_Q = 1.41; // One octave of bandwidth per band, matching the octave spacing of the centres.
const EQ_GAIN_LIMIT = 12;
const EQ_STORAGE_KEY = "nudibranch:equalizer";

const EQ_BUILT_IN_PRESETS = [
  ["Flat", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
  ["Acoustic", [3, 2.5, 1.5, 0, 1, 1.5, 2.5, 2.5, 2, 1]],
  ["Bass Boost", [7, 6, 4.5, 2.5, 0, 0, 0, 0, 0, 0]],
  ["Bass Cut", [-7, -6, -4.5, -2.5, 0, 0, 0, 0, 0, 0]],
  ["Classical", [4, 3.5, 2.5, 1.5, -1, -1, 0, 2, 3, 3.5]],
  ["Electronic", [5, 4, 1.5, 0, -1.5, 1.5, 1, 2, 4, 4.5]],
  ["Hip-Hop", [6, 5, 2, 2.5, -1, -0.5, 1.5, 1.5, 2.5, 3]],
  ["Jazz", [4, 3, 1.5, 2, -1, -1, 0, 1.5, 3, 3.5]],
  ["Loudness", [6, 4.5, 1, 0, -2, 0, -1, -2, 4, 6]],
  ["Podcast", [-4, -3, 0, 2.5, 3.5, 3.5, 3, 1.5, 0, -1]],
  ["Pop", [-1.5, -1, 0, 2.5, 3.5, 3.5, 2, 0, -1, -1.5]],
  ["Rock", [5, 3.5, 1.5, -0.5, -1.5, 0, 2, 3.5, 4, 4.5]],
  ["Spoken Word", [-5, -4, -1, 2, 4, 4, 3.5, 2, 0.5, -1]],
  ["Treble Boost", [0, 0, 0, 0, 0, 1.5, 3, 4.5, 6, 7]],
  ["Vocal", [-3, -2.5, -1, 2, 4, 4, 3, 1.5, 0, -1]],
].map(([name, gains]) => ({ name, gains, builtIn: true }));

const EQ_FLAT_GAINS = EQ_FREQUENCIES.map(() => 0);

function eqBandLabel(frequency) {
  return frequency >= 1000 ? `${frequency / 1000}k` : String(frequency);
}

// Clamp/pad any stored or hand-edited curve to exactly EQ_FREQUENCIES.length valid values, so a
// preset written by an older build can never desynchronize the filter chain from the UI.
function normalizeEqGains(gains) {
  return EQ_FREQUENCIES.map((_, index) => {
    const value = Number(Array.isArray(gains) ? gains[index] : 0);
    if (!Number.isFinite(value)) return 0;
    return Math.min(Math.max(value, -EQ_GAIN_LIMIT), EQ_GAIN_LIMIT);
  });
}

const EQ_DEFAULT_SETTINGS = {
  enabled: false, // Off by default — the EQ must not colour anyone's audio until asked for.
  gains: EQ_FLAT_GAINS,
  appliesToPodcasts: false, // Matches iOS: spoken word is exempt unless you opt in.
  presetName: "Flat",
  customPresets: [],
};

function readStoredEqualizer() {
  try {
    const raw = JSON.parse(window.localStorage.getItem(EQ_STORAGE_KEY) || "null");
    if (!raw || typeof raw !== "object") return EQ_DEFAULT_SETTINGS;
    return {
      enabled: Boolean(raw.enabled),
      gains: normalizeEqGains(raw.gains),
      appliesToPodcasts: Boolean(raw.appliesToPodcasts),
      presetName: typeof raw.presetName === "string" ? raw.presetName : null,
      customPresets: Array.isArray(raw.customPresets)
        ? raw.customPresets
            .filter((preset) => preset && typeof preset.name === "string")
            .slice(0, 50)
            .map((preset) => ({ name: preset.name, gains: normalizeEqGains(preset.gains), builtIn: false }))
        : [],
    };
  } catch {
    return EQ_DEFAULT_SETTINGS;
  }
}

const OPEN_SOURCE_ATTRIBUTIONS = {
  Server: [
    ["FastAPI", "0.115.6", "MIT", "https://github.com/fastapi/fastapi"],
    ["Uvicorn", "0.34.0", "BSD-3-Clause", "https://github.com/encode/uvicorn"],
    ["SQLAlchemy", "2.0.36", "MIT", "https://github.com/sqlalchemy/sqlalchemy"],
    ["Pydantic Settings", "2.7.1", "MIT", "https://github.com/pydantic/pydantic-settings"],
    ["python-multipart", "0.0.20", "Apache-2.0", "https://github.com/Kludex/python-multipart"],
    ["HTTPX", "0.28.1", "BSD-3-Clause", "https://github.com/encode/httpx"],
    ["Hyper-h2", "4.1.0", "MIT", "https://github.com/python-hyper/h2"],
    ["PyJWT", "2.10.1", "MIT", "https://github.com/jpadilla/pyjwt"],
    ["cryptography", "44.0.0", "Apache-2.0 OR BSD-3-Clause", "https://github.com/pyca/cryptography"],
    ["bcrypt", "4.2.1", "Apache-2.0", "https://github.com/pyca/bcrypt"],
    ["RapidFuzz", "3.10.1", "MIT", "https://github.com/rapidfuzz/RapidFuzz"],
    ["croniter", "3.0.3", "MIT", "https://github.com/pallets-eco/croniter"],
    ["Mutagen", "1.47.0", "GPL-2.0-or-later", "https://github.com/quodlibet/mutagen"],
    ["yt-dlp", "2026.6.9", "Unlicense", "https://github.com/yt-dlp/yt-dlp"],
    ["SpotifyScraper", "2.1.5", "MIT", "https://github.com/aliakhtari78/spotifyscraper"],
    ["Beautiful Soup", "4.13.4", "MIT", "https://www.crummy.com/software/BeautifulSoup/"],
    ["feedparser", "6.0.11", "BSD-2-Clause", "https://github.com/kurtmckee/feedparser"],
  ],
  "Web client": [
    ["React", "19.2.6", "MIT", "https://react.dev/"],
    ["Lucide React", "1.14.0", "ISC", "https://lucide.dev/"],
    ["Vite", "8.0.12", "MIT", "https://vite.dev/"],
    ["Vite React plugin", "6.0.1", "MIT", "https://github.com/vitejs/vite-plugin-react"],
  ],
};

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
  // Hidden toggle for the "Stats for geeks" overlay: hold Shift and click the "Nudibranch" title 3 times.
  const diagTapRef = useRef({ count: 0, last: 0 });
  const handleBrandTap = (event) => {
    if (!event.shiftKey) { diagTapRef.current = { count: 0, last: 0 }; return; }
    event.preventDefault();
    const now = Date.now();
    const count = now - diagTapRef.current.last < 1500 ? diagTapRef.current.count + 1 : 1;
    diagTapRef.current = { count, last: now };
    if (count >= 3) {
      diagTapRef.current = { count: 0, last: 0 };
      togglePlayerDiagnostics(!playerDiagnostics);
    }
  };
  const [page, setPage] = useState("Library");
  const [albumDetail, setAlbumDetail] = useState(null);
  const [artistDetail, setArtistDetail] = useState(null);
  const [homeVersion, setHomeVersion] = useState(0);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [importUploadProgress, setImportUploadProgress] = useState(null);
  const [pinnedAlbumIds, setPinnedAlbumIds] = useState(() => new Set());
  const [pinnedArtistIds, setPinnedArtistIds] = useState(() => new Set());
  const [pinnedPodcastIds, setPinnedPodcastIds] = useState(() => new Set());
  const [podcastOpenRequest, setPodcastOpenRequest] = useState(null);
  const [dark, setDark] = useState(initialAppearance.dark);
  const [trayOpen, setTrayOpen] = useState(false);
  // Screen coordinates for the notification tray's portal — computed from whichever bell button
  // was actually clicked (see openNotificationTray), since the tray now renders via createPortal
  // into document.body rather than as a CSS-positioned descendant of the button (see the docked-
  // player clipping note on the tray's render site below).
  const [trayAnchor, setTrayAnchor] = useState(null);
  const [toast, setToast] = useState(null);
  const [accentColor, setAccentColor] = useState(initialAppearance.accentColor);
  const [backgroundTint, setBackgroundTint] = useState(initialAppearance.backgroundTint);
  const [crossfadeDuration, setCrossfadeDuration] = useState(0.5);
  // Device-local, like iOS — the EQ is a property of these speakers/headphones, not the account.
  const [equalizer, setEqualizer] = useState(readStoredEqualizer);
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  // The Inspector holds actions no other surface offers on several pages (Create playlist, Add
  // selected to task queue, the whole Import/Add scan+upload flow) — it can't just be hidden on
  // mobile the way the sidebar is. Presented as a bottom sheet there instead of a fixed aside.
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);
  useEffect(() => { setMobileInspectorOpen(false); }, [page]);
  const [library, setLibrary] = useState([]);
  const [importFiles, setImportFiles] = useState([]);
  const [importSeedDownloads, setImportSeedDownloads] = useState([]);
  const addImportAlbumsRef = useRef(null);
  const playbackControlRef = useRef(null);
  // Portal target for the notification tray (see openNotificationTray/topbarUtilityActions):
  // must be the themed root, not document.body — the theme's CSS custom properties
  // (--panel-strong etc.) are set via inline style on this <main> element, and a portal target
  // outside it can't see them, which is why a first attempt at document.body rendered a
  // correctly-positioned but entirely transparent tray.
  const appRootRef = useRef(null);
  const importUploadXhrRef = useRef(null); // in-flight import upload, so it can be canceled
  const unshuffledQueueRef = useRef(null); // snapshot of queue order before shuffle, to revert
  const currentSessionIdRef = useRef(null);
  const remoteExecRef = useRef(null);
  const lastLibraryPollRef = useRef(0); // throttle the heavy /library/tree poll (see interval below)
  const lastRecordedPlayRef = useRef(null);
  const lastEpisodeProgressRef = useRef(null);
  const commandPollingRef = useRef(false);
  const commandPollNowRef = useRef(null);
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
  const [approvalsInspectorActions, setApprovalsInspectorActions] = useState(null);
  const [playlistInspectorActions, setPlaylistInspectorActions] = useState(null);
  const [podcastInspectorActions, setPodcastInspectorActions] = useState(null);
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
  // ⚠ Declared up here with the other player state, not beside its poll further down: `playerDocked`
  // reads `activeRemoteSession`, and a `const` used before its declaration is a temporal-dead-zone
  // crash at runtime that the bundler will not warn about.
  const [remoteSessions, setRemoteSessions] = useState([]);
  const remoteConfirmRef = useRef(null);
  /// When each session's reported position was last *observed to change*, so the clock can be
  /// advanced locally between polls instead of stepping once per poll.
  const remoteAnchorsRef = useRef(new Map());
  /// What this tab has asserted about a session ahead of the far end confirming it, so a report
  /// older than the assertion cannot overwrite it. See `refreshRemoteSessions`.
  const remoteAssertionsRef = useRef(new Map());
  /// The queue a remote session published, so the player lists it exactly as it lists a local one.
  const [remoteQueue, setRemoteQueue] = useState([]);
  const [remoteQueueSession, setRemoteQueueSession] = useState(null);
  /// Bumped on a timer purely to force a re-render so interpolated values are re-read.
  const [remoteClockTick, setRemoteClockTick] = useState(0);
  /// How many surfaces want live remote state. While any do, the poll runs fast.
  const [remoteViewers, setRemoteViewers] = useState(0);
  /// The session worth showing a card for: recently described, and actually playing something.
  /// `presence === "live"` only — a `reachable` session can still be handed a queue, but drawing a
  /// now-playing card for one would assert something the server has said it no longer knows.
  const activeRemoteSession = remoteSessions.find(
    (r) => !r.current && r.presence === "live" && (r.status === "playing" || r.status === "paused"),
  );
  const [playerPopped, setPlayerPopped] = useState(false);
  const [playerDockHeight, setPlayerDockHeight] = useState(0);
  const [playerToastHeight, setPlayerToastHeight] = useState(0);
  const [queueOpen, setQueueOpen] = useState(false);
  const [appearanceReady, setAppearanceReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
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
  // Reference match first: a queue-row click hands loadPlayerTrack the exact element it clicked
  // (upcomingQueue is a slice of playerQueue, so it's the same object), and shuffle/reorder spread
  // the array without cloning its elements — so this always finds the position actually playing
  // even when the SAME track appears twice in the queue. Falling back to an id match only covers
  // the rare case where currentTrack isn't literally one of playerQueue's own elements. Matching by
  // id alone (the old behaviour) always resolved a duplicate to its FIRST occurrence, so jumping to
  // the second copy of a repeated track snapped the highlighted row — and "next"/"previous", which
  // are index-based — right back to the first one.
  const currentTrackIndex = currentTrack
    ? (() => {
        const refIndex = playerQueue.indexOf(currentTrack);
        return refIndex >= 0 ? refIndex : playerQueue.findIndex((track) => track.id === currentTrack.id);
      })()
    : -1;
  // The remote dock occupies the same slot and the same height variables, so anything keyed on
  // "a player is docked" has to count it too — otherwise content sits under it.
  const playerDocked = (playerOpen && !playerPopped) || Boolean(!playerOpen && activeRemoteSession);
  const appearanceVars = useMemo(() => buildAppearanceVars(dark, accentColor, backgroundTint), [dark, accentColor, backgroundTint]);
  const nextAudioUrl = useMemo(() => {
    const next = playerQueue[currentTrackIndex + 1];
    if (!next?.id || !token) return null;
    return trackStreamUrl(next, token);
  }, [playerQueue, currentTrackIndex, token]);

  const lyricsUrl = useMemo(() => {
    const track = playerQueue[currentTrackIndex];
    if (!track?.id || !token) return null;
    // Podcast episodes have no LRC lyrics.
    if (track._streamPath) return null;
    return `${API_BASE}/library/tracks/${track.id}/lyrics?api_key=${encodeURIComponent(token)}`;
  }, [playerQueue, currentTrackIndex, token]);

  // Podcast resume: when an episode with a saved position becomes current, seek to it once the
  // audio is seekable (retrying until controlRef.seek succeeds after metadata loads).
  useEffect(() => {
    const track = currentTrack;
    if (!track || track._kind !== "episode" || !(track._resumeMs > 0)) return undefined;
    let cancelled = false;
    let tries = 0;
    const target = track._resumeMs / 1000;
    const attempt = () => {
      if (cancelled) return;
      tries += 1;
      const ok = playbackControlRef.current?.seek?.(target);
      if (!ok && tries < 40) setTimeout(attempt, 250);
    };
    const timer = setTimeout(attempt, 300);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [currentTrack?.id]);

  useEffect(() => {
    const handlePointerDown = (event) => {
      // .closest, not trayRef.current.contains: the notification bell now renders in more than
      // one place (desktop topbar, inside the docked player, the mobile More sheet), and only
      // one of those DOM nodes can ever hold a single ref — a stale ref pointing at whichever
      // copy last mounted would misjudge every other copy's own click as "outside".
      // The tray itself renders via a portal into document.body (see openNotificationTray), so
      // it is no longer a DOM descendant of ".notification-anchor" — a click inside it (e.g.
      // "Clear") has to be checked separately or it always reads as an outside click.
      if (!event.target.closest?.(".notification-anchor") && !event.target.closest?.(".notification-tray")) {
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

  // EQ settings are device-local (see EQ_STORAGE_KEY) — persist them straight to localStorage
  // rather than to /me, so one account can have a different curve on each machine.
  useEffect(() => {
    try {
      window.localStorage.setItem(EQ_STORAGE_KEY, JSON.stringify(equalizer));
    } catch { /* private mode / quota — the EQ still works for this session */ }
  }, [equalizer]);

  // Initial data load — keyed on token ONLY. refreshAll() calls setUser(me), which
  // changes user?.id / the permission key; when this effect also depended on those
  // it re-fired refreshAll 2–3× at startup (~35 parallel requests queuing on the
  // browser's 6-connection limit and SQLite's write lock → 4–18s page loads).
  useEffect(() => {
    if (!token) return;
    refreshAll();
  }, [token]);

  // Polling interval — dep changes here only re-create the interval (no immediate fetch).
  useEffect(() => {
    if (!token) return;
    const interval = window.setInterval(() => {
      // The library tree is ~470 KB and near-static (only changes on import/edit,
      // which trigger their own refreshLibrary). Polling it every 2.5–10s saturated
      // the WAN tunnel and starved audio streaming, so throttle it to ~60s here;
      // mutations and manual Refresh still update it immediately.
      if (hasPermission(user, "library:view") && Date.now() - lastLibraryPollRef.current >= 60000) {
        lastLibraryPollRef.current = Date.now();
        refreshLibrary();
      }
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
        // `client` so a browser identifies itself in the device picker before it has played anything.
        body: JSON.stringify({ username, password, device_label: getDeviceLabel(), client: "web" }),
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
      lastLibraryPollRef.current = Date.now(); // count this fetch toward the 60s poll throttle
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

  // Changing your own password re-authenticates: /me/pin requires the current one, so a borrowed
  // session token cannot be used to lock the real owner out of their account.
  async function updateOwnPin(currentPassword, password) {
    setLoading(true);
    try {
      const updated = await api("/me/pin", {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, password }),
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

  // Persist the web home-screen arrangement. Separate from the iOS app's `home_layout` on
  // purpose — the two clients lay Home out differently, so each keeps its own order.
  // Best-effort: the reorder has already been applied on screen, and a failed save just means
  // the arrangement isn't remembered, which isn't worth interrupting the user over.
  async function saveHomeLayoutWeb(rows) {
    try {
      const updated = await api("/me/home-layout-web", {
        method: "PUT",
        body: JSON.stringify({ layout: { rows } }),
      });
      setUser(updated);
      setUsers((current) => upsertUser(current, updated));
    } catch { /* keep the on-screen order for this session */ }
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

  async function openNotificationTray(event) {
    const nextOpen = !trayOpen;
    if (nextOpen && event?.currentTarget) {
      const rect = event.currentTarget.getBoundingClientRect();
      setTrayAnchor({ top: rect.bottom + 8, right: window.innerWidth - rect.right });
    }
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
    api("/me/pinned-podcasts").then((rows) => setPinnedPodcastIds(new Set((rows || []).map((r) => r.podcast_id)))).catch(() => {});
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

  async function togglePodcastPin(podcast) {
    const pinned = pinnedPodcastIds.has(podcast.id);
    try {
      const rows = pinned
        ? await api(`/me/pinned-podcasts/${encodeURIComponent(podcast.id)}`, { method: "DELETE" })
        : await api("/me/pinned-podcasts", { method: "POST", body: JSON.stringify({ podcast_id: podcast.id }) });
      setPinnedPodcastIds(new Set((rows || []).map((row) => row.podcast_id)));
      setHomeVersion((version) => version + 1);
    } catch (pinError) {
      notify("Pin failed", pinError.message, "ui_error");
    }
  }

  function openPodcastDetail(podcast) {
    setPodcastOpenRequest({ id: podcast.id, nonce: Date.now() });
    setPage("Podcasts");
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

  // Apply one of the /cover-candidates URLs. The SERVER downloads it and stores the bytes in the
  // library folder; cover_path is a filesystem path and must never be set to a URL by a client.
  // (It is no longer an editable metadata field either, so the old
  // `apply({ cover_path: "https://..." })` route is gone on purpose.)
  async function setCoverFromUrl(kind, id, url) {
    if (!url) return false;
    setLoading(true);
    try {
      await api(`/library/${kind}/${id}/cover-from-url`, {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      coverCacheBust = Date.now();
      await refreshLibrary();
      setToast({ title: kind === "albums" ? "Cover art updated" : "Artist art updated", body: "Saved to the library folder." });
      return true;
    } catch (coverError) {
      notify("Couldn't save that image", coverError.message, "ui_error");
      return false;
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
    // opts.localOnly is set only by executeRemoteCommand, when THIS session is itself the
    // target of a received "play" command — forwarding again there would bounce the request
    // to whatever session happens to look active instead of honoring the one that was told to
    // play. Every other caller (a row/album/artist/playlist/Home Play click) wants exactly the
    // opposite: standing at an idle tab with music on another device, "Play" obviously means
    // "play it there", not "start a second, silent player here" — the same reasoning
    // forwardQueueAddition already applies to Add to Queue / Play Next.
    if (!opts.localOnly && forwardPlayToRemote(queue, wantShuffle)) return;
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

  // Play next for a whole collection: same resolution as Play, inserted after the current track
  // instead of replacing the queue. Resolved only when the row is chosen.
  async function playAlbumNext(album) {
    try {
      const tracks = await loadAlbumPlayables(album);
      if (tracks.length === 0) { notify("Queue", "No tracks found for this album.", "ui_error"); return; }
      playTracksNext(tracks);
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

  async function playArtistNext(artist) {
    try {
      const data = await api(`/library/albums?artist_id=${encodeURIComponent(artist.id)}&page_size=500`);
      let tracks = [];
      for (const al of data?.items || []) tracks = tracks.concat(await loadAlbumPlayables(al));
      if (tracks.length === 0) { notify("Queue", "No tracks found for this artist.", "ui_error"); return; }
      playTracksNext(tracks);
    } catch (error) {
      notify("Queue failed", error.message, "ui_error");
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

  /// `controlRef.seek` returns false until the media element is actually seekable, so a seek that
  /// arrives while a track is still loading has to be retried rather than dropped — the same shape as
  /// the podcast resume retry elsewhere in this file.
  function seekWithRetry(seconds, attempt = 0) {
    const ctl = playbackControlRef.current;
    if (ctl?.seek?.(seconds)) return undefined;
    if (attempt >= 20) return undefined;
    setTimeout(() => seekWithRetry(seconds, attempt + 1), 250);
    return undefined;
  }

  /// Adopt a queue handed over by another of this account's sessions.
  ///
  /// ⚠ Returns "retry" for a transient failure so the poll loop leaves the command PENDING. A
  /// handoff carries a queue rather than an instruction, so acking a network blip would drop
  /// someone's playback with nothing to recover from. It is bounded by the server's five-minute
  /// expiry, after which the fetch 410s — a permanent outcome, which acks.
  async function adoptHandoff(handoffId) {
    if (!handoffId) return undefined;
    let handoff;
    try {
      handoff = await api(`/player/handoffs/${encodeURIComponent(handoffId)}`);
    } catch (error) {
      // 404/410 are permanent (gone, expired, already taken); anything else is worth another poll.
      const permanent = /\b(404|410)\b/.test(String(error?.message || ""));
      return permanent ? undefined : "retry";
    }
    const snapshot = handoff?.snapshot;
    if (!snapshot?.items?.length) return undefined;

    const tracks = await resolveSnapshotItems(snapshot.items);
    if (tracks.length === 0) {
      // Leave the local queue exactly as it was — a handoff that cannot play here must not also
      // destroy what this device already had.
      await api(`/player/handoffs/${encodeURIComponent(handoffId)}/rejected`, {
        method: "POST",
        body: JSON.stringify({ reason: "nothing_resolved" }),
      }).catch(() => {});
      notify("Playback not moved", "Nothing in that queue is available here.", "ui_notice");
      return undefined;
    }

    // Start where the sender was, or at the first item after it that resolved here.
    const wantedId = snapshot.items[snapshot.current_index]?.id;
    let startIndex = tracks.findIndex((t) => (t._episodeId || t.id) === wantedId);
    if (startIndex < 0) {
      const laterIds = new Set(snapshot.items.slice(snapshot.current_index + 1).map((i) => i.id));
      startIndex = tracks.findIndex((t) => laterIds.has(t._episodeId || t.id));
    }
    if (startIndex < 0) startIndex = 0;

    setPlayerQueue(tracks);
    setPlayerOpen(true);
    // An adopted queue arrives already in playback order and its pre-shuffle order stayed on the
    // sending device, so there is nothing to restore. setShuffleState's off-branch handles a null
    // snapshot by leaving the queue as it is, which is the right degradation.
    unshuffledQueueRef.current = null;
    setShuffleState(Boolean(snapshot.shuffle));
    setRepeat(["off", "one", "all"].includes(snapshot.repeat) ? snapshot.repeat : "off");
    await loadPlayerTrack(tracks[startIndex]);
    if (snapshot.position_seconds > 0) seekWithRetry(Math.round(snapshot.position_seconds));
    // The SERVER decides whether to start playing — it decays autoplay for a handoff collected late,
    // so audio never starts on a device the user has since walked away from.
    if (handoff.autoplay_effective === false) {
      setTimeout(() => playbackControlRef.current?.pause?.(), 0);
    }
    await api(`/player/handoffs/${encodeURIComponent(handoffId)}/adopted`, { method: "POST" }).catch(() => {});
    setToast({ title: "Playback moved here", body: `From ${handoff.from_device_label || "another device"}.` });
    return undefined;
  }

  /// Resolve a snapshot's ids into playable rows, PRESERVING SNAPSHOT ORDER — the order is part of
  /// what was handed over, and neither lookup below returns it. Unresolvable items are skipped
  /// silently, as playlist playback already does.
  async function resolveSnapshotItems(items) {
    const byId = new Map();

    // Tracks come from the in-memory library tree; it is already loaded and holds everything the
    // player needs, so this costs no requests.
    const wantedTracks = new Set(items.filter((i) => i.type === "track").map((i) => i.id));
    if (wantedTracks.size > 0) {
      for (const artist of library || []) {
        for (const album of artist.albums || []) {
          for (const track of album.tracks || []) {
            if (wantedTracks.has(track.id)) byId.set(track.id, hydrateTrack(track, artist, album));
          }
        }
      }
    }

    // Episodes are grouped by podcast: one request per SHOW, not one per episode.
    const wantedByPodcast = new Map();
    for (const item of items) {
      if (item.type !== "episode" || !item.podcast_id) continue;
      if (!wantedByPodcast.has(item.podcast_id)) wantedByPodcast.set(item.podcast_id, new Set());
      wantedByPodcast.get(item.podcast_id).add(item.id);
    }
    if (wantedByPodcast.size > 0) {
      // An episode can only be made playable from its podcast (cover, author, stream path), so the
      // subscription list is fetched once rather than per show.
      let podcasts = [];
      try {
        podcasts = (await api("/podcasts")) || [];
      } catch {
        podcasts = [];
      }
      for (const [podcastId, wanted] of wantedByPodcast) {
        const podcast = podcasts.find((p) => p.id === podcastId);
        if (!podcast) continue;
        try {
          const data = await api(`/podcasts/${encodeURIComponent(podcastId)}/episodes?page=1&page_size=500`);
          for (const episode of data?.items || []) {
            if (wanted.has(episode.id)) {
              byId.set(episode.id, episodeToPlayable(episode, podcast, token));
            }
          }
        } catch {
          /* skip this show; its items simply won't resolve */
        }
      }
    }

    return items.map((item) => byId.get(item.id)).filter(Boolean);
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
    // A mode-only command. Loop and shuffle were already applied above; before this action existed
    // there was no way to change them on another device without also sending it a transport action
    // nobody asked for.
    if (action === "state") return undefined;
    if (action === "seek") return seekWithRetry(Number(cmd.position_seconds) || 0);
    if (action === "adopt_handoff") return adoptHandoff(cmd.target_id);
    // Another of this account's sessions clicked a row in what it sees as OUR published queue
    // (the web queue panel's remote-jump, and the app's own queue view). The index is into the
    // full local queue this session actually holds, not into any windowed copy — loadPlayerTrack
    // re-derives currentTrackIndex from the track id, same as a local queue-row click does.
    if (action === "jump" && typeof cmd.queue_index === "number") {
      const track = playerQueue[cmd.queue_index];
      return track ? loadPlayerTrack(track) : undefined;
    }
    if (isPlay) {
      let tracks = resolvePlayableFromLibrary(cmd.target_type, cmd.target_id);
      if (tracks.length === 0 && cmd.target_type === "playlist") tracks = await resolvePlaylistTracks(cmd.target_id);
      if (tracks.length === 0) {
        notify("Remote playback", `Could not find ${cmd.target_label || "the requested item"} in the library.`, "ui_error");
        return undefined;
      }
      // localOnly: this session IS the target the command named — it must play locally, not
      // re-forward to whatever session refreshRemoteSessions happens to consider "active" right
      // now (which could even be this same command bouncing back).
      return playTracks(tracks, { shuffle: cmd.shuffle != null ? Boolean(cmd.shuffle) : undefined, localOnly: true });
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

  // What this account's OTHER sessions are playing, for the idle dock and the device picker.
  //
  // Polled only when this tab is NOT the one playing and is actually visible (§7): the answer is
  // only ever shown in those conditions. A remote action applies optimistically and is confirmed by
  // ONE delayed read rather than by polling faster — the far end has to receive, act and report
  // before its row changes, so an immediate re-read would just show the old state.

  const refreshRemoteSessions = useCallback(async () => {
    try {
      const fresh = (await api("/player/sessions")) || [];
      // ⚠ Re-anchor only when a report actually MOVED. Re-anchoring every poll would reset the
      // interpolation to a stale value each time and reproduce exactly the stutter it removes — the
      // far end only reports every 15s, so most polls return the same position and must be left to
      // keep running.
      const now = Date.now();
      // ⚠ Keep this tab's own assertions on top of any report that PREDATES them. The far end
      // collects commands on its own schedule, so for a beat after a click the server still
      // describes the state before it — and letting that land is what snapped a control back and
      // then forward again. A report generated before we acted cannot describe the result of
      // acting, so it is not allowed to speak about the fields we changed.
      const reconciled = fresh.map((row) => {
        const assertion = remoteAssertionsRef.current.get(row.session_id);
        if (!assertion) return row;
        // ⚠ Compared against the far end's OWN previous `reported_at`, never against a local clock.
        // `reported_at` comes from the server; `Date.now()` does not. Any skew between them either
        // pins the assertion for its whole lifetime — the scrubber sticking where you dragged it
        // while every other device moved on — or retires it instantly and brings the rubberband
        // back. The local clock is only good enough for the lifetime backstop.
        if (row.reported_at !== assertion.observedReport || now - assertion.at > 12000) {
          remoteAssertionsRef.current.delete(row.session_id);
          return row;
        }
        return { ...row, ...assertion.fields };
      });
      const anchors = remoteAnchorsRef.current;
      for (const row of reconciled) {
        const reported = row.position_seconds || 0;
        const previous = anchors.get(row.session_id);
        if (!previous || previous.reported !== reported) {
          anchors.set(row.session_id, { reported, at: now });
        }
      }
      for (const key of [...anchors.keys()]) {
        if (!reconciled.some((r) => r.session_id === key)) anchors.delete(key);
      }
      setRemoteSessions(reconciled);
    } catch {
      /* ambient information; a failure to describe another device is not worth interrupting anyone */
    }
  }, [api]);

  /// Where a session is *now*, advanced from its last report by the wall clock.
  ///
  /// Interpolation, not truth: a poll can only ever deliver a position already stale by the round
  /// trip. Advancing locally at 1x and re-anchoring on each new report is the trick game clients use
  /// for other players' positions — it can be a second out either way and it looks right, which is
  /// what a progress bar is for. Only a playing session advances.
  function interpolatedRemotePosition(row) {
    const reported = row?.position_seconds || 0;
    if (!row || row.status !== "playing") return reported;
    const anchor = remoteAnchorsRef.current.get(row.session_id);
    if (!anchor) return reported;
    const advanced = anchor.reported + (Date.now() - anchor.at) / 1000;
    if (row.duration_seconds > 0) return Math.min(advanced, row.duration_seconds);
    return Math.max(advanced, reported);
  }

  // The queue a remote session published. Fetched once per session rather than per poll: it changes
  // only when that session's queue does, and `enqueueOnRemote` clears the marker to force a re-read.
  useEffect(() => {
    const sessionId = activeRemoteSession?.session_id;
    if (!token || !sessionId) { setRemoteQueue([]); return undefined; }
    if (remoteQueueSession === sessionId) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const snapshot = await api(`/player/sessions/${encodeURIComponent(sessionId)}/queue?resolve=true`);
        if (cancelled) return;
        // Shaped like a local queue entry so the player's list renders it without branching.
        const index = Math.max(0, snapshot?.current_index || 0);
        setRemoteQueue((snapshot?.items || []).map((item, at) => ({
          id: item.id,
          title: item.title || "Unknown track",
          _artist: item.artist || "",
          _album: "",
          _albumId: item.album_id || null,
          _kind: item.type === "episode" ? "episode" : "track",
          _remoteIndex: at,
          _remoteCurrent: at === index,
        })));
        setRemoteQueueSession(sessionId);
      } catch {
        if (!cancelled) { setRemoteQueue([]); setRemoteQueueSession(sessionId); }
      }
    })();
    return () => { cancelled = true; };
  }, [token, activeRemoteSession?.session_id, remoteQueueSession, api]);

  // Only ticks while something is actually showing live remote state, so an idle tab does no work.
  useEffect(() => {
    if (remoteViewers === 0) return undefined;
    const timer = setInterval(() => setRemoteClockTick((t) => t + 1), 500);
    return () => clearInterval(timer);
  }, [remoteViewers]);

  // ⚠ Deliberately NOT gated on this tab being idle. Playing something here is no reason to lose
  // the ability to see and drive what is playing elsewhere. The RATE adapts instead: two seconds
  // while a remote surface is open, ten otherwise.
  useEffect(() => {
    if (!token || !user?.id) return undefined;
    if (document.visibilityState === "visible") refreshRemoteSessions();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") refreshRemoteSessions();
    }, remoteViewers > 0 ? 2000 : 10000);
    return () => clearInterval(timer);
  }, [token, user?.id, remoteViewers, refreshRemoteSessions]);

  /// Read back quickly for a short window after acting, rather than once at a guessed delay.
  ///
  /// How long the far end takes to collect a command, act, and report is not knowable from here, so
  /// a single delayed read usually landed too early: it cost a request and told us nothing. The
  /// burst stops as soon as every assertion has been answered.
  function confirmRemoteSoon() {
    clearInterval(remoteConfirmRef.current);
    const started = Date.now();
    remoteConfirmRef.current = setInterval(async () => {
      await refreshRemoteSessions();
      if (remoteAssertionsRef.current.size === 0 || Date.now() - started > 6000) {
        clearInterval(remoteConfirmRef.current);
      }
    }, 500);
  }

  /// Assert the result of a click locally and hold it until the far end confirms.
  function assertRemote(sessionId, fields) {
    const observedReport = remoteSessions.find((row) => row.session_id === sessionId)?.reported_at ?? null;
    remoteAssertionsRef.current.set(sessionId, { at: Date.now(), observedReport, fields });
    setRemoteSessions((rows) =>
      rows.map((row) => (row.session_id === sessionId ? { ...row, ...fields } : row)));
    const anchor = fields.position_seconds;
    if (anchor !== undefined) {
      remoteAnchorsRef.current.set(sessionId, { reported: anchor, at: Date.now() });
    }
  }

  async function remoteCommand(sessionId, action, positionSeconds) {
    const predicted = {
      pause: { status: "paused" },
      resume: { status: "playing" },
      play: { status: "playing" },
      stop: { status: "stopped" },
      // next/previous change the TRACK, and only the queue knows to what. Predicting a title we
      // cannot derive would put the wrong one on screen, so these wait for the report.
      seek: positionSeconds === undefined ? null : { position_seconds: positionSeconds },
    }[action];
    if (predicted) assertRemote(sessionId, predicted);
    try {
      await api("/player/commands", {
        method: "POST",
        body: JSON.stringify({
          action,
          device_id: sessionId,
          ...(positionSeconds === undefined ? {} : { position_seconds: Math.round(positionSeconds) }),
        }),
      });
      confirmRemoteSoon();
    } catch {
      notify("Remote playback", "That device didn't accept the command.", "ui_error");
    }
  }

  /// Jump to a specific track in another session's queue — the remote counterpart of clicking a
  /// queue row locally. No optimistic title prediction (like next/previous, not pause/resume):
  /// which track that index resolves to is only known once the far end reports back.
  async function remoteQueueJump(sessionId, queueIndex) {
    try {
      await api("/player/commands", {
        method: "POST",
        body: JSON.stringify({ action: "jump", device_id: sessionId, queue_index: queueIndex }),
      });
      // The published queue copy's current_index is now stale; force a re-fetch once the far end
      // has had a beat to collect the command (up to its 4s poll interval), act, report the new
      // track (which flags its queue hash stale) and re-publish — so the queue panel's highlighted
      // row catches up. Two staggered attempts cover both a fast and a worst-case poll cycle.
      setTimeout(() => setRemoteQueueSession(null), 1500);
      setTimeout(() => setRemoteQueueSession(null), 5000);
      confirmRemoteSoon();
    } catch {
      notify("Remote playback", "That device didn't accept the command.", "ui_error");
    }
  }

  /// Change shuffle or repeat on another session without also sending it a transport action.
  ///
  /// ⚠ Only ever send loop/shuffle with `state`: both clients apply those two unconditionally on
  /// every command they receive, so putting them on a seek would change the far end's mode as a
  /// side effect of moving the scrubber.
  async function remoteMode(sessionId, { loop, shuffle: shuffleOn } = {}) {
    const fields = {};
    if (loop !== undefined) fields.repeat = loop;
    if (shuffleOn !== undefined) fields.shuffle = shuffleOn;
    assertRemote(sessionId, fields);
    try {
      await api("/player/commands", {
        method: "POST",
        body: JSON.stringify({ action: "state", device_id: sessionId, loop, shuffle: shuffleOn }),
      });
      confirmRemoteSoon();
    } catch {
      notify("Remote playback", "That device didn't accept the command.", "ui_error");
    }
  }

  /// Add tracks to the queue of the session that is actually playing, rather than starting a second
  /// player in this tab. The far end folds them in with its own Play Next / append, so the action
  /// means the same thing whichever device holds the audio.
  async function enqueueOnRemote(sessionId, items, next) {
    if (!items.length) return;
    try {
      await api("/player/enqueue", {
        method: "POST",
        body: JSON.stringify({
          to_session_id: sessionId,
          mode: next ? "next" : "end",
          snapshot: {
            version: 1,
            items,
            current_index: 0,
            position_seconds: 0,
            playing: false,
            shuffle: false,
            repeat: "off",
          },
        }),
      });
      setRemoteQueueSession(null);
      confirmRemoteSoon();
    } catch {
      notify("Remote playback", "That device didn't accept the queue change.", "ui_error");
    }
  }

  /// A cheap fingerprint of the queue and where we are in it.
  ///
  /// ⚠ Covers ORDER and position-in-queue, deliberately not elapsed seconds: the stored copy exists
  /// so another device can move this queue, and only needs re-sending when the queue itself changes.
  /// Including the playhead would re-upload it on every heartbeat, which is what the hash avoids.
  function queueHash() {
    const parts = [String(currentTrackIndex), String(shuffle), repeat];
    for (const item of playerQueue) parts.push(item._episodeId || item.id);
    const joined = parts.join("|");
    let hash = 0;
    for (let i = 0; i < joined.length; i++) {
      hash = ((hash << 5) - hash + joined.charCodeAt(i)) | 0;
    }
    return String(hash);
  }

  /// Publish this tab's queue so another device can move it. The local queue stays authoritative —
  /// nothing ever reads this copy back to play from.
  function publishQueue() {
    if (playerQueue.length === 0) return;
    api("/player/queue", {
      method: "POST",
      body: JSON.stringify({ hash: queueHash(), snapshot: buildQueueSnapshot() }),
    }).catch(() => {});
  }

  /// The queue as ids, windowed. "Play library" really does page an entire library in here while the
  /// server caps a transfer, so send what surrounds the current position and re-base the index.
  function buildQueueSnapshot() {
    const MAX = 500;
    const LOOK_BACK = 50;
    const anchorIndex = Math.min(Math.max(currentTrackIndex, 0), Math.max(0, playerQueue.length - 1));
    const start = Math.max(0, Math.min(anchorIndex - LOOK_BACK, Math.max(0, playerQueue.length - MAX)));
    const slice = playerQueue.slice(start, start + MAX);
    return {
      version: 1,
      items: slice.map((item) => ({
        type: item._kind === "episode" ? "episode" : "track",
        id: item._episodeId || item.id,
        podcast_id: item._podcastId || null,
      })),
      current_index: anchorIndex - start,
      position_seconds: Math.round(playbackControlRef.current?.position?.() || 0),
      playing: playbackControlRef.current?.isPlaying?.() || false,
      shuffle,
      repeat,
    };
  }

  /// Hand this tab's queue to another session.
  ///
  /// ⚠ Local playback stops only AFTER the server accepts. Every refusal arrives in this request, so
  /// a failed transfer costs nothing — stopping first would throw the queue away to find out.
  async function transferPlaybackTo(sessionId, deviceLabel) {
    if (playerQueue.length === 0) return;
    // ⚠ One snapshot builder, not two. This used to carry its own copy of the windowing arithmetic,
    // which is exactly how the two drifted: publish kept the position and transfer sent a zero.
    const snapshot = buildQueueSnapshot();
    try {
      const result = await api("/player/transfer", {
        method: "POST",
        body: JSON.stringify({ to_session_id: sessionId, autoplay: snapshot.playing, snapshot }),
      });
      // Playback moved: stop rather than pause, so this tab becomes the idle remote showing the
      // device that now owns the queue.
      reportPlayerStatus(currentTrack, "stopped");
      playbackControlRef.current?.stop?.();
      setPlayerQueue([]);
      setCurrentTrack(null);
      // ⚠ `playerOpen` deliberately STAYS true. Closing it unmounts the player, which throws away
      // whatever form it was in — docked, fullscreen or popped out — at the exact moment the user
      // moved the music. The view should follow the playback, not the device: the same component
      // stays mounted and re-renders against the session that now holds the queue.
      setPlayerOpen(true);
      setToast({ title: "Playback moved", body: `Continues on ${result?.to_device_label || deviceLabel || "that device"}.` });
      refreshRemoteSessions();
    } catch (error) {
      // The server refuses an unreachable target before anything moves, so say which device and when
      // it was last seen rather than reporting a failure the user cannot act on.
      const detail = parseTransferError(error);
      notify("Playback not moved", detail, "ui_error");
      refreshRemoteSessions();
    }
  }

  function parseTransferError(error) {
    try {
      const body = JSON.parse(String(error?.message || "{}"));
      const inner = body?.detail;
      if (inner?.detail === "device_unreachable") {
        const label = inner.device_label || "That device";
        if (inner.last_seen_at) {
          return `${label} isn't reachable. Last seen ${fmtTimeAgo(inner.last_seen_at)}.`;
        }
        return `${label} isn't reachable right now.`;
      }
    } catch { /* fall through */ }
    return "That device didn't accept the queue.";
  }

  /// The session whose queue a pick would move: whatever is actually playing, wherever it is,
  /// falling back to this tab.
  function playbackSourceSession() {
    return remoteSessions.find((r) => r.presence === "live" && r.status === "playing")
      || remoteSessions.find((r) => r.presence === "live" && (r.status === "playing" || r.status === "paused"))
      || remoteSessions.find((r) => r.current);
  }

  function deviceDisplayName(device) {
    // The current session is named for what it IS. Repeating this machine's own name in a picker
    // reads as a duplicate row rather than as "here".
    if (device.current) return "This Device";
    return device.device_label || "Another device";
  }

  /// "Play on" rows, shared by the docked player and the idle dock so the two cannot offer different
  /// things. Picking a device MOVES playback there from wherever it is — you are not limited to
  /// moving this tab's own queue. Unreachable devices are listed and DISABLED with a "last seen"
  /// line: a picker that omits a device the user owns reads as broken.
  function deviceMenuItems() {
    if (remoteSessions.length === 0) {
      return [{ label: "No sessions signed in", disabled: true }];
    }
    const source = playbackSourceSession();
    return remoteSessions.map((device) => {
      const label = deviceDisplayName(device);
      if (device.presence === "unreachable") {
        return {
          label: `${label} — last seen ${device.last_used_at ? fmtTimeAgo(device.last_used_at) : "a while ago"}`,
          disabled: true,
        };
      }
      const isSource = source && device.session_id === source.session_id
        && (device.status === "playing" || device.status === "paused");
      if (isSource) {
        return { label: `${label} — ${device.status === "playing" ? "playing" : "paused"}`, disabled: true };
      }
      return {
        label: `Play on ${label}`,
        disabled: !source,
        action: () => movePlaybackTo(source, device),
      };
    });
  }

  /// Move playback from any session to any other.
  ///
  /// When the source is this tab the queue travels with the request and local playback stops on the
  /// 200. When it is another device the server already holds that session's published queue and tells
  /// it to stop itself — which is what lets this tab move a phone's music to a Mac it isn't.
  async function movePlaybackTo(source, target) {
    if (!source || source.session_id === target.session_id) return;
    if (source.current) {
      await transferPlaybackTo(target.session_id, deviceDisplayName(target));
      return;
    }
    try {
      const result = await api("/player/transfer", {
        method: "POST",
        body: JSON.stringify({
          from_session_id: source.session_id,
          to_session_id: target.session_id,
          autoplay: source.status === "playing",
        }),
      });
      setToast({
        title: "Playback moved",
        body: `Now on ${result?.to_device_label || deviceDisplayName(target)}.`,
      });
    } catch (error) {
      notify("Playback not moved", parseTransferError(error), "ui_error");
    }
    refreshRemoteSessions();
  }

  // Returning to a backgrounded tab should not wait out a throttled interval before noticing a
  // queue that was handed here while it was hidden.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") commandPollNowRef.current?.();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);

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
          let outcome;
          try { outcome = await remoteExecRef.current?.(cmd); } catch { /* keep going */ }
          // ⚠ The one command that may be left unacked. A handoff carries a QUEUE rather than an
          // instruction, so acking a transient network failure would drop someone's playback with
          // nothing to retry from. Bounded by the server's five-minute handoff expiry, after which
          // the fetch 410s — a permanent outcome, which acks. Remove that expiry and this retries
          // forever.
          if (outcome === "retry") continue;
          await api(`/player/commands/${cmd.id}/ack`, { method: "POST" }).catch(() => {});
        }
      } catch {
        /* offline / transient */
      } finally {
        commandPollingRef.current = false;
      }
    }
    poll();
    // Exposed so returning to a hidden tab can poll at once. A hidden tab's setInterval is throttled
    // to roughly once a minute, which would otherwise leave a handed-over queue sitting unnoticed for
    // up to that long after the user came back to the page.
    commandPollNowRef.current = poll;
    const timer = setInterval(() => { if (!stopped) poll(); }, 4000);
    return () => { stopped = true; clearInterval(timer); };
  }, [token, user?.id]);

  /// Send a queue addition to whichever session is actually playing, when that is not this tab.
  ///
  /// ⚠ Both queue-addition paths go through this, so "Add to Queue" and "Play Next" mean the same
  /// thing everywhere they appear. Standing at an idle tab with music on another device, queueing a
  /// song obviously means "put it after what I'm listening to" — starting a second, silent player
  /// here is never what was asked for.
  function forwardQueueAddition(tracks, next) {
    if (currentTrack || !activeRemoteSession) return false;
    const items = (tracks || [])
      .filter((track) => track?.id || track?._episodeId)
      .map((track) => ({
        type: track._kind === "episode" ? "episode" : "track",
        id: track._episodeId || track.id,
        podcast_id: track._podcastId || null,
      }));
    if (items.length === 0) return false;
    enqueueOnRemote(activeRemoteSession.session_id, items, next);
    setToast({
      title: next ? "Playing next" : "Queue updated",
      body: `Added to ${activeRemoteSession.device_label || "the device that's playing"}.`,
    });
    return true;
  }

  /// Send a fresh queue to whichever session is actually playing, when that is not this tab.
  ///
  /// The Play counterpart of `forwardQueueAddition` above: standing at an idle tab that is only
  /// showing another device's now-playing card, clicking Play on a row/album/artist/playlist
  /// means "play this there", not "start a second, silent player in this tab" (which is what
  /// unconditionally calling loadPlayerTrack here used to do — it also stomped `currentTrack`,
  /// which flips `remote` to null and is why pause/skip/resume looked broken afterwards: the
  /// docked player had quietly switched from controlling the other session to controlling this
  /// tab's own, mostly-empty local queue).
  function forwardPlayToRemote(queue, wantShuffle) {
    if (currentTrack || !activeRemoteSession) return false;
    // Same cap `buildQueueSnapshot` uses for an already-loaded queue (mirrors the server's
    // HANDOFF_MAX_ITEMS) — starting fresh at index 0, there is no "look back" to preserve, so this
    // just takes the head. A library-sized "Shuffle all" started remotely still needs to fit.
    const MAX = 500;
    const items = queue
      .filter((track) => track?.id || track?._episodeId)
      .slice(0, MAX)
      .map((track) => ({
        type: track._kind === "episode" ? "episode" : "track",
        id: track._episodeId || track.id,
        podcast_id: track._podcastId || null,
      }));
    if (items.length === 0) return false;
    transferSnapshotToRemote(activeRemoteSession.session_id, {
      version: 1,
      items,
      current_index: 0,
      position_seconds: 0,
      playing: true,
      shuffle: wantShuffle,
      repeat,
    }, activeRemoteSession.device_label);
    return true;
  }

  /// Push a freshly-built snapshot (not this tab's own queue — nothing is playing here) to another
  /// session and have it start playing. Reuses `/player/transfer`, the same endpoint the explicit
  /// "Play on {device}" picker uses, with `autoplay: true`; unlike `transferPlaybackTo` there is no
  /// local playback to stop first, since this tab was only ever viewing the remote card.
  async function transferSnapshotToRemote(sessionId, snapshot, deviceLabel) {
    try {
      const result = await api("/player/transfer", {
        method: "POST",
        body: JSON.stringify({ to_session_id: sessionId, autoplay: true, snapshot }),
      });
      setToast({ title: "Now playing", body: `Playing on ${result?.to_device_label || deviceLabel || "that device"}.` });
      setRemoteQueueSession(null);
      refreshRemoteSessions();
      confirmRemoteSoon();
    } catch (error) {
      notify("Playback not started", parseTransferError(error), "ui_error");
      refreshRemoteSessions();
    }
  }

  function addTracksToPlayerQueue(tracks) {
    const playable = tracks.filter((track) => track?.id);
    if (playable.length === 0) return;
    if (forwardQueueAddition(playable, false)) return;
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

  /// Insert immediately after whatever is playing, rather than at the end of the queue.
  ///
  /// Anchored on the CURRENT INDEX, not on a fresh id lookup: an id can appear more than once in a
  /// queue, and re-finding it resolves to the wrong occurrence. With nothing playing there is no
  /// "next", so this starts playback instead of quietly doing nothing.
  function playTracksNext(tracks) {
    const playable = (tracks || []).filter((track) => track?.id);
    if (playable.length === 0) return;
    if (forwardQueueAddition(playable, true)) return;
    if (!currentTrack) {
      playTracks(playable);
      return;
    }
    const anchor = currentTrackIndex;
    setPlayerQueue((current) => {
      const at = anchor >= 0 && anchor < current.length ? anchor + 1 : current.length;
      return [...current.slice(0, at), ...playable, ...current.slice(at)];
    });
    setPlayerOpen(true);
    setToast({
      title: "Playing next",
      body: playable.length === 1 ? playable[0].title : `${playable.length} tracks queued next.`,
    });
  }

  async function loadPlayerTrack(track) {
    if (!track?.id) return;
    try {
      setAudioUrl(trackStreamUrl(track, token));
      setCurrentTrack(track);
      // Podcast episodes track their own per-user resume position (see the resume effect)
      // and don't log a track play; library tracks record a play.
      if (track._kind !== "episode") recordPlay(track.id);
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
    const isEpisode = track?._kind === "episode";
    api("/player/status", {
      method: "POST",
      body: JSON.stringify({
        track_id: isEpisode ? null : (track?.id || null),
        episode_id: isEpisode ? (track?._episodeId || track?.id || null) : null,
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
        client: "web",
        queue_hash: queueHash(),
      }),
    })
      .then((reply) => {
        // The server only asks when its stored copy disagrees with the hash we sent, so an unchanged
        // queue is never re-uploaded however long it plays.
        if (reply?.queue_stale) publishQueue();
      })
      .catch(() => {});
    if (isEpisode && details.position_seconds != null) {
      reportEpisodeProgress(track, details.position_seconds, details.duration_seconds);
    }
  }

  // Throttled per-episode resume/played sync (position is reported ~every few seconds by the player).
  function reportEpisodeProgress(track, positionSeconds, durationSeconds, { played = false } = {}) {
    const episodeId = track?._episodeId || track?.id;
    if (!episodeId) return;
    const now = Date.now();
    const last = lastEpisodeProgressRef.current;
    if (!played && last && last.id === episodeId && now - last.at < 10000) return;
    lastEpisodeProgressRef.current = { id: episodeId, at: now };
    const body = { position_ms: Math.max(0, Math.round((positionSeconds || 0) * 1000)) };
    if (durationSeconds) body.duration_ms = Math.round(durationSeconds * 1000);
    if (played) body.played = true;
    api(`/podcasts/episodes/${episodeId}/progress`, { method: "PUT", body: JSON.stringify(body) }).catch(() => {});
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

  // When a track finishes: mark a podcast episode played (resets its resume), then advance.
  async function handlePlaybackEnded() {
    if (currentTrack?._kind === "episode") {
      reportEpisodeProgress(currentTrack, 0, null, { played: true });
    }
    await playNextTrack();
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

  // Task Queue selection is LOCAL UI state — toggling a checkbox never touches the backend.
  // It only decides what gets sent when "Run selected" is clicked, keeping selection
  // independent from what is actually running/downloading.
  const [selectedApprovalIds, setSelectedApprovalIds] = useState(() => new Set());
  const knownApprovalIdsRef = useRef(new Set());

  // Reconcile local selection whenever the approvals list refreshes (polled ~2.5s):
  //  - newly-appeared items seed from the server's default `selected` (fresh candidates come in
  //    checked, preserving the old auto-select behaviour),
  //  - ids that vanished (search finished/moved, item rejected/completed) are dropped,
  //  - the user's own local toggles on still-present items are preserved.
  useEffect(() => {
    const currentIds = new Set();
    const seeds = [];
    for (const batch of approvals) {
      for (const item of batch.items) {
        currentIds.add(item.id);
        if (!knownApprovalIdsRef.current.has(item.id) && item.selected) seeds.push(item.id);
      }
    }
    setSelectedApprovalIds((prev) => {
      const next = new Set();
      let changed = false;
      for (const id of prev) { if (currentIds.has(id)) next.add(id); else changed = true; }
      for (const id of seeds) { if (!next.has(id)) { next.add(id); changed = true; } }
      return changed ? next : prev;
    });
    knownApprovalIdsRef.current = currentIds;
  }, [approvals]);

  function toggleApprovalItems(itemIds, selected) {
    setSelectedApprovalIds((prev) => {
      const next = new Set(prev);
      for (const id of itemIds) { if (selected) next.add(id); else next.delete(id); }
      return next;
    });
  }

  // Candidate picker: choose exactly one file among a track's alternates (local only).
  function selectOnlyApprovalItem(siblingIds, itemId) {
    setSelectedApprovalIds((prev) => {
      const next = new Set(prev);
      for (const id of siblingIds) next.delete(id);
      next.add(itemId);
      return next;
    });
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
        const ids = batchItems.map((item) => item.id);
        // Select exactly what we're running (select-only — deselections are never pushed, so a
        // checkbox change can't cancel an already-running download), then approve those ids.
        await api(`/approvals/${batchId}/selection`, {
          method: "POST",
          body: JSON.stringify({ item_ids: ids, selected: true }),
        });
        createdTasks.push(
          await api(`/approvals/${batchId}/approve`, {
            method: "POST",
            body: JSON.stringify({ item_ids: ids }),
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
          body: JSON.stringify({ item_ids: batchItems.map((item) => item.id) }),
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

  // Rendered either standalone in the topbar (nothing playing) or inside the docked player
  // (headerActions prop) so the two never fight over layout — see the topbar JSX below.
  const topbarUtilityActions = (
    <>
      <button className="icon-button" onClick={refreshAll} title="Refresh">
        <RefreshCw size={18} />
      </button>
      {user && (
        <div className="notification-anchor">
          <button className="icon-button" onClick={openNotificationTray} title="Notifications">
            <Bell size={18} />
            {unreadNotifications.length > 0 && <span className="badge">{unreadNotifications.length}</span>}
          </button>
          {/* Portalled to document.body rather than positioned relative to this button: when the
              player is docked, this whole group renders inside .topbar-player-row, which needs
              overflow: hidden for its own layout — a plain position:absolute tray there was
              clipped away entirely (visibly toggled, but never actually visible). Anchored with
              trayAnchor, computed from the actually-clicked button in openNotificationTray so it
              still works from whichever of the tray's several render sites was clicked. */}
          {trayOpen && trayAnchor && appRootRef.current && createPortal(
            <NotificationTray
              notifications={notifications}
              onClear={clearNotifications}
              style={{ position: "fixed", top: trayAnchor.top, right: trayAnchor.right }}
            />,
            appRootRef.current
          )}
        </div>
      )}
      <button className="icon-button" onClick={logout} title="Sign out">
        <LogOut size={18} />
      </button>
    </>
  );

  return (
    <main
      ref={appRootRef}
      className={`${theme}${playerDocked ? " player-docked" : ""}`}
      style={{
        ...appearanceVars,
        "--player-dock-height": playerDocked ? `${playerDockHeight}px` : "0px",
        "--toast-bottom": playerDocked ? `${playerToastHeight + 32}px` : "18px",
      }}
    >
      <aside className="sidebar">
        <div className="brand">
          {/* The lockup carries the app name, so there is no separate text node. Keeps
              handleBrandTap (shift + 3 clicks toggles the diagnostics overlay). */}
          <div
            className="brand-lockup"
            role="img"
            aria-label="Nudibranch"
            onClick={handleBrandTap}
            style={{ userSelect: "none", WebkitUserSelect: "none", cursor: "default" }}
          />
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
          {/* ⚠ ONE player, whether the audio is here or on another session. There used to be a
              separate remote dock, and it drifted immediately — different controls, no queue, no
              favourite, its own layout. The player takes a `remote` prop instead and reads its
              display and transport from that; the audio engine below is simply idle. */}
          {(playerOpen || activeRemoteSession) && (
            <AudioPlayer
              headerActions={topbarUtilityActions}
              remote={currentTrack ? null : activeRemoteSession}
              remotePosition={activeRemoteSession ? interpolatedRemotePosition(activeRemoteSession) : 0}
              remoteQueue={remoteQueue}
              onRemoteLive={(delta) => setRemoteViewers((n) => Math.max(0, n + delta))}
              onRemoteCommand={(action, positionSeconds) =>
                activeRemoteSession && remoteCommand(activeRemoteSession.session_id, action, positionSeconds)}
              onRemoteMode={(mode) =>
                activeRemoteSession && remoteMode(activeRemoteSession.session_id, mode)}
              onRemoteQueueJump={(queueIndex) =>
                activeRemoteSession && remoteQueueJump(activeRemoteSession.session_id, queueIndex)}
              controlRef={playbackControlRef}
              equalizer={equalizer}
              currentTrack={currentTrack}
              audioUrl={audioUrl}
              nextAudioUrl={nextAudioUrl}
              lyricsUrl={lyricsUrl}
              queue={playerQueue}
              currentIndex={currentTrackIndex}
              queueOpen={queueOpen}
              setQueueOpen={setQueueOpen}
              onPlayTrack={loadPlayerTrack}
              onEnded={handlePlaybackEnded}
              onSkipBack={playPreviousTrack}
              onSkipForward={playNextTrack}
              shuffle={shuffle}
              repeat={repeat}
              onToggleShuffle={toggleShuffle}
              onCycleRepeat={() => setRepeat((r) => (r === "off" ? "all" : r === "all" ? "one" : "off"))}
              playlists={playlists}
              onAddToPlaylist={addTracksToPlaylist}
              onPlaybackState={(status, details) => reportPlayerStatus(currentTrack, status, details)}
              onPlaybackError={(track, reason) => notify("Can't play track", `${track?.title || "This track"} couldn't be played — ${reason}.`, "ui_error")}
              onDockChange={({ popped, compactHeight, fullHeight }) => {
                setPlayerPopped(popped);
                setPlayerDockHeight(compactHeight || 0);
                setPlayerToastHeight(fullHeight || compactHeight || 0);
              }}
              onRemoveFromQueue={removeFromQueue}
              deviceMenuItems={deviceMenuItems}
              crossfadeDuration={crossfadeDuration}
              apiKey={token}
              diagnostics={playerDiagnostics}
              onClose={() => {
                reportPlayerStatus(currentTrack, "stopped");
                setPlayerOpen(false);
              }}
            />
          )}
          {/* When the player is docked, these render INSIDE it (headerActions prop) so the
              player and the utility icons read as one cohesive header instead of two stacked,
              visually disconnected rows — the gap between them was the single biggest
              complaint once the player moved into the topbar. Only one of the two renders at
              a time; the notification tray's outside-click ref only ever attaches to whichever
              copy is actually mounted. */}
          {!(playerOpen || activeRemoteSession) && (
            <div className="topbar-side topbar-side-right">{topbarUtilityActions}</div>
          )}
          {loading && <div className="working-indicator" aria-live="polite">Working…</div>}
        </header>

        <div className={`content-grid${NO_INSPECTOR_PAGES.has(page) ? " no-inspector" : ""}`}>
          <section className="panel main-panel">
            {albumDetail ? (
              <AlbumDetailPage
                playlists={playlists}
                onAddToPlaylist={addTracksToPlaylist}
                onPlayAlbumNext={playAlbumNext}
                onPlayNextTracks={playTracksNext}
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
                playlists={playlists}
                onAddToPlaylist={addTracksToPlaylist}
                onPlayArtistNext={playArtistNext}
                onPlayNextTracks={playTracksNext}
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
              <HomeView homeLayout={user?.home_layout_web?.rows} onSaveHomeLayout={saveHomeLayoutWeb} api={api} apiKey={token} onPlayAlbum={playAlbumFromHome} onPlayAlbumNext={playAlbumNext} onQueueAlbum={queueAlbumFromHome} onPlayPlaylist={playPlaylistFromHome} onOpenAlbum={(al) => openAlbumDetail(al, "Home")} onPlayArtist={playArtistFromHome} onPlayArtistNext={playArtistNext} pinnedAlbumIds={pinnedAlbumIds} onTogglePinAlbum={toggleAlbumPin} pinnedArtistIds={pinnedArtistIds} onTogglePinArtist={toggleArtistPin} pinnedPodcastIds={pinnedPodcastIds} onTogglePinPodcast={togglePodcastPin} onOpenPodcast={openPodcastDetail} homeVersion={homeVersion} onUnpinPlaylist={unpinPlaylist} onOpenArtist={(ar) => openArtistDetail(ar, "Home")} onQueueArtist={queueArtistFromHome} onPlayTracks={playTracks} onPlayNextTracks={playTracksNext} onQueueTracks={addTracksToPlayerQueue} onPlayAll={() => playAllLibrary(false)} onShuffleAll={() => playAllLibrary(true)} playlists={playlists} onAddToPlaylist={addTracksToPlaylist} />
            )}
            {page === "Library" && (
              <LibraryTree
                onPlayNext={playTracksNext}
                onPlayAlbumNext={playAlbumNext}
                onPlayArtistNext={playArtistNext}
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
                onSetCoverFromUrl={setCoverFromUrl}
                notify={notify}
                onArtistCoverUpload={uploadArtistCover}
                refreshVersion={refreshVersion}
                onOpenArtist={(ar) => openArtistDetail(ar, "Library")}
                onPlayArtist={playArtistFromHome}
                onQueueArtist={queueArtistFromHome}
              />
            )}
            {page === "Task Queue" && (
              <Approvals
                approvals={approvals}
                selectedIds={selectedApprovalIds}
                onToggle={toggleApprovalItems}
                onSelectOnly={selectOnlyApprovalItem}
                onApprove={approveItems}
                onReject={rejectItems}
                onRemove={(item) => rejectItems([item])}
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
                equalizer={equalizer}
                setEqualizer={setEqualizer}
                onSaveSearchThreshold={saveSearchThreshold}
                user={user}
                apiKey={token}
                playlists={playlists}
                integrationSettings={integrationSettings}
                onSaveIntegrations={saveIntegrationSettings}
                onUploadYoutubeCookies={uploadYoutubeCookies}
                api={api}
                notify={notify}
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
              <WishlistWorkspace
                user={user}
                wishlist={wishlist}
                approvals={wishlistApprovals}
                onSearch={searchDiscover}
                onFetchTracks={fetchDiscoverAlbumTracks}
                onQueue={queueDiscoverDownloads}
                apiKey={token}
                onAdd={createWishlistItem}
                onRemove={removeWishlistItem}
                onRemoveMany={removeWishlistItems}
                onSubmit={submitWishlistApprovals}
                onSearchAlbums={searchImportAlbums}
                onLookupAlbum={lookupImportAlbum}
                onInspectorActionsChange={setWishlistInspectorActions}
              />
            )}
            {page === "Approvals" && (
              <WishlistApprovalsView
                wishlist={wishlist}
                user={user}
                onRemove={removeWishlistItem}
                onRemoveMany={removeWishlistItems}
                onSubmit={submitWishlistApprovals}
                onInspectorActionsChange={setApprovalsInspectorActions}
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
                onPlayNext={playTracksNext}
                onQueue={addTracksToPlayerQueue}
                onQueuePosition={proposePlaylistPosition}
                onInspectorActionsChange={setPlaylistInspectorActions}
                onRefresh={refreshPlaylists}
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
            {page === "Podcasts" && (
              <PodcastsView
                api={api}
                apiKey={token}
                notify={notify}
                onPlay={playTracks}
                onPlayNext={playTracksNext}
                onQueue={addTracksToPlayerQueue}
                refreshVersion={refreshVersion}
                onInspectorActionsChange={setPodcastInspectorActions}
                pinnedPodcastIds={pinnedPodcastIds}
                onTogglePinPodcast={togglePodcastPin}
                initialPodcastRequest={podcastOpenRequest}
                onInitialPodcastConsumed={() => setPodcastOpenRequest(null)}
              />
            )}
            {!["Home", "Library", "Task Queue", "Import/Add", "Activity", "Settings", "Tools", "Wishlist", "Approvals", "Playlists", "Podcasts", "Users", "Automations"].includes(page) && <Placeholder page={page} />}
            </>
            )}
          </section>

          {!NO_INSPECTOR_PAGES.has(page) && (
          <Inspector
            page={page}
            mobileOpen={mobileInspectorOpen}
            onCloseMobile={() => setMobileInspectorOpen(false)}
            api={api}
            user={user}
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
            approvalsActions={approvalsInspectorActions}
            playlistActions={playlistInspectorActions}
            podcastActions={podcastInspectorActions}
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

      {/* Mobile navigation. The desktop sidebar is hidden below the breakpoint and this takes
          over as an iOS-style bottom tab bar: the first four permitted pages get a tab, and
          everything else lives behind "More". Rendered unconditionally and hidden with CSS so
          there is no viewport-width state in React to get out of sync with the media query. */}
      <nav className="mobile-tabbar">
        {visibleNavItems.slice(0, 4).map(([label, Icon]) => (
          <button
            key={label}
            className={page === label ? "active" : ""}
            onClick={() => { setAlbumDetail(null); setArtistDetail(null); setPage(label); setMobileMoreOpen(false); }}
          >
            <Icon size={19} />
            <span>{label}</span>
          </button>
        ))}
        {/* Unconditional now: it used to only exist when nav overflowed past 4 items, but it's
            also mobile's only route to refresh/notifications/sign-out (see mobile-more-sheet
            below) now that those no longer sit in the header there. */}
        <button
          className={mobileMoreOpen || !visibleNavItems.slice(0, 4).some(([label]) => label === page) ? "active" : ""}
          onClick={() => setMobileMoreOpen((value) => !value)}
          aria-expanded={mobileMoreOpen}
        >
          <Menu size={19} />
          <span>More</span>
        </button>
      </nav>
      {/* The Inspector carries actions with no other home on several pages (Create playlist,
          Add selected to task queue, the whole Import scan/upload flow) — a FAB opens it as a
          bottom sheet on mobile rather than leaving those permanently unreachable there. */}
      {!NO_INSPECTOR_PAGES.has(page) && (
        <button className="mobile-inspector-fab" onClick={() => setMobileInspectorOpen(true)} aria-label="Page actions">
          <MoreHorizontal size={22} />
        </button>
      )}
      {mobileInspectorOpen && (
        <div className="mobile-inspector-backdrop" onClick={() => setMobileInspectorOpen(false)} />
      )}
      {mobileMoreOpen && (
        <div className="mobile-more-backdrop" onClick={() => setMobileMoreOpen(false)}>
          <div className="mobile-more-sheet" onClick={(event) => event.stopPropagation()}>
            {/* Refresh/notifications/sign-out: global, not page-scoped, so the Inspector (a
                page-actions sheet, and absent on Home/Settings besides) is the wrong home for
                them — the More sheet is the one surface every page has in common. */}
            <div className="mobile-more-utility-row">{topbarUtilityActions}</div>
            {visibleNavItems.slice(4).map(([label, Icon]) => (
              <button
                key={label}
                className={page === label ? "active" : ""}
                onClick={() => { setAlbumDetail(null); setArtistDetail(null); setPage(label); setMobileMoreOpen(false); }}
              >
                <Icon size={18} />
                <span>{label}</span>
              </button>
            ))}
          </div>
        </div>
      )}
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
          <div className="brand-lockup" role="img" aria-label="Nudibranch" />
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

function NotificationTray({ notifications, onClear, style }) {
  return (
    <div className="notification-tray" style={style}>
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

function episodeToPlayable(episode, podcast, apiKey) {
  const cover = podcast?.has_cover && apiKey
    ? `${API_BASE}/podcasts/${encodeURIComponent(podcast.id)}/cover?api_key=${encodeURIComponent(apiKey)}`
    : "";
  const resumeMs = episode.progress && !episode.progress.played ? (episode.progress.position_ms || 0) : 0;
  return {
    id: episode.id,
    title: episode.title,
    _kind: "episode",
    _episodeId: episode.id,
    _streamPath: `/podcasts/episodes/${episode.id}/stream`,
    _artist: podcast?.author || podcast?.title || "Podcast",
    _album: podcast?.title || "Podcast",
    // ⚠ The podcast's ID, not just its title. A handed-over queue identifies an episode by
    // (episode_id, podcast_id) because the receiving client can only rebuild a playable episode
    // from its podcast — without this, every episode in a web-sent queue is unresolvable there.
    _podcastId: podcast?.id || null,
    _coverUrl: cover,
    _resumeMs: resumeMs,
    _durationMs: episode.duration_ms || 0,
  };
}

// Every episode is playable: the audio streams from the publisher through the server's relay, so
// there is no longer such a thing as an episode that exists but cannot be played.
/// The single episode a podcast resumes to: the one last left unfinished, else the most recent
/// unplayed, else the newest. Shared by "Play" and "Play next" so the two can never disagree about
/// where a show is up to — the same rule the iOS app resumes by.
function podcastResumeEpisode(episodes) {
  const playable = episodes || [];
  if (!playable.length) return null;
  const partial = playable
    .filter((episode) => !episode.progress?.played && (episode.progress?.position_ms || 0) > 0)
    .sort((left, right) => {
      const leftTime = Date.parse(left.progress?.updated_at || "") || 0;
      const rightTime = Date.parse(right.progress?.updated_at || "") || 0;
      return rightTime - leftTime;
    })[0];
  return partial || playable.find((episode) => !episode.progress?.played) || playable[0];
}

function podcastPlayQueue(episodes, podcast, apiKey) {
  const playable = episodes || [];
  const lead = podcastResumeEpisode(playable);
  if (!lead) return [];
  const remainingUnplayed = playable.filter((episode) => episode.id !== lead.id && !episode.progress?.played);
  return [lead, ...remainingUnplayed].map((episode) => episodeToPlayable(episode, podcast, apiKey));
}

function formatEpisodeDate(value) {
  if (!value) return "";
  try { return new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
  catch { return ""; }
}

function formatDurationMs(ms) {
  if (!ms) return "";
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function PodcastsView({ api, apiKey, notify, onPlay, onPlayNext, onQueue, refreshVersion, onInspectorActionsChange, pinnedPodcastIds, onTogglePinPodcast, initialPodcastRequest, onInitialPodcastConsumed }) {
  const [podcasts, setPodcasts] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [addOpen, setAddOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const selected = (podcasts || []).find((podcast) => podcast.id === selectedId) || null;

  const loadPodcasts = useCallback(async () => {
    try { setPodcasts(await api("/podcasts")); }
    catch { setPodcasts([]); }
  }, [api]);

  useEffect(() => { loadPodcasts(); }, [loadPodcasts, refreshVersion]);

  useEffect(() => {
    if (!initialPodcastRequest?.id) return;
    setSelectedId(initialPodcastRequest.id);
    onInitialPodcastConsumed?.();
  }, [initialPodcastRequest?.nonce]);

  const subscribe = useCallback(async (feedUrl) => {
    const url = feedUrl.trim();
    if (!url) return;
    setSubmitting(true);
    try {
      await api("/podcasts", { method: "POST", body: JSON.stringify({ feed_url: url }) });
      await loadPodcasts();
      setAddOpen(false);
      notify?.("Podcast added", "The feed is being checked for new episodes.", "ui_notice");
    } catch (error) {
      notify?.("Couldn't subscribe", error.message, "ui_error");
    } finally {
      setSubmitting(false);
    }
  }, [api, loadPodcasts, notify]);

  const [scanning, setScanning] = useState(false);
  const scanAll = useCallback(async () => {
    setScanning(true);
    try {
      await api("/podcasts/scan", { method: "POST" });
      await loadPodcasts();
      notify?.("Podcast scan queued", "Checking subscribed feeds for new episodes.", "ui_notice");
    } catch (error) {
      notify?.("Couldn't scan podcasts", error.message, "ui_error");
    } finally {
      setScanning(false);
    }
  }, [api, loadPodcasts, notify]);

  useEffect(() => {
    if (selectedId) return undefined;
    const rows = podcasts || [];
    onInspectorActionsChange?.({
      onAdd: () => setAddOpen(true),
      onScan: scanAll,
      scanning,
      podcastCount: rows.length,
      episodeCount: rows.reduce((total, podcast) => total + (podcast.episode_count || 0), 0),
    });
    return () => onInspectorActionsChange?.(null);
  }, [podcasts, scanAll, onInspectorActionsChange, selectedId, scanning]);

  const playableEpisodes = useCallback(async (podcast) => {
    const data = await api(`/podcasts/${encodeURIComponent(podcast.id)}/episodes?page=1&page_size=500`);
    return data?.items || [];
  }, [api]);

  async function playPodcast(podcast) {
    try {
      const episodes = await playableEpisodes(podcast);
      const queue = podcastPlayQueue(episodes, podcast, apiKey);
      if (!queue.length) return notify?.("Nothing to play", "This podcast has no episodes yet.", "ui_notice");
      onPlay(queue, { keepLead: false });
    } catch (error) {
      notify?.("Couldn't play podcast", error.message, "ui_error");
    }
  }

  async function queuePodcast(podcast) {
    try {
      const episodes = (await playableEpisodes(podcast)).map((episode) => episodeToPlayable(episode, podcast, apiKey));
      if (!episodes.length) return notify?.("Nothing to queue", "This podcast has no episodes yet.", "ui_notice");
      onQueue(episodes);
    } catch (error) {
      notify?.("Couldn't queue podcast", error.message, "ui_error");
    }
  }

  // Just the resume episode, so a show can be lined up behind the current track without the rest
  // of its feed coming with it. Fetched only when the menu row is chosen.
  async function playPodcastNext(podcast) {
    try {
      const lead = podcastResumeEpisode(await playableEpisodes(podcast));
      if (!lead) return notify?.("Nothing to play", "This podcast has no episodes yet.", "ui_notice");
      onPlayNext([episodeToPlayable(lead, podcast, apiKey)]);
    } catch (error) {
      notify?.("Couldn't queue podcast", error.message, "ui_error");
    }
  }

  if (selected) {
    return (
      <>
        <PodcastDetailPage
          onPlayNext={onPlayNext}
          podcast={selected}
          api={api}
          apiKey={apiKey}
          notify={notify}
          onPlay={onPlay}
          onQueue={onQueue}
          onBack={() => setSelectedId(null)}
          onChanged={loadPodcasts}
          onInspectorActionsChange={onInspectorActionsChange}
          pinned={pinnedPodcastIds?.has(selected.id)}
          onTogglePin={onTogglePinPodcast}
        />
        {addOpen && <AddPodcastDialog api={api} submitting={submitting} onClose={() => !submitting && setAddOpen(false)} onSubscribe={subscribe} />}
      </>
    );
  }

  return (
    <div className="podcasts-view library-album-view">
      {podcasts === null ? (
        <p className="muted">Loading…</p>
      ) : podcasts.length === 0 ? (
        <div className="podcast-empty">
          <Mic2 size={36} />
          <strong>No podcasts yet</strong>
          <span className="muted">Use Add podcast in the Inspector to search or enter an RSS feed.</span>
        </div>
      ) : (
        <div className="home-album-grid podcast-library-grid">
          {podcasts.map((podcast) => (
            <PodcastCard
              key={podcast.id}
              podcast={podcast}
              apiKey={apiKey}
              onOpen={() => setSelectedId(podcast.id)}
              onPlay={() => playPodcast(podcast)}
              onPlayNext={() => playPodcastNext(podcast)}
              onQueue={() => queuePodcast(podcast)}
              pinned={pinnedPodcastIds?.has(podcast.id)}
              onTogglePin={() => onTogglePinPodcast?.(podcast)}
            />
          ))}
        </div>
      )}
      {addOpen && (
        <AddPodcastDialog
          api={api}
          submitting={submitting}
          onClose={() => !submitting && setAddOpen(false)}
          onSubscribe={subscribe}
        />
      )}
    </div>
  );
}

function PodcastCard({ podcast, apiKey, onOpen, onPlay, onPlayNext, onQueue, pinned, onTogglePin }) {
  const cover = podcastCoverUrl(podcast, apiKey);
  const subtitle = `${podcast.episode_count || 0} episode${podcast.episode_count === 1 ? "" : "s"}${podcast.unplayed_count > 0 ? ` · ${podcast.unplayed_count} unplayed` : ""}`;
  const [openMenu, menuElement] = useMenuHost();
  // No "Add to playlist": there is no episode→playlist relationship server-side, so the row could
  // never do anything. Same rule the iOS player follows.
  //
  // "Play next" queues the ONE episode the show resumes to, not the whole feed — a back catalogue
  // is a plausible feed and an implausible thing to drop in front of what you are listening to.
  const menuItems = playbackMenuItems({
    onPlay: onPlay && (() => onPlay()),
    onQueue: onQueue && (() => onQueue()),
    playLabel: "Play episodes",
    afterPlay: [onPlayNext && { label: "Play next", action: () => onPlayNext() }],
    extra: [
      onOpen && { label: "Open podcast", action: () => onOpen() },
      onTogglePin && { label: pinned ? "Unpin from Home" : "Pin to Home", action: () => onTogglePin() },
    ],
  });
  return (
    <div
      className="album-card podcast-card"
      title={`${podcast.title} — ${podcast.author || "Podcast"}`}
      onContextMenu={(event) => openMenu(event, menuItems)}
    >
      {menuElement}
      <div className="album-card-art" onClick={onOpen} role="button" tabIndex={0}>
        {cover ? <img src={cover} alt="" loading="lazy" /> : <Mic2 size={28} />}
        <span className="album-card-hover">
          <button className={`album-card-pin${pinned ? " active" : ""}`} onClick={(event) => { event.stopPropagation(); onTogglePin?.(); }} title={pinned ? "Unpin from Home" : "Pin to Home"}>
            <Pin size={15} />
          </button>
          <QueueButton className="album-card-queue" size={15} onClick={onQueue} />
          <button className="album-card-play" onClick={(event) => { event.stopPropagation(); onPlay?.(); }} title="Play episodes">
            <Play size={20} />
          </button>
        </span>
      </div>
      <div className="album-card-meta" onClick={onOpen}>
        <span className="album-card-title">{podcast.title}</span>
        <span className="album-card-artist">{subtitle}</span>
      </div>
    </div>
  );
}

function AddPodcastDialog({ api, submitting, onClose, onSubscribe }) {
  const [source, setSource] = useState("search");
  const [query, setQuery] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [results, setResults] = useState([]);
  const [searching, setSearching] = useState(false);

  useEffect(() => {
    const q = query.trim();
    if (source !== "search" || q.length < 2) {
      setResults([]);
      setSearching(false);
      return undefined;
    }
    let active = true;
    setSearching(true);
    const timer = setTimeout(() => {
      api(`/podcasts/search?q=${encodeURIComponent(q)}&limit=25`)
        .then((rows) => { if (active) setResults(rows || []); })
        .catch(() => { if (active) setResults([]); })
        .finally(() => { if (active) setSearching(false); });
    }, 350);
    return () => { active = false; clearTimeout(timer); };
  }, [api, query, source]);

  useEffect(() => {
    const closeOnEscape = (event) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const dialog = (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="podcast-add-dialog" role="dialog" aria-modal="true" aria-labelledby="add-podcast-title">
        <div className="dialog-title-row">
          <div>
            <h2 id="add-podcast-title">Add podcast</h2>
            <p className="muted">Search the Apple podcast directory or paste an RSS feed.</p>
          </div>
          <button className="icon-button" onClick={onClose} disabled={submitting} title="Close"><X size={18} /></button>
        </div>
        <div className="mode-toggle podcast-source-toggle">
          <button className={source === "search" ? "active" : ""} onClick={() => setSource("search")}><Search size={14} /> Search</button>
          <button className={source === "rss" ? "active" : ""} onClick={() => setSource("rss")}><Mic2 size={14} /> RSS feed</button>
        </div>
        {source === "search" ? (
          <>
            <div className="podcast-search-field"><Search size={16} /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search podcasts" /></div>
            <div className="podcast-search-results">
              {searching ? <p className="muted">Searching…</p> : query.trim().length >= 2 && results.length === 0 ? <p className="muted">No podcasts found.</p> : results.map((result) => (
                <div className="podcast-search-row" key={result.feed_url}>
                  <div className="podcast-search-icon"><Mic2 size={20} /></div>
                  <div className="podcast-search-copy">
                    <strong>{result.title}</strong>
                    <span className="muted">{result.author}{result.genre ? ` · ${result.genre}` : ""}</span>
                    {result.store_url && <a href={result.store_url} target="_blank" rel="noreferrer">View in Apple Podcasts</a>}
                  </div>
                  <button className="primary compact" disabled={submitting} onClick={() => onSubscribe(result.feed_url)}><Plus size={14} /> Add</button>
                </div>
              ))}
            </div>
          </>
        ) : (
          <form className="podcast-rss-form" onSubmit={(event) => { event.preventDefault(); onSubscribe(feedUrl); }}>
            <label>RSS feed URL<input autoFocus type="url" value={feedUrl} onChange={(event) => setFeedUrl(event.target.value)} placeholder="https://example.com/podcast.xml" /></label>
            <button className="primary" type="submit" disabled={submitting || !feedUrl.trim()}><Plus size={15} /> {submitting ? "Adding…" : "Add podcast"}</button>
          </form>
        )}
      </section>
    </div>
  );
  // Into the themed app root, not document.body: the panel's colours are custom properties defined
  // on `main.app`, and outside it `var(--panel)` resolves to nothing and the dialog renders with no
  // background at all. See themedPortalHost.
  const host = themedPortalHost();
  return host ? createPortal(dialog, host) : null;
}

function PodcastDetailPage({ podcast, api, apiKey, notify, onPlay, onPlayNext, onQueue, onBack, onChanged, onInspectorActionsChange, pinned, onTogglePin }) {
  const [episodes, setEpisodes] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [openMenu, menuElement] = useMenuHost();

  const loadEpisodes = useCallback(async () => {
    try {
      const data = await api(`/podcasts/${encodeURIComponent(podcast.id)}/episodes?page=1&page_size=200`);
      setEpisodes(data?.items || []);
    } catch {
      setEpisodes([]);
    }
  }, [api, podcast.id]);

  useEffect(() => { loadEpisodes(); }, [loadEpisodes]);

  const playable = useMemo(
    () => (episodes || []).map((e) => episodeToPlayable(e, podcast, apiKey)),
    [episodes, podcast, apiKey],
  );

  function playFrom(episode) {
    const list = episodes || [];
    const startIndex = list.findIndex((e) => e.id === episode.id);
    const ordered = startIndex >= 0 ? list.slice(startIndex) : list;
    onPlay(ordered.map((e) => episodeToPlayable(e, podcast, apiKey)));
  }

  async function withBusy(id, fn) {
    setBusyId(id);
    try { await fn(); await loadEpisodes(); onChanged?.(); }
    catch (error) { notify?.("Action failed", error.message, "ui_error"); }
    finally { setBusyId(null); }
  }

  const episodeCount = podcast.episode_count || 0;
  const unplayedCount = podcast.unplayed_count || 0;
  const playedCount = Math.max(0, episodeCount - unplayedCount);
  const allPlayed = episodeCount > 0 && unplayedCount === 0;
  const markAllPlayed = (played) => withBusy("__markAll", () => api(`/podcasts/${encodeURIComponent(podcast.id)}/mark-played`, {
    method: "POST",
    body: JSON.stringify({ played, scope: "all" }),
  }));
  const markBeforeOldestPlayed = () => withBusy("__markBeforeOldest", () => api(`/podcasts/${encodeURIComponent(podcast.id)}/mark-played`, {
    method: "POST",
    body: JSON.stringify({ played: true, scope: "before_oldest_played" }),
  }));

  useEffect(() => {
    onInspectorActionsChange?.({
      mode: "detail",
      onBack,
      onScan: () => withBusy("__scan", () => api(`/podcasts/${encodeURIComponent(podcast.id)}/scan`, { method: "POST" })),
      scanning: busyId === "__scan",
      onMarkAllToggle: () => markAllPlayed(!allPlayed),
      markAllBusy: busyId === "__markAll",
      allPlayed,
      onMarkBeforeOldestPlayed: markBeforeOldestPlayed,
      markBeforeOldestBusy: busyId === "__markBeforeOldest",
      canMarkBeforeOldest: playedCount > 0 && !allPlayed,
      episodeCount,
      unplayedCount,
    });
    return () => onInspectorActionsChange?.(null);
  }, [podcast.id, episodeCount, unplayedCount, busyId]);

  const cover = podcastCoverUrl(podcast, apiKey);
  // The card's right-click rows, plus the scan this page already offers from the inspector.
  const overflowItems = playbackMenuItems({
    onPlay: () => onPlay(podcastPlayQueue(episodes, podcast, apiKey), { keepLead: false }),
    onQueue: () => onQueue(playable),
    playLabel: "Play episodes",
    afterPlay: [
      onPlayNext && {
        label: "Play next",
        action: () => {
          const lead = podcastResumeEpisode(episodes || []);
          if (lead) onPlayNext([episodeToPlayable(lead, podcast, apiKey)]);
        },
      },
    ],
    extra: [
      onTogglePin && {
        label: pinned ? "Unpin from Home" : "Pin to Home",
        action: () => onTogglePin(podcast),
      },
      {
        label: "Check for New Episodes",
        disabled: busyId === "__scan",
        action: () => withBusy("__scan", () => api(`/podcasts/${encodeURIComponent(podcast.id)}/scan`, { method: "POST" })),
      },
      episodeCount > 0 && {
        label: allPlayed ? "Mark All Unplayed" : "Mark All Played",
        disabled: busyId === "__markAll",
        action: () => markAllPlayed(!allPlayed),
      },
      playedCount > 0 && !allPlayed && {
        label: "Mark Played Before Oldest Played",
        disabled: busyId === "__markBeforeOldest",
        action: markBeforeOldestPlayed,
      },
    ],
  });
  const episodeMenuItems = (episode) => playbackMenuItems({
    onPlay: () => playFrom(episode),
    onQueue: () => onQueue([episodeToPlayable(episode, podcast, apiKey)]),
    playLabel: "Play from here",
    afterPlay: [
      { label: "Play only this episode", action: () => onPlay([episodeToPlayable(episode, podcast, apiKey)]) },
    ],
    extra: [
      {
        label: episode.progress?.played ? "Mark unplayed" : "Mark played",
        disabled: busyId === episode.id,
        action: () => withBusy(episode.id, () => api(`/podcasts/episodes/${episode.id}/progress`, {
          method: "PUT",
          body: JSON.stringify({ played: !episode.progress?.played, position_ms: 0 }),
        })),
      },
    ],
  });
  return (
    <div className="album-detail-overlay podcast-detail">
      {menuElement}
      <div className="album-detail-hero">
        <div className="album-detail-cover">{cover ? <img src={cover} alt="" /> : <Mic2 size={48} />}</div>
        <div className="album-detail-info">
          <h1>{podcast.title}</h1>
          {podcast.author && <p className="muted">{podcast.author}</p>}
          <div className="album-detail-actions">
            <button onClick={() => onPlay(podcastPlayQueue(episodes, podcast, apiKey), { keepLead: false })} disabled={playable.length === 0}>
              <Play size={15} /> Play
            </button>
            <button className="secondary" onClick={() => onQueue(playable)} disabled={playable.length === 0}>
              <ListPlus size={15} /> Queue
            </button>
            <button className={`secondary${pinned ? " active" : ""}`} onClick={() => onTogglePin?.(podcast)}>
              <Pin size={15} /> {pinned ? "Pinned" : "Pin"}
            </button>
            <OverflowMenuButton openMenu={openMenu} items={overflowItems} />
          </div>
          {podcast.description && <p className="muted podcast-description">{podcast.description}</p>}
        </div>
      </div>
      <div className="album-detail-tracks">
        {episodes === null ? (
          <p className="muted">Loading…</p>
        ) : episodes.length === 0 ? (
          <p className="muted">No episodes.</p>
        ) : (
          episodes.map((episode) => {
            const progress = episode.progress;
            const pct = progress && progress.duration_ms ? Math.min(100, Math.round((progress.position_ms / progress.duration_ms) * 100)) : 0;
            return (
              <div key={episode.id} className="podcast-episode-row" onContextMenu={(event) => openMenu(event, episodeMenuItems(episode))}>
                <div className="podcast-episode-main">
                  <div className="podcast-episode-title">
                    {progress?.played && <CheckCircle size={14} className="podcast-played" />}
                    <span>{episode.title}</span>
                  </div>
                  <div className="podcast-episode-meta muted">
                    {formatEpisodeDate(episode.published_at)}
                    {episode.duration_ms ? ` · ${formatDurationMs(episode.duration_ms)}` : ""}
                  </div>
                  {pct > 0 && !progress?.played && (
                    <div className="podcast-progress"><div className="podcast-progress-fill" style={{ width: `${pct}%` }} /></div>
                  )}
                </div>
                {/* No download control: the browser streams from the publisher through the
                    server's relay. Saving episodes for offline is a native-app feature. */}
                <div className="podcast-episode-actions">
                  <button className="icon-button" title="Play" onClick={() => playFrom(episode)}><Play size={15} /></button>
                  <button className="icon-button" title="Queue" onClick={() => onQueue([episodeToPlayable(episode, podcast, apiKey)])}><ListPlus size={15} /></button>
                  <button className="icon-button" title={progress?.played ? "Mark unplayed" : "Mark played"} disabled={busyId === episode.id}
                    onClick={() => withBusy(episode.id, () => api(`/podcasts/episodes/${episode.id}/progress`, { method: "PUT", body: JSON.stringify({ played: !progress?.played, position_ms: 0 }) }))}>
                    <Check size={15} />
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function LibraryAlbumGrid({ api, apiKey, bucket, pageSize, onPageSizeChange, onPlayAlbum, onPlayAlbumNext, onQueueAlbum, onOpenAlbum, onTogglePinAlbum, pinnedAlbumIds, refreshVersion, playlists, onAddToPlaylist }) {
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
  // Resolved on demand when a context-menu row is chosen, never while building the grid: one
  // request per album here would be one request per visible card.
  const albumTrackRows = useCallback(async (album) => {
    const data = await api(`/library/tracks?album_id=${encodeURIComponent(album.id)}&page_size=500`);
    return data?.items || [];
  }, [api]);
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
              onPlayNext={onPlayAlbumNext}
              onQueue={onQueueAlbum}
              onOpen={onOpenAlbum}
              pinned={pinnedAlbumIds?.has(al.id)}
              onTogglePin={onTogglePinAlbum}
              playlists={playlists}
              onAddToPlaylist={onAddToPlaylist}
              onResolveTracks={albumTrackRows}
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

// Flat, server-paginated track browse — the third position of the Artists/Albums/Tracks toggle.
// Deliberately not a tree: there is nothing to expand under a track, so this is a plain list and
// the Tree/Grid toggle is locked to Tree while it is showing.
function LibraryTrackList({ api, bucket, pageSize, onPageSizeChange, onPlay, onPlayNext, onQueue, refreshVersion, playlists, onAddToPlaylist }) {
  const [data, setData] = useState(null);
  const [page, setPage] = useState(1);
  const [openMenu, menuElement] = useMenuHost();
  useEffect(() => { setPage(1); }, [bucket, pageSize]);
  useEffect(() => {
    let active = true;
    setData(null);
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (bucket && bucket !== "all") params.set("bucket", bucket);
    api(`/library/tracks?${params.toString()}`)
      .then((d) => { if (active) setData(d); })
      .catch(() => { if (active) setData({ items: [], total: 0 }); });
    return () => { active = false; };
  }, [api, bucket, page, pageSize, refreshVersion]);

  const items = data?.items || [];
  const total = data?.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  // No _coverUrl: playerCoverUrl derives the cover from album_id, which every row carries.
  const playable = (t) => ({ ...t, album_id: t.album_id, _artist: t.artist_name, _album: t.album_title });

  const trackMenuItems = (track, index) => playbackMenuItems({
    onPlay: () => onPlay([playable(track)]),
    onQueue: () => onQueue([playable(track)]),
    afterPlay: [
      onPlayNext && { label: "Play next", action: () => onPlayNext([playable(track)]) },
      { label: "Play from here", action: () => onPlay(items.slice(index).map(playable)) },
    ],
    playlists,
    onAddToPlaylist,
    resolve: () => [track],
  });

  return (
    <div className="library-track-view">
      {menuElement}
      {data === null ? (
        <p className="muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="muted">No tracks in this bucket.</p>
      ) : (
        <div className="tree">
          {items.map((track, index) => (
            <div key={track.id} className="tree-action-row library-row-actions" onContextMenu={(event) => openMenu(event, trackMenuItems(track, index))}>
              {/* Matches the tree's behaviour: clicking the row plays this track and queues the
                  rest of the page behind it, rather than playing one track in isolation. */}
              <button
                type="button"
                className="track-list-row"
                onClick={() => onPlay(items.slice(index).map(playable))}
              >
                <FileAudio size={15} />
                <span className="track-list-title">{track.title}</span>
                <span className="track-list-sub">{track.artist_name} — {track.album_title}</span>
              </button>
              <span className="muted track-list-duration">{formatDuration(track.duration_ms)}</span>
              <button type="button" className="icon-button" title="Play" onClick={() => onPlay([playable(track)])}>
                <Play size={15} />
              </button>
              <button type="button" className="icon-button" title="Queue" onClick={() => onQueue([playable(track)])}>
                <ListPlus size={15} />
              </button>
            </div>
          ))}
        </div>
      )}
      <div className="tree-toolbar library-page-size-row">
        <span className="muted">{total} tracks</span>
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

function LibraryArtistGrid({ api, apiKey, bucket, pageSize, onPageSizeChange, onPlayArtist, onPlayArtistNext, onQueueArtist, onOpenArtist, onTogglePinArtist, pinnedArtistIds, refreshVersion, playlists, onAddToPlaylist }) {
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
  // Same on-demand rule as the album grid, and more important here: an artist costs one request
  // per album, so this must never run just because a card rendered.
  const artistTrackRows = useCallback(async (artist) => {
    const albums = await api(`/library/albums?artist_id=${encodeURIComponent(artist.id)}&page_size=500`);
    let rows = [];
    for (const album of albums?.items || []) {
      const data = await api(`/library/tracks?album_id=${encodeURIComponent(album.id)}&page_size=500`);
      rows = rows.concat(data?.items || []);
    }
    return rows;
  }, [api]);
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
              onPlayNext={onPlayArtistNext}
              onQueue={onQueueArtist}
              onOpen={onOpenArtist}
              pinned={pinnedArtistIds?.has(ar.id)}
              onTogglePin={onTogglePinArtist}
              playlists={playlists}
              onAddToPlaylist={onAddToPlaylist}
              onResolveTracks={artistTrackRows}
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
  const menuItems = playbackMenuItems({
    onPlay: ctx.onPlay && (() => ctx.onPlay([hydrateTrack(track, artist, album)])),
    onQueue: ctx.onQueue && (() => ctx.onQueue([hydrateTrack(track, artist, album)])),
    afterPlay: [
      ctx.onPlayNext && {
        label: "Play next",
        action: () => ctx.onPlayNext([hydrateTrack(track, artist, album)]),
      },
      ctx.onPlay && { label: "Play from here", action: playFromHere },
    ],
    extra: [
      (ctx.canEditMetadata || ctx.canUsePlaylists) && { label: "Edit song…", action: () => toggleSet(ctx.setOpenTrackDetails, track.id) },
      ctx.canRemoveLibrary && { label: "Remove from library…", danger: true, action: () => ctx.setRemoveTarget(removeKey("track", track.id)) },
    ],
    playlists: ctx.canUsePlaylists ? ctx.playlists : [],
    onAddToPlaylist: ctx.onAddToPlaylist,
    resolve: () => [track],
  });
  return (
    <div>
      <div
        className="tree-action-row library-row-actions"
        onContextMenu={ctx.openMenu ? (event) => ctx.openMenu(event, menuItems) : undefined}
      >
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
  const menuItems = playbackMenuItems({
    onPlay: ctx.onPlay && (() => ctx.onPlay(albumTracks(artist, album), { keepLead: false })),
    onQueue: ctx.onQueue && (() => ctx.onQueue(albumTracks(artist, album))),
    afterPlay: [
      ctx.onPlayNext && { label: "Play next", action: () => ctx.onPlayNext(albumTracks(artist, album)) },
    ],
    extra: [
      ctx.setOpenAlbums && {
        label: ctx.openAlbums?.has(album.id) ? "Collapse" : "Expand",
        action: () => toggleSet(ctx.setOpenAlbums, album.id),
      },
      ctx.onOpenAlbum && {
        label: "Open album",
        action: () => ctx.onOpenAlbum({ ...album, artist_name: artist.name, artist_id: artist.id }),
      },
      ctx.onTogglePinAlbum && {
        label: ctx.pinnedAlbumIds?.has(album.id) ? "Unpin from Home" : "Pin to Home",
        action: () => ctx.onTogglePinAlbum(album),
      },
      (ctx.canEditMetadata || ctx.canRemoveLibrary || ctx.canUsePlaylists) && {
        label: "Edit album…",
        action: () => toggleSet(ctx.setOpenAlbumDetails, album.id),
      },
      ctx.canRemoveLibrary && {
        label: "Remove from library…",
        danger: true,
        action: () => ctx.setRemoveTarget(removeKey("album", album.id)),
      },
    ],
    playlists: ctx.canUsePlaylists ? ctx.playlists : [],
    onAddToPlaylist: ctx.onAddToPlaylist,
    resolve: () => album.tracks,
  });
  return (
    <div>
      <div
        className="tree-action-row library-row-actions"
        onContextMenu={ctx.openMenu ? (event) => ctx.openMenu(event, menuItems) : undefined}
      >
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
          onCoverPick={ctx.onSetCoverFromUrl ? {
            kind: "albums",
            id: album.id,
            api: ctx.api,
            notify: ctx.notify,
            apply: (url) => ctx.onSetCoverFromUrl("albums", album.id, url),
          } : undefined}
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

function LibraryTree({ artists, onCheckAlbum, onCoverSearch, onCheckTrackAudio, onRequeueTrack, onRequeueAlbum, onSearchAlbums, onQueueMetadata, onQueueRemove, playlists, onAddToPlaylist, user, apiKey, api, onPlay, onPlayNext, onQueue, onSearchLibrary, onSavePageSize, onPlayAlbum, onQueueAlbum, onOpenAlbum, onTogglePinAlbum, pinnedAlbumIds, onTogglePinArtist, pinnedArtistIds, onArtistCoverSearch, onAlbumCoverUpload, onArtistCoverUpload, onSetCoverFromUrl, notify, refreshVersion, onOpenArtist, onPlayArtist, onPlayArtistNext, onQueueArtist, onPlayAlbumNext }) {
  const [libraryEntity, setLibraryEntity] = useState("artist");
  const [libraryLayout, setLibraryLayout] = useState("tree");
  // Tracks have no grouping to expand and no cover to show in a grid — they are a flat list — so
  // the Tree/Grid toggle is pinned to Tree while Tracks is selected. `libraryLayout` keeps the
  // user's real choice untouched so flipping back to Artists/Albums restores their Grid.
  const trackMode = libraryEntity === "track";
  const effectiveLayout = trackMode ? "tree" : libraryLayout;
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
  // One menu host for the whole tree: the artist rows are rendered inline here and cannot own a
  // hook each, and the branch components below take `openMenu` through the context object.
  const [openMenu, menuElement] = useMenuHost();
  const treeCtx = {
    openMenu,
    onPlay, onPlayNext, onQueue,
    canEditMetadata, canRemoveLibrary, canUsePlaylists,
    playlists, onAddToPlaylist,
    removeTarget, setRemoveTarget, onQueueRemove,
    openAlbums, setOpenAlbums,
    openAlbumDetails, setOpenAlbumDetails,
    openTrackDetails, setOpenTrackDetails,
    onCheckAlbum, onCoverSearch, onAlbumCoverUpload, onSetCoverFromUrl, notify,
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
      {menuElement}
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
              {effectiveLayout === "tree" && !trackMode && (
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
              <div
                className={`library-view-toggle${trackMode ? " locked" : ""}`}
                title={trackMode ? "Tracks are always shown as a list" : undefined}
              >
                <button type="button" disabled={trackMode} className={effectiveLayout === "tree" ? "active" : ""} onClick={() => setLibraryLayout("tree")}>Tree</button>
                <button type="button" disabled={trackMode} className={effectiveLayout === "grid" ? "active" : ""} onClick={() => setLibraryLayout("grid")}>Grid</button>
              </div>
              <div className="library-view-toggle">
                <button type="button" className={libraryEntity === "artist" ? "active" : ""} onClick={() => setLibraryEntity("artist")}>Artists</button>
                <button type="button" className={libraryEntity === "album" ? "active" : ""} onClick={() => setLibraryEntity("album")}>Albums</button>
                <button type="button" className={trackMode ? "active" : ""} onClick={() => setLibraryEntity("track")}>Tracks</button>
              </div>
            </div>
          )}
          {libraryEntity === "artist" && libraryLayout === "tree" && (
          <div className="tree">
        {pagedArtists.map((artist) => (
          <div key={artist.id}>
            <div
              className="tree-action-row library-row-actions"
              onContextMenu={(event) => openMenu(event, playbackMenuItems({
                onPlay: () => onPlay(artistTracks(artist), { keepLead: false }),
                onQueue: () => onQueue(artistTracks(artist)),
                afterPlay: [onPlayNext && { label: "Play next", action: () => onPlayNext(artistTracks(artist)) }],
                extra: [
                  { label: openArtists.has(artist.id) ? "Collapse" : "Expand", action: () => toggleSet(setOpenArtists, artist.id) },
                  onOpenArtist && { label: "Open artist", action: () => onOpenArtist(artist) },
                  onTogglePinArtist && {
                    label: pinnedArtistIds?.has(artist.id) ? "Unpin from Home" : "Pin to Home",
                    action: () => onTogglePinArtist(artist),
                  },
                  canEditMetadata && { label: "Edit artist…", action: () => toggleSet(setOpenArtistDetails, artist.id) },
                  canRemoveLibrary && { label: "Remove from library…", danger: true, action: () => setRemoveTarget(removeKey("artist", artist.id)) },
                ],
                playlists: canUsePlaylists ? playlists : [],
                onAddToPlaylist,
                resolve: () => artistTracks(artist),
              }))}
            >
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
                onCoverPick={onSetCoverFromUrl ? {
                  kind: "artists",
                  id: artist.id,
                  api,
                  notify,
                  apply: (url) => onSetCoverFromUrl("artists", artist.id, url),
                } : undefined}
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
              onPlayArtistNext={onPlayArtistNext}
              onQueueArtist={onQueueArtist}
              onOpenArtist={onOpenArtist}
              onTogglePinArtist={onTogglePinArtist}
              pinnedArtistIds={pinnedArtistIds}
              refreshVersion={refreshVersion}
              playlists={playlists}
              onAddToPlaylist={onAddToPlaylist}
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
              onPlayAlbumNext={onPlayAlbumNext}
              onQueueAlbum={onQueueAlbum}
              onOpenAlbum={onOpenAlbum}
              onTogglePinAlbum={onTogglePinAlbum}
              pinnedAlbumIds={pinnedAlbumIds}
              refreshVersion={refreshVersion}
              playlists={playlists}
              onAddToPlaylist={onAddToPlaylist}
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
          {trackMode && (
            <LibraryTrackList
              api={api}
              bucket={bucket}
              pageSize={pageSize}
              onPageSizeChange={(v) => { setPageSize(v); if (onSavePageSize) onSavePageSize(v); }}
              onPlay={onPlay}
              onPlayNext={onPlayNext}
              onQueue={onQueue}
              refreshVersion={refreshVersion}
              playlists={canUsePlaylists ? playlists : []}
              onAddToPlaylist={onAddToPlaylist}
            />
          )}
          {effectiveLayout === "tree" && !trackMode && (
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

function Approvals({ approvals, selectedIds, onToggle, onSelectOnly, onApprove, onReject, onRemove }) {
  const groups = useMemo(() => groupApprovalBatches(approvals), [approvals]);

  if (groups.length === 0) {
    return <EmptyState title="No queued changes" body="Import scans, download searches, and maintenance actions will add review items here." />;
  }

  return (
    <div className="approval-tree">
      {groups.map((group) => (
        <ApprovalBatch key={group.id} batch={group} selectedIds={selectedIds} onToggle={onToggle} onSelectOnly={onSelectOnly} onApprove={onApprove} onReject={onReject} onRemove={onRemove} />
      ))}
    </div>
  );
}

function ApprovalBatch({ batch, selectedIds, onToggle, onSelectOnly, onApprove, onReject, onRemove }) {
  const [openItems, setOpenItems] = useState(() => new Set(batch.items.filter((item) => !item.parent_id).map((item) => item.id)));
  const [openCandidatePickers, setOpenCandidatePickers] = useState(() => new Set());
  const tree = useMemo(() => buildItemTree(batch.items), [batch.items]);
  const itemById = useMemo(() => new Map(batch.items.map((item) => [item.id, item])), [batch.items]);
  const selectedItems = batch.items.filter((item) => selectedIds.has(item.id));
  const selectedExecutableItems = selectedItems.filter(isExecutableApprovalItem);
  const allSelected = batch.items.length > 0 && batch.items.every((item) => selectedIds.has(item.id));
  const locked = batch.status === "executing";
  // Only the SELECTED items gate the Run button — a still-searching row you haven't picked
  // shouldn't block running the ones you have.
  const selectedSearching = selectedItems.some(isCandidateSearchItem);
  const runDisabled = locked || selectedExecutableItems.length === 0 || selectedSearching;

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
            {locked ? "Running" : selectedSearching ? "Waiting for candidates" : "Run selected"}
          </button>
        </div>
      </div>
      <div className="bulk-row">
        <label>
          <input
            type="checkbox"
            checked={allSelected}
            onChange={(event) => onToggle(batch.items.map((item) => item.id), event.target.checked)}
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
          selectedIds={selectedIds}
          onToggle={onToggle}
          onSelectOnly={onSelectOnly}
          onReject={onReject}
          onRemove={onRemove}
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
  selectedIds,
  onToggle,
  onSelectOnly,
  onReject,
  onRemove,
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
  const firstSelectedSibling = siblingCandidates.find((sibling) => selectedIds?.has(sibling.id));
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
    // Selection is local UI state (a global id set), so toggling just adds/removes ids — no
    // per-batch grouping or backend call. Checking a candidate leaf picks only that file.
    if (leafDownloadCandidate && checked) {
      onSelectOnly?.(siblingIds, item.id);
    } else {
      onToggle?.(descendantIds, checked);
    }
  }

  return (
    <>
      <div className={`proposal-row status-${item.status}`} style={{ "--depth": depth }}>
        <input
          type="checkbox"
          checked={selectedIds?.has(item.id) || false}
          disabled={item.status === "executing"}
          onChange={(event) => updateChecked(event.target.checked)}
        />
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
        {onRemove && item.status !== "executing" && (
          <button className="row-icon-button" onClick={() => onRemove(item)} title="Remove from queue">
            <X size={14} />
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
            selectedIds={selectedIds}
            onToggle={onToggle}
            onSelectOnly={onSelectOnly}
            onReject={onReject}
            onRemove={onRemove}
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

// Discover + Wishlist as one page, matching the iOS app: finding music and tracking what you
// asked for are a single flow, so they no longer live in two separate nav entries.
//
// Both panels stay MOUNTED and are hidden with the `hidden` attribute rather than being
// conditionally rendered. Two reasons: WishlistView publishes the page's Inspector actions from
// an effect, so unmounting it on every tab switch would tear those down and leave the Inspector
// empty; and keeping DiscoverView alive preserves search results when you flip over to check
// what you already requested. (`[hidden]` needs a `display: none !important` rule in styles.css
// to beat the panels' own display values.)
function WishlistWorkspace({
  user, wishlist, approvals, onSearch, onFetchTracks, onQueue, apiKey,
  onAdd, onRemove, onRemoveMany, onSubmit, onSearchAlbums, onLookupAlbum, onInspectorActionsChange,
}) {
  const [tab, setTab] = useState("discover");
  const ownCount = useMemo(
    () => wishlist.filter((item) => item.user_id === user.id).length,
    [wishlist, user.id],
  );

  return (
    <div className="workspace-split">
      <div className="workspace-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "discover"}
          className={tab === "discover" ? "active" : ""}
          onClick={() => setTab("discover")}
        >
          <Compass size={15} /> Discover
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "requests"}
          className={tab === "requests" ? "active" : ""}
          onClick={() => setTab("requests")}
        >
          <Sparkles size={15} /> My requests{ownCount ? ` (${ownCount})` : ""}
        </button>
      </div>
      <div className="workspace-tabpanel" hidden={tab !== "discover"}>
        <DiscoverView
          user={user}
          onSearch={onSearch}
          onFetchTracks={onFetchTracks}
          onWishlist={onAdd}
          onQueue={onQueue}
          apiKey={apiKey}
        />
      </div>
      <div className="workspace-tabpanel" hidden={tab !== "requests"}>
        <WishlistView
          wishlist={wishlist}
          approvals={approvals}
          user={user}
          onAdd={onAdd}
          onRemove={onRemove}
          onRemoveMany={onRemoveMany}
          onSubmit={onSubmit}
          onSearchAlbums={onSearchAlbums}
          onLookupAlbum={onLookupAlbum}
          onInspectorActionsChange={onInspectorActionsChange}
        />
      </div>
    </div>
  );
}

// Personal wishlist only — always scoped to the viewer's own items, regardless of permission.
// Other users' requests live entirely on the separate Approvals page (WishlistApprovalsView
// below), never mixed in here, even for a wishlist:approve_all holder viewing their own list.
function WishlistView({ wishlist, approvals, user, onAdd, onRemove, onRemoveMany, onSubmit, onSearchAlbums, onLookupAlbum, onInspectorActionsChange }) {
  const [albumSearchOpen, setAlbumSearchOpen] = useState(false);
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [selectedItems, setSelectedItems] = useState(() => new Set());
  const canApproveAll = hasPermission(user, "wishlist:approve_all");
  const ownWishlist = useMemo(() => wishlist.filter((item) => item.user_id === user.id), [wishlist, user.id]);
  const tree = useMemo(() => buildWishlistTree(ownWishlist), [ownWishlist]);
  const wantedItems = useMemo(() => ownWishlist.filter((item) => item.status === "wanted"), [ownWishlist]);
  const treeKey = useMemo(
    () => tree.map((artist) => `${artist.name}:${artist.albums.map((album) => album.name).join(",")}`).join("|"),
    [tree],
  );

  useEffect(() => {
    setOpenArtists(new Set(tree.map((artist) => artist.name)));
    setOpenAlbums(new Set(tree.flatMap((artist) => artist.albums.map((album) => `${artist.name}/${album.name}`))));
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
      {ownWishlist.length === 0 ? (
        <EmptyState title="No wishlist items" body="Add music here to request it." />
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
          {tree.map((artist) => renderWishlistArtist(artist, 0, "", openArtists, setOpenArtists, openAlbums, setOpenAlbums, selectedItems, setSelectedItems, onRemove, onRemoveMany))}
        </div>
      )}
    </div>
  );
}

// Other users' wishlist requests, for a wishlist:approve_all holder to review and queue for
// download. The viewer's own items never appear here — they're on the Wishlist page instead.
function WishlistApprovalsView({ wishlist, user, onRemove, onRemoveMany, onSubmit, onInspectorActionsChange }) {
  const [openOwners, setOpenOwners] = useState(() => new Set());
  const [openArtists, setOpenArtists] = useState(() => new Set());
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [selectedItems, setSelectedItems] = useState(() => new Set());
  const othersWishlist = useMemo(() => wishlist.filter((item) => item.user_id !== user.id), [wishlist, user.id]);
  const ownerTree = useMemo(() => buildWishlistOwnerTree(othersWishlist), [othersWishlist]);
  const wantedItems = useMemo(() => othersWishlist.filter((item) => item.status === "wanted"), [othersWishlist]);
  const treeKey = useMemo(
    () => ownerTree.map((owner) => `${owner.name}:${owner.artists.map((artist) => `${artist.name}:${artist.albums.map((album) => album.name).join(",")}`).join("|")}`).join("|"),
    [ownerTree],
  );

  useEffect(() => {
    setOpenOwners(new Set(ownerTree.map((owner) => owner.id)));
    setOpenArtists(new Set(ownerTree.flatMap((owner) => owner.artists.map((artist) => `${owner.id}:${artist.name}`))));
    setOpenAlbums(
      new Set(
        ownerTree.flatMap((owner) => owner.artists.map((artist) => ({ ownerId: owner.id, artist }))).flatMap(
          ({ ownerId, artist }) => artist.albums.map((album) => `${ownerId}:${artist.name}/${album.name}`),
        ),
      ),
    );
    setSelectedItems(new Set(wantedItems.map((item) => item.id)));
  }, [treeKey, wantedItems.length]);

  useEffect(() => {
    onInspectorActionsChange?.({
      selectedCount: selectedItems.size,
      onSubmitSelected: () => onSubmit([...selectedItems], { denyUnselected: true }),
    });
    return () => onInspectorActionsChange?.(null);
  }, [selectedItems.size]);

  return (
    <div className="wishlist-view">
      {othersWishlist.length === 0 ? (
        <EmptyState title="No requests waiting" body="Other users' requests will appear here for approval." />
      ) : (
        <div className="tree">
          <TreeToolbar
            expanded={openArtists.size > 0 || openAlbums.size > 0}
            onExpand={() => {
              setOpenOwners(new Set(ownerTree.map((owner) => owner.id)));
              setOpenArtists(new Set(ownerTree.flatMap((owner) => owner.artists.map((artist) => `${owner.id}:${artist.name}`))));
              setOpenAlbums(
                new Set(
                  ownerTree.flatMap((owner) => owner.artists.map((artist) => ({ ownerId: owner.id, artist }))).flatMap(
                    ({ ownerId, artist }) => artist.albums.map((album) => `${ownerId}:${artist.name}/${album.name}`),
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
          {ownerTree.map((owner) => (
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
          ))}
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

function PlaylistsView({ playlists, library, onCreatePlaylist, onAddToPlaylist, onRename, onDelete, onPlay, onPlayNext, onQueue, onQueuePosition, onInspectorActionsChange, onRefresh, api }) {
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
  const [shareOpen, setShareOpen] = useState(null);
  const [playlistName, setPlaylistName] = useState("");
  const [playlistDraftName, setPlaylistDraftName] = useState("");
  const [playlistSearch, setPlaylistSearch] = useState("");
  const [draftPositions, setDraftPositions] = useState({});
  const [openMenu, menuElement] = useMenuHost();

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
      {menuElement}
      <TreeToolbar
        expanded={openPlaylists.size > 0}
        onExpand={() => setOpenPlaylists(new Set(playlists.map((playlist) => playlist.name)))}
        onCollapse={() => setOpenPlaylists(new Set())}
      />
      <IncomingSharesPanel api={api} onAccepted={onRefresh} />
      {playlists.map((playlist) => {
        const tracks = playlist.tracks || [];
        const playableTracks = tracks.map(playlistPlayableTrack);
        // Every gesture the row's buttons offer, so the menu is never a smaller set than the row.
        // Rename and Delete live behind Edit, which is where the confirm and the name field are.
        const playlistMenuItems = playbackMenuItems({
          onPlay: playableTracks.length ? () => onPlay(playableTracks, { keepLead: false }) : null,
          onQueue: playableTracks.length ? () => onQueue(playableTracks) : null,
          afterPlay: [
            onPlayNext && playableTracks.length
              ? { label: "Play next", action: () => onPlayNext(playableTracks) }
              : null,
          ],
          extra: [
            { label: openPlaylists.has(playlist.name) ? "Collapse" : "Expand", action: () => toggleSet(setOpenPlaylists, playlist.name) },
            !playlist.protected && {
              label: pinnedIds.has(playlist.id) ? "Unpin from Home" : "Pin to Home",
              action: () => togglePin(playlist),
            },
            { label: "Add music…", action: () => setAddOpen(addOpen === playlist.id ? null : playlist.id) },
            { label: "Share…", action: () => setShareOpen(shareOpen === playlist.id ? null : playlist.id) },
            {
              label: "Edit playlist…",
              action: () => { setEditOpen(editOpen === playlist.id ? null : playlist.id); setPlaylistDraftName(playlist.name); },
            },
          ],
        });
        return (
          <div key={playlist.id}>
            <div
              className="tree-action-row library-row-actions"
              onContextMenu={(event) => openMenu(event, playlistMenuItems)}
            >
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
                  className={`row-icon-button row-secondary-action${pinnedIds.has(playlist.id) ? " active" : ""}`}
                  onClick={() => togglePin(playlist)}
                  title={pinnedIds.has(playlist.id) ? "Unpin from Home" : "Pin to Home"}
                >
                  {pinnedIds.has(playlist.id) ? <PinOff size={14} /> : <Pin size={14} />}
                </button>
              )}
              <button className="row-icon-button row-secondary-action" onClick={() => setAddOpen(addOpen === playlist.id ? null : playlist.id)} title="Add music">
                <Plus size={14} />
              </button>
              <button
                className="row-icon-button row-secondary-action"
                onClick={() => setShareOpen(shareOpen === playlist.id ? null : playlist.id)}
                title="Share playlist"
              >
                <Share2 size={14} />
              </button>
              <button
                className="row-icon-button row-secondary-action"
                onClick={() => {
                  setEditOpen(editOpen === playlist.id ? null : playlist.id);
                  setPlaylistDraftName(playlist.name);
                }}
                title="Edit playlist"
              >
                <Pencil size={14} />
              </button>
              <OverflowMenuButton
                openMenu={openMenu}
                items={playlistMenuItems}
                className="row-icon-button"
              />
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
            {shareOpen === playlist.id && (
              <PlaylistSharePanel playlist={playlist} api={api} />
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
                    <div
                      className="tree-action-row library-row-actions"
                      key={track.id}
                      onContextMenu={(event) => openMenu(event, playbackMenuItems({
                        onPlay: () => onPlay([playlistPlayableTrack(track)]),
                        onQueue: () => onQueue([playlistPlayableTrack(track)]),
                        afterPlay: [
                          onPlayNext && {
                            label: "Play next",
                            action: () => onPlayNext([playlistPlayableTrack(track)]),
                          },
                        ],
                        playlists,
                        onAddToPlaylist,
                        // The playlist entry id is not the track id — adding must send the track's.
                        resolve: () => [{ id: track.track_id }],
                      }))}
                    >
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

/* Sharing — the send half. A share is an OFFER: the recipient accepts and gets an independent copy
   materialized in whichever backend they use (native rows, or a real Jellyfin playlist if linked),
   which the server handles, so nothing here branches on backend. Stays open after each send so one
   playlist can go to several people. */
function PlaylistSharePanel({ playlist, api }) {
  const [targets, setTargets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [sent, setSent] = useState(() => new Set());
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    api("/playlists/share-targets")
      .then((rows) => { if (active) { setTargets(rows || []); setError(""); } })
      .catch(() => { if (active) setError("Couldn't load people to share with."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api]);

  async function send(target) {
    setBusy(target.id);
    setError("");
    try {
      await api(`/playlists/${encodeURIComponent(playlist.id)}/share`, {
        method: "POST",
        body: JSON.stringify({ to_user_id: target.id }),
      });
      setSent((previous) => new Set(previous).add(target.id));
    } catch (shareError) {
      setError(shareError.message || "Couldn't share that playlist.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="album-search-panel playlist-edit-panel">
      <div className="muted" style={{ marginBottom: 8 }}>
        Share &ldquo;{playlist.name}&rdquo;. They get a notification and can accept or decline;
        accepting makes them their own copy.
      </div>
      {loading && <div className="muted">Loading&hellip;</div>}
      {!loading && targets.length === 0 && (
        <div className="muted">No other account on this server can manage playlists.</div>
      )}
      {targets.map((target) => (
        <div className="tree-action-row" key={target.id}>
          <TreeRow depth={1} icon={Users} title={target.display_name} meta={target.username || ""} />
          <button
            className="secondary compact"
            onClick={() => send(target)}
            disabled={busy === target.id || sent.has(target.id)}
          >
            {sent.has(target.id) ? <Check size={15} /> : <Share2 size={15} />}
            {sent.has(target.id) ? "Sent" : busy === target.id ? "Sending\u2026" : "Send"}
          </button>
        </div>
      ))}
      {error && <div className="error-banner" style={{ marginTop: 8 }}>{error}</div>}
    </div>
  );
}

/* Sharing — the receive half. Renders nothing at all when there is nothing pending, so it costs no
   permanent chrome; its load is deliberately separate and swallowed so a share-inbox failure can
   never blank a good playlist list. */
function IncomingSharesPanel({ api, onAccepted }) {
  const [shares, setShares] = useState([]);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api("/playlists/shares")
      .then((rows) => setShares(rows || []))
      .catch(() => setShares([]));
  }, [api]);
  useEffect(load, [load]);

  async function respond(share, accept) {
    setBusy(share.id);
    setError("");
    try {
      await api(`/playlists/shares/${encodeURIComponent(share.id)}/${accept ? "accept" : "decline"}`, { method: "POST" });
      setShares((previous) => previous.filter((row) => row.id !== share.id));
      if (accept) await onAccepted?.();
    } catch (respondError) {
      setError(respondError.message || "Couldn't update that share.");
    } finally {
      setBusy(null);
    }
  }

  if (shares.length === 0) return null;
  return (
    <div className="album-search-panel playlist-edit-panel" style={{ marginBottom: 12 }}>
      <strong>Shared with you</strong>
      {shares.map((share) => (
        <div className="tree-action-row" key={share.id}>
          <TreeRow
            depth={1}
            icon={ListMusic}
            title={share.name}
            meta={
              `From ${share.from_user_name} / ${share.track_count} tracks` +
              (share.available_track_count < share.track_count
                ? ` / ${share.available_track_count} available here`
                : "")
            }
          />
          <div className="playlist-edit-actions">
            <button className="primary compact" onClick={() => respond(share, true)} disabled={busy === share.id}>
              <Check size={15} />
              Accept
            </button>
            <button className="secondary compact" onClick={() => respond(share, false)} disabled={busy === share.id}>
              <X size={15} />
              Decline
            </button>
          </div>
        </div>
      ))}
      {error && <div className="error-banner" style={{ marginTop: 8 }}>{error}</div>}
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
  onCoverPick,
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
  const [pickingCover, setPickingCover] = useState(false);

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
      {pickingCover && onCoverPick && (
        <CoverCandidatePicker
          kind={onCoverPick.kind}
          id={onCoverPick.id}
          title={title}
          api={onCoverPick.api}
          notify={onCoverPick.notify}
          onApply={onCoverPick.apply}
          onClose={() => setPickingCover(false)}
        />
      )}
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
                  {field.key === "replaygain_track_gain" && !field.readOnly && String(draft[field.key] ?? "") !== "" && (
                    <button
                      className="row-icon-button"
                      type="button"
                      onClick={() => {
                        const next = { ...draft, [field.key]: "" };
                        setDraft(next);
                        commit(next);
                      }}
                      title="Remove ReplayGain"
                      aria-label="Remove ReplayGain"
                    >
                      <X size={14} />
                    </button>
                  )}
                  {field.key === "cover_path" && onCoverPick && (
                    <button
                      className="row-icon-button"
                      type="button"
                      onClick={() => setPickingCover(true)}
                      title="Search for cover art"
                      aria-label="Search for cover art"
                    >
                      <Search size={14} />
                    </button>
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

// Grid over /library/{albums,artists}/{id}/cover-candidates. The server's first result is often
// the wrong pressing, so the whole list is shown and the operator picks. A pick posts the URL to
// /cover-from-url — the server downloads it; the client never writes a URL into cover_path.
function CoverCandidatePicker({ kind, id, title, onApply, onClose, api, notify }) {
  const [urls, setUrls] = useState(null);
  const [failed, setFailed] = useState(false);
  const [applying, setApplying] = useState(null);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const data = await api(`/library/${kind}/${id}/cover-candidates`);
        if (active) setUrls(data?.urls || []);
      } catch (searchError) {
        if (active) {
          setFailed(true);
          notify("Cover search failed", searchError.message, "ui_error");
        }
      }
    })();
    return () => { active = false; };
  }, [kind, id, api, notify]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
    >
      <section className="cover-candidate-dialog" role="dialog" aria-modal="true" aria-labelledby="cover-candidate-title">
        <div className="dialog-title-row">
          <div>
            <h2 id="cover-candidate-title">Choose cover art</h2>
            <p className="muted">{title}</p>
          </div>
          <button className="icon-button" onClick={onClose} title="Close"><X size={18} /></button>
        </div>
        {urls === null && !failed && <p className="user-note">Searching…</p>}
        {failed && <p className="user-note">Couldn't search for cover art.</p>}
        {urls?.length === 0 && <p className="user-note">No cover art found.</p>}
        {urls?.length > 0 && (
          <div className="cover-candidate-grid">
            {urls.map((url) => (
              <button
                key={url}
                type="button"
                className="cover-candidate"
                disabled={applying !== null}
                onClick={async () => {
                  setApplying(url);
                  const ok = await onApply(url);
                  setApplying(null);
                  if (ok) onClose();
                }}
              >
                <img src={url} alt="" loading="lazy" />
                {applying === url && <span className="cover-candidate-busy">Saving…</span>}
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

async function artistAutoLookup(field, draft, artistId, onCoverSearch) {
  // Cover art is no longer an auto-lookup *field*: it can't be written through the metadata draft
  // because cover_path is a filesystem path, not a URL. The Search button opens the candidate
  // picker instead, which posts the chosen URL to /cover-from-url for the server to download.
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
  // See artistAutoLookup: cover art goes through the candidate picker + /cover-from-url, never
  // through the metadata draft.
  if (field === "cover_path") return null;
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

// Build the audio stream URL for a playable object. Podcast episodes carry a `_streamPath`
// (e.g. "/podcasts/episodes/ID/stream"); library tracks fall back to the tracks endpoint.
function trackStreamUrl(track, apiKey) {
  const path = track?._streamPath || (track?.id ? `/library/tracks/${track.id}/stream` : "");
  if (!path || !apiKey) return "";
  return `${API_BASE}${path}?api_key=${encodeURIComponent(apiKey)}`;
}

function podcastCoverUrl(podcast, apiKey) {
  if (!podcast?.has_cover || !podcast?.id || !apiKey) return "";
  return `${API_BASE}/podcasts/${encodeURIComponent(podcast.id)}/cover?api_key=${encodeURIComponent(apiKey)}${coverCacheBust ? `&_cb=${coverCacheBust}` : ""}`;
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
  if (page === "Import/Add") return hasPermission(user, "import:run");
  if (page === "Wishlist") return hasPermission(user, "discover") || hasPermission(user, "wishlist:approve_all");
  if (page === "Approvals") return hasPermission(user, "wishlist:approve_all");
  if (page === "Task Queue") return hasPermission(user, "approvals:manage");
  if (page === "Playlists") return hasPermission(user, "playlists:manage");
  if (page === "Podcasts") return hasPermission(user, "podcasts:manage");
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
    ["Find duplicate files", "Find tracks with the same artist + album + title in multiple files; queue the extras for deletion, keeping the best copy of each song.", "check-duplicates", "tools:manage"],
    ["Check album covers", "Find albums without cover art and prepare images for review.", "check-album-covers", "tools:manage"],
    ["Check artist covers", "Find artists without cover art and prepare images for review.", "check-artist-covers", "tools:manage"],
    ["Refresh low-res covers", "Re-fetch high-resolution artwork for album covers stored at low resolution (upgrades old 250px/600px art in place).", "refresh-covers", "tools:manage"],
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
  ["Refresh low-res covers", "refresh-covers"],
  ["Check lyrics", "check-lyrics"],
  ["Check MusicBrainz info", "check-musicbrainz-ids"],
  ["Check audio content", "check-audio-content"],
  ["Check lossy tracks", "check-non-lossless"],
  ["Apply ReplayGain", "apply-replaygain"],
  ["Consolidate folders", "consolidate-folders"],
  ["Clear downloads", "clear-downloads"],
  ["Check podcasts for new episodes", "scan-podcasts"],
  ["Backup now", "backup"],
];

function triggerSummary(automation) {
  const { trigger_type, trigger_config } = automation;
  if (trigger_type === "webhook") return "Webhook";
  if (trigger_type === "shortcut") return "iOS Shortcut";
  if (trigger_type === "event") {
    const labels = { download_complete: "On download complete", wishlist_match: "On wishlist match", scan_complete: "On scan complete", podcast_episode_added: "On new podcast episode" };
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
                    <option value="shortcut">iOS Shortcut</option>
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

                {triggerType === "shortcut" && (
                  <p className="automation-stage-note">
                    Run this automation with Nudibranch’s Run Automation action in the iOS Shortcuts app or with Siri.
                  </p>
                )}

                {triggerType === "event" && (
                  <label className="automation-field">
                    <span>Event</span>
                    <select value={eventType} onChange={(e) => setEventType(e.target.value)}>
                      <option value="download_complete">Download complete</option>
                      <option value="wishlist_match">Wishlist match</option>
                      <option value="scan_complete">Scan complete</option>
                      <option value="podcast_episode_added">New podcast episode</option>
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
            <span style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
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
      {/* Password changes route by identity, not permission: /users/{id}/pin now refuses to change
          your own password (it never proved you knew it), so self always goes through /me/pin —
          including for an admin, who would otherwise get a 403 on their own card. */}
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
            onUpdatePin={(userId, password, currentPassword) =>
              userId === currentUser?.id
                ? onUpdateOwnPin(currentPassword, password)
                : onUpdatePin(userId, password)}
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
  const [currentPassword, setCurrentPassword] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isSelf = user.id === currentUser?.id;
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
        {isSelf && (
          <label>
            Current Password
            <input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} />
          </label>
        )}
        <label>
          New Password
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <button
          className="secondary compact"
          disabled={!password || (isSelf && !currentPassword)}
          onClick={() =>
            onUpdatePin(user.id, password, currentPassword).then(() => {
              setPassword("");
              setCurrentPassword("");
            })
          }
        >
          {isSelf ? "Change Password" : "Reset Password"}
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

const CONNECTION_LABELS = { connected: "Connected", error: "Unreachable", disabled: "Not configured", checking: "Checking…" };
function connectionLabel(status) {
  return CONNECTION_LABELS[status] || "Unknown";
}
function connectionStyle(status) {
  return { color: status === "connected" ? "#37c871" : status === "error" ? "#ff5a5a" : "var(--muted)" };
}

// Ten-band EQ editor. Collapsed and disabled by default — see EQ_DEFAULT_SETTINGS. Mirrors the
// iOS editor: a master switch, a preset picker (built-ins then your own), ten vertical band
// sliders, save/delete for custom presets, and the podcast-exemption toggle.
function EqualizerSettings({ equalizer, setEqualizer }) {
  const [open, setOpen] = useState(false);
  const [presetDraft, setPresetDraft] = useState("");
  const settings = equalizer || EQ_DEFAULT_SETTINGS;
  const gains = normalizeEqGains(settings.gains);
  const presets = [...EQ_BUILT_IN_PRESETS, ...(settings.customPresets || [])];
  const dirty = gains.some((value) => value !== 0);

  function update(patch) {
    setEqualizer((current) => ({ ...(current || EQ_DEFAULT_SETTINGS), ...patch }));
  }

  // Moving any band means the curve is no longer the named preset — drop the label, like iOS.
  function setBand(index, value) {
    const next = gains.slice();
    next[index] = Math.min(Math.max(Number(value) || 0, -EQ_GAIN_LIMIT), EQ_GAIN_LIMIT);
    update({ gains: next, presetName: null });
  }

  function applyPreset(name) {
    const preset = presets.find((entry) => entry.name === name);
    if (!preset) return;
    update({ gains: normalizeEqGains(preset.gains), presetName: preset.name });
  }

  function saveCustomPreset() {
    const name = presetDraft.trim();
    if (!name) return;
    // A built-in name is reserved; saving over your own custom preset just replaces it.
    if (EQ_BUILT_IN_PRESETS.some((preset) => preset.name.toLowerCase() === name.toLowerCase())) return;
    const others = (settings.customPresets || []).filter((preset) => preset.name.toLowerCase() !== name.toLowerCase());
    update({ customPresets: [...others, { name, gains, builtIn: false }], presetName: name });
    setPresetDraft("");
  }

  function deleteCustomPreset(name) {
    update({
      customPresets: (settings.customPresets || []).filter((preset) => preset.name !== name),
      presetName: settings.presetName === name ? null : settings.presetName,
    });
  }

  return (
    <section className="settings-section">
      <button type="button" className="settings-collapse-header" onClick={() => setOpen((value) => !value)}>
        {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        <h2>Equalizer</h2>
        <span className="muted">{settings.enabled ? (settings.presetName || "Custom") : "Off"}</span>
      </button>
      {open && (
        <div className="equalizer-panel">
          <label className="setting-row">
            <span>
              Enable equalizer
              <small>Ten-band EQ applied to playback on this device.</small>
            </span>
            <input
              type="checkbox"
              checked={Boolean(settings.enabled)}
              onChange={(event) => update({ enabled: event.target.checked })}
            />
          </label>

          <label className="setting-row">
            <span>Preset</span>
            <select
              value={settings.presetName || ""}
              onChange={(event) => applyPreset(event.target.value)}
              disabled={!settings.enabled}
            >
              <option value="">Custom</option>
              {presets.map((preset) => (
                <option key={(preset.builtIn ? "b:" : "c:") + preset.name} value={preset.name}>
                  {preset.name}{preset.builtIn ? "" : " (saved)"}
                </option>
              ))}
            </select>
          </label>

          <div className={`equalizer-bands${settings.enabled ? "" : " disabled"}`}>
            {EQ_FREQUENCIES.map((frequency, index) => (
              <div className="equalizer-band" key={frequency}>
                <span className="equalizer-band-gain">{gains[index] > 0 ? `+${gains[index]}` : gains[index]}</span>
                <input
                  className="equalizer-slider"
                  type="range"
                  orient="vertical"
                  min={-EQ_GAIN_LIMIT}
                  max={EQ_GAIN_LIMIT}
                  step="0.5"
                  value={gains[index]}
                  disabled={!settings.enabled}
                  aria-label={`${eqBandLabel(frequency)} hertz`}
                  onChange={(event) => setBand(index, event.target.value)}
                />
                <span className="equalizer-band-label">{eqBandLabel(frequency)}</span>
              </div>
            ))}
          </div>

          <div className="equalizer-actions">
            <button
              type="button"
              className="secondary compact"
              disabled={!settings.enabled || !dirty}
              onClick={() => update({ gains: EQ_FLAT_GAINS, presetName: "Flat" })}
            >
              Reset to flat
            </button>
            <input
              type="text"
              placeholder="Save current as…"
              value={presetDraft}
              disabled={!settings.enabled}
              onChange={(event) => setPresetDraft(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); saveCustomPreset(); } }}
            />
            <button
              type="button"
              className="secondary compact"
              disabled={!settings.enabled || !presetDraft.trim()}
              onClick={saveCustomPreset}
            >
              Save preset
            </button>
          </div>

          {(settings.customPresets || []).length > 0 && (
            <div className="equalizer-custom-list">
              {settings.customPresets.map((preset) => (
                <div className="equalizer-custom-row" key={preset.name}>
                  <button type="button" className="secondary compact" onClick={() => applyPreset(preset.name)}>
                    {preset.name}
                  </button>
                  <button
                    type="button"
                    className="icon-button"
                    title={`Delete ${preset.name}`}
                    onClick={() => deleteCustomPreset(preset.name)}
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}

          <label className="setting-row">
            <span>
              Apply to podcasts
              <small>Off by default, so spoken word keeps its own balance.</small>
            </span>
            <input
              type="checkbox"
              checked={Boolean(settings.appliesToPodcasts)}
              disabled={!settings.enabled}
              onChange={(event) => update({ appliesToPodcasts: event.target.checked })}
            />
          </label>
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
  equalizer,
  setEqualizer,
  onSaveSearchThreshold,
  user,
  apiKey,
  playlists,
  integrationSettings,
  onSaveIntegrations,
  onUploadYoutubeCookies,
  api,
  notify,
}) {
  const [showAttributions, setShowAttributions] = useState(false);
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

  if (showAttributions) {
    return <OpenSourceAttributions onBack={() => setShowAttributions(false)} />;
  }

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
      </section>
      <EqualizerSettings equalizer={equalizer} setEqualizer={setEqualizer} />
      {canManageSettings(user) && (
        <section className="settings-section">
          <h2>Integrations</h2>
          {[
            ["jellyfin_url", "Jellyfin URL"],
            ["jellyfin_api_key", "Jellyfin API key"],
            ["slskd_url", "slskd URL"],
            ["slskd_api_key", "slskd API key"],
            ["acoustid_api_key", "AcoustID API key"],
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
      {canManageSettings(user) && (
        <MatchTuningSettings
          api={api}
          notify={notify}
          integrationDraft={integrationDraft}
          setIntegrationDraft={setIntegrationDraft}
          onSaveIntegrations={onSaveIntegrations}
        />
      )}
      <SessionsPanel api={api} notify={notify} />
      {user?.is_admin && <SecuritySettings api={api} notify={notify} />}
      <footer className="settings-footer">
        Made by Poplel | <a href="https://poplel.xyz" target="_blank" rel="noreferrer">poplel.xyz</a>
        {" | "}<button type="button" onClick={() => setShowAttributions(true)}>Attributions</button>
      </footer>
    </div>
  );
}

function OpenSourceAttributions({ onBack }) {
  return (
    <div className="settings-grid attribution-page">
      <div className="album-detail-head">
        <button className="secondary compact" onClick={onBack}><ArrowLeft size={16} /> Back to Settings</button>
      </div>
      <header>
        <h1>Open source attributions</h1>
        <p className="muted">Nudibranch is built with the following directly included open-source projects.</p>
      </header>
      {Object.entries(OPEN_SOURCE_ATTRIBUTIONS).map(([group, entries]) => (
        <section className="settings-section attribution-group" key={group}>
          <h2>{group}</h2>
          <div className="attribution-list">
            {entries.map(([name, version, license, url]) => (
              <a className="attribution-row" href={url} target="_blank" rel="noreferrer" key={`${group}:${name}`}>
                <span><strong>{name}</strong><small>Version {version}</small></span>
                <span className="attribution-license">{license}</span>
              </a>
            ))}
          </div>
        </section>
      ))}
      <section className="settings-section attribution-group">
        <h2>Podcast directory</h2>
        <p className="muted">Podcast search uses Apple&apos;s public iTunes Search API. Apple and Apple Podcasts are trademarks of Apple Inc.</p>
        <a href="https://performance-partners.apple.com/search-api" target="_blank" rel="noreferrer">Apple iTunes Search API documentation</a>
      </section>
    </div>
  );
}

function MatchTuningSettings({ api, notify, integrationDraft, setIntegrationDraft, onSaveIntegrations }) {
  const [schema, setSchema] = useState([]);
  const [draft, setDraft] = useState({});
  const [open, setOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
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
      // The album-match confidence / folder tries / m4a toggle are integration settings; persist
      // them (the parent's handler shows its own toast) alongside the advanced matching weights.
      await onSaveIntegrations?.(integrationDraft);
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
    } catch (error) {
      notify?.("Download settings failed", error?.message || "Could not save download settings", "ui_error");
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

  const m4aChecked = !["false", "0", "no", "off"].includes(String(integrationDraft.allow_m4a_downloads ?? "true").toLowerCase());
  const ytdlpFallbackChecked = !["false", "0", "no", "off"].includes(String(integrationDraft.allow_ytdlp_fallback ?? "false").toLowerCase());
  const setIntegration = (key, value) => setIntegrationDraft((current) => ({ ...current, [key]: value }));

  return (
    <section className="settings-section">
      <h2 className="settings-collapse-header" onClick={() => setOpen((value) => !value)} style={{ cursor: "pointer" }}>
        {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />} Download settings
      </h2>
      {open && (
      <>
      <label className="setting-row integration-row">
        <span>slskd album match confidence</span>
        <input
          type="number"
          min="50"
          max="95"
          step="1"
          value={integrationDraft.slskd_album_match_threshold || ""}
          onChange={(event) => setIntegration("slskd_album_match_threshold", event.target.value)}
        />
      </label>
      <label className="setting-row integration-row">
        <span>Album folder tries</span>
        <input
          type="number"
          min="1"
          max="12"
          step="1"
          value={integrationDraft.slskd_album_folder_tries || ""}
          onChange={(event) => setIntegration("slskd_album_folder_tries", event.target.value)}
        />
      </label>
      <label className="setting-row integration-row">
        <span>Download m4a files (AAC/ALAC)</span>
        <input
          type="checkbox"
          checked={m4aChecked}
          onChange={(event) => setIntegration("allow_m4a_downloads", event.target.checked ? "true" : "false")}
        />
      </label>
      <label className="setting-row integration-row" title="When off, a track with no Soulseek candidate is left needing attention instead of downloading from YouTube. Soulseek results are still tried FLAC first, then other formats.">
        <span>Allow YouTube (yt-dlp) fallback</span>
        <input
          type="checkbox"
          checked={ytdlpFallbackChecked}
          onChange={(event) => setIntegration("allow_ytdlp_fallback", event.target.checked ? "true" : "false")}
        />
      </label>
      <h3 className="settings-collapse-header" onClick={() => setAdvancedOpen((value) => !value)} style={{ cursor: "pointer" }}>
        {advancedOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />} Advanced matching tuning
      </h3>
      {advancedOpen && (
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
              Reset matching to defaults
            </button>
          </div>
        </>
      )}
      <div className="settings-button-row">
        <button className="primary compact-button" type="button" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save download settings"}
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

/// Right-click menus.
//
// The web had no context menus at all, so "add this album to a playlist" meant opening the album,
// finding the inspector, and using a <select>. A row or card is where the user already is.
//
// ONE menu per screen, not one per row: `useMenuHost` hands back an `openMenu(event, items)` to
// hang off any row's `onContextMenu` plus a single element to render. Rows built inside a `.map`
// cannot own hooks of their own, and only one menu is ever open at a time anyway — so a screen
// that renders its rows inline hosts one menu for the whole list, and a row that is a real
// component (AlbumCard, PodcastCard) hosts its own with the same call.
//
// `container` is the element the menu portals into, and it must be a themed one: every colour in
// the app is a CSS custom property defined on `<main class="app">`, so a portal to `document.body`
// lands outside them and `background: var(--panel-strong)` resolves to nothing — which is what
// made the first version of this menu see-through. It defaults to the app root and is passed
// explicitly only by the popped-out player, whose container lives in another window's document.
function themedPortalHost(container) {
  return container || (typeof document === "undefined" ? null : document.querySelector("main.app")) || null;
}

// NOTHING in a menu is ever behind a scrollbar, at any depth. A menu is a list of choices, and a
// choice you have to scroll to find is a choice you don't know you have. Two rules follow:
//
//  - A long list is a SUBMENU off one row (`item.submenu`) rather than rows inline.
//  - A panel that would outgrow the window PAGES: it shows what fits and swaps its contents for
//    the next batch behind a "Next page ›" row, with a "‹ Back" row to return.
//
// Both rules are properties of `ContextMenuPanel`, which draws the root menu and every nested one
// alike — so nesting deeper cannot produce a panel that runs off the screen or needs scrolling,
// and no level needs its own special case. Nesting is capped at MENU_MAX_DEPTH panels; today only
// the playlists use a second level, and the cap is headroom rather than a description.
const MENU_MAX_DEPTH = 5;
// 6px padding top and bottom + 1px border each side, matching .context-menu.
const MENU_CHROME = 14;
// 1px rule + 5px margin each side, matching .context-menu-separator.
const MENU_SEPARATOR_HEIGHT = 11;
const MENU_MARGIN = 8;

function useMenuHost(container) {
  const [menu, setMenu] = useState(null);
  // Resolved only while a menu is open: a grid calls this hook once per card, and a querySelector
  // per card per render is a cost for nothing when nothing is showing.
  const host = menu ? themedPortalHost(container) : null;
  const close = useCallback(() => setMenu(null), []);
  // Built once so a row can spread it without re-rendering the list on every parent render.
  const openMenu = useCallback((event, items) => {
    const rows = (items || []).filter(Boolean);
    if (rows.length === 0) return;
    event.preventDefault();
    // Nested rows (a track inside an album branch) must open the innermost menu, not both.
    event.stopPropagation();
    setMenu({ x: event.clientX, y: event.clientY, items: rows });
  }, []);
  return [openMenu, <ContextMenu menu={menu} host={host} onClose={close} />];
}

function ContextMenu({ menu, host, onClose }) {
  // `path[depth]` is the item opened at that depth, and the panel it produced sits at depth + 1.
  const [path, setPath] = useState([]);
  const panels = useRef([]);
  const registerPanel = useCallback((depth, node) => { panels.current[depth] = node; }, []);

  useEffect(() => { setPath([]); }, [menu]);

  useEffect(() => {
    const view = host?.ownerDocument?.defaultView;
    if (!menu || !view) return undefined;
    // ⚠ Dismissal tests CONTAINMENT across every open panel; it must not rely on stopPropagation
    // inside them. This listener is on the window in the capture phase, so it runs before React's
    // own handler at the root container — closing on any mousedown unmounted the item before its
    // click could be dispatched at all, which left every row of the menu inert.
    const insideMenu = (target) => panels.current.some((node) => node?.contains(target));
    const onMouseDown = (event) => { if (!insideMenu(event.target)) onClose(); };
    const onKeyDown = (event) => { if (event.key === "Escape") onClose(); };
    view.addEventListener("mousedown", onMouseDown, true);
    view.addEventListener("scroll", onClose, true);
    view.addEventListener("keydown", onKeyDown);
    return () => {
      view.removeEventListener("mousedown", onMouseDown, true);
      view.removeEventListener("scroll", onClose, true);
      view.removeEventListener("keydown", onKeyDown);
    };
  }, [menu, host, onClose]);

  const openAt = useCallback((depth, step) => {
    setPath((current) => {
      // A row with no submenu closes everything deeper; the cap is enforced here as well as in the
      // render, so a too-deep row never highlights as though it had opened something.
      if (!step || depth + 1 >= MENU_MAX_DEPTH) return current.slice(0, depth);
      return [...current.slice(0, depth), step];
    });
  }, []);

  const select = useCallback((item) => { onClose(); item.action?.(); }, [onClose]);

  if (!menu || !host) return null;

  // Walk the open chain, stopping at the first level with nothing to open or at the cap.
  const levels = [{ items: menu.items }];
  for (const step of path) {
    if (levels.length >= MENU_MAX_DEPTH) break;
    const parent = levels[levels.length - 1].items[step.index];
    if (!parent?.submenu?.length) break;
    levels.push({ items: parent.submenu, anchor: step.anchor });
  }

  return createPortal(
    <>
      {levels.map((level, depth) => (
        <ContextMenuPanel
          key={depth}
          depth={depth}
          items={level.items}
          at={depth === 0 ? menu : null}
          anchor={level.anchor || null}
          activeIndex={depth < path.length ? path[depth].index : null}
          onActivate={openAt}
          onSelect={select}
          onRegister={registerPanel}
        />
      ))}
    </>,
    host,
  );
}

// One panel of a menu — the root or any nested level. It measures itself, clamps itself into the
// window, and pages its own contents, so every level gets those properties for free.
function ContextMenuPanel({ depth, items, at, anchor, activeIndex, onActivate, onSelect, onRegister }) {
  const nodeRef = useRef(null);
  const [page, setPage] = useState(0);
  const [layout, setLayout] = useState(null);

  // A panel reused for a different parent row (hovering along a row of submenus) starts at its
  // first page rather than wherever the previous list was left.
  useLayoutEffect(() => { setPage(0); }, [items, anchor]);

  useLayoutEffect(() => {
    const node = nodeRef.current;
    if (!node) return;
    const view = node.ownerDocument?.defaultView || window;
    // Measured from a real row, never a paging row — those are set in a smaller type.
    const row = node.querySelector(".context-menu-body .context-menu-item");
    const rowHeight = row ? row.getBoundingClientRect().height : 30;
    const width = node.getBoundingClientRect().width;
    const available = view.innerHeight - MENU_MARGIN * 2 - MENU_CHROME;
    const maxRows = Math.max(1, Math.floor(available / rowHeight));
    // Paging costs a Back row, a Next row and their separators. Both are charged for on every page
    // — and both are always drawn, greyed out where they don't apply — so the page size and the
    // panel's own height are identical on every page: the frame stays put and only its contents
    // swap. Separators and headers are counted as full rows, which only ever over-estimates; they
    // are shorter than a row, so a page can come out short but never too tall.
    const navHeight = rowHeight * 2 + MENU_SEPARATOR_HEIGHT * 2;
    const paged = items.length > maxRows;
    const perPage = paged ? Math.max(1, Math.floor((available - navHeight) / rowHeight)) : items.length;
    const height = MENU_CHROME + perPage * rowHeight + (paged ? navHeight : 0);
    let left;
    let top;
    if (anchor) {
      // Beside the parent row, flipping to its left when the window has no room on the right. The
      // 2px overlap is what lets the pointer travel from the row into the panel without leaving
      // both at once.
      left = anchor.right - 2;
      if (left + width > view.innerWidth - MENU_MARGIN) left = anchor.left - width + 2;
      top = anchor.top - 6;
    } else {
      left = at.x;
      top = at.y;
    }
    setLayout({
      left: Math.max(MENU_MARGIN, Math.min(left, Math.max(MENU_MARGIN, view.innerWidth - width - MENU_MARGIN))),
      top: Math.max(MENU_MARGIN, Math.min(top, Math.max(MENU_MARGIN, view.innerHeight - height - MENU_MARGIN))),
      perPage,
      // Only a paged panel is pinned to a height; an ordinary menu sizes to its rows.
      height: paged ? height : undefined,
    });
  }, [items, anchor, at?.x, at?.y]);

  const perPage = layout?.perPage ?? items.length;
  const pageCount = Math.max(1, Math.ceil(items.length / perPage));
  const paged = pageCount > 1;
  const start = page * perPage;
  const visible = items.slice(start, start + perPage);
  const canNest = depth + 1 < MENU_MAX_DEPTH;

  return (
    <div
      ref={(node) => { nodeRef.current = node; onRegister(depth, node); }}
      className={`context-menu${paged ? " context-menu-paged" : ""}`}
      role="menu"
      // Hidden for the one frame between rendering at the raw anchor point and measuring, so a
      // panel never visibly jumps into place near an edge or flashes its unpaged contents.
      style={{
        left: layout?.left ?? (anchor ? anchor.right : at.x),
        top: layout?.top ?? (anchor ? anchor.top : at.y),
        height: layout?.height,
        visibility: layout ? "visible" : "hidden",
      }}
      onContextMenu={(event) => event.preventDefault()}
    >
      {paged && (
        <>
          {/* Drawn on every page, disabled on the first, so the frame is identical throughout. */}
          <button
            type="button"
            className="context-menu-item context-menu-page"
            disabled={page === 0}
            onClick={() => setPage(page - 1)}
          >
            <ChevronLeft size={13} className="context-menu-chevron" />
            <span className="context-menu-label">Back</span>
          </button>
          <div className="context-menu-separator" />
        </>
      )}
      {/* The slack on a short last page sits inside this block, so the nav rows never move. */}
      <div className="context-menu-body">
        {visible.map((item, offset) => {
          const index = start + offset;
          if (item.separator) return <div className="context-menu-separator" key={`sep-${index}`} />;
          if (item.header) return <div className="context-menu-header" key={`head-${index}`}>{item.label}</div>;
          const nests = Boolean(item.submenu?.length) && canNest;
          return (
            <button
              key={`${item.label}-${index}`}
              type="button"
              role="menuitem"
              className={`context-menu-item${item.danger ? " danger" : ""}${nests ? " has-submenu" : ""}${activeIndex === index ? " open" : ""}`}
              disabled={item.disabled}
              aria-haspopup={nests ? "menu" : undefined}
              aria-expanded={nests ? activeIndex === index : undefined}
              // Hover opens and switches, click opens too — a submenu you can only reach by
              // hovering is unreachable to anyone who clicks first.
              onMouseEnter={(event) => onActivate(depth, nests ? { index, anchor: event.currentTarget.getBoundingClientRect() } : null)}
              onClick={(event) => {
                if (nests) { onActivate(depth, { index, anchor: event.currentTarget.getBoundingClientRect() }); return; }
                onSelect(item);
              }}
            >
              <span className="context-menu-label">{item.label}</span>
              {nests && <ChevronRight size={13} className="context-menu-chevron" />}
            </button>
          );
        })}
      </div>
      {paged && (
        <>
          <div className="context-menu-separator" />
          <button
            type="button"
            className="context-menu-item context-menu-page"
            disabled={page >= pageCount - 1}
            onClick={() => setPage(page + 1)}
          >
            <span className="context-menu-label">Next page</span>
            <ChevronRight size={13} className="context-menu-chevron" />
          </button>
        </>
      )}
    </div>
  );
}

/// A (...) that opens the same menu the surface's right-click gesture does.
///
/// A detail page has no card to right-click, and a pointer-only user should not have to know the
/// gesture exists to reach the actions — so every collection that carries a context menu carries
/// this too, with the same rows. `openMenu` reads the click's coordinates, so a button click
/// positions the menu exactly as a right-click would.
function OverflowMenuButton({
  openMenu,
  items,
  className = "secondary",
  label = "More actions",
  // The devices button uses these to match the apps: its own glyph rather than an ellipsis, an
  // active state while playback is on another device, and it stays visible with nothing to offer
  // (the apps show it unconditionally and say so inside).
  icon: Icon = MoreHorizontal,
  iconSize = 15,
  active = false,
  alwaysShow = false,
}) {
  const rows = (items || []).filter(Boolean);
  if (rows.length === 0 && !alwaysShow) return null;
  return (
    <button
      type="button"
      className={`${className}${active ? " active" : ""}`}
      title={label}
      aria-label={label}
      aria-haspopup="menu"
      onClick={(event) => openMenu(event, rows)}
    >
      <Icon size={iconSize} />
    </button>
  );
}

// The shared "Play / Add to queue / … / Add to playlist" rows for anything playable. `resolve`
// returns the tracks a playlist row would add, and is only ever called when such a row is chosen —
// a grid must not fetch a track list per card just to build menus it may never show.
// `afterPlay` rows are other ways to start playback ("Play from here") and belong next to Play;
// `extra` rows are everything else and follow the queue row.
function playbackMenuItems({ onPlay, onQueue, playLabel = "Play", queueLabel = "Add to queue", afterPlay = [], extra = [], playlists, onAddToPlaylist, resolve }) {
  const items = [];
  if (onPlay) items.push({ label: playLabel, action: onPlay });
  for (const row of afterPlay) if (row) items.push(row);
  if (onQueue) items.push({ label: queueLabel, action: onQueue });
  for (const row of extra) if (row) items.push(row);
  if (onAddToPlaylist && resolve && (playlists || []).length > 0) {
    items.push({ separator: true });
    // A submenu, not inline rows: a library with forty playlists made the menu taller than the
    // window, and the rows past the fold were unreachable.
    items.push({
      label: "Add to playlist",
      submenu: playlists.map((playlist) => ({
        label: playlist.name,
        action: async () => {
          const tracks = await resolve();
          const ids = (tracks || []).map((track) => track.id).filter(Boolean);
          if (ids.length) onAddToPlaylist(playlist.id, ids);
        },
      })),
    });
  }
  return items;
}

function AlbumCard({ album, apiKey, onPlay, onPlayNext, onQueue, onOpen, pinned, onTogglePin, playlists, onAddToPlaylist, onResolveTracks }) {
  const cover = albumCoverUrl(album, apiKey);
  const subtitle = album.artist || album.artist_name || "";
  const [openMenu, menuElement] = useMenuHost();
  const menuItems = playbackMenuItems({
    playlists,
    onAddToPlaylist,
    resolve: () => onResolveTracks?.(album),
    onPlay: onPlay && (() => onPlay(album)),
    onQueue: onQueue && (() => onQueue(album)),
    afterPlay: [onPlayNext && { label: "Play next", action: () => onPlayNext(album) }],
    extra: [
      onOpen && { label: "Open album", action: () => onOpen(album) },
      onTogglePin && { label: pinned ? "Unpin from Home" : "Pin to Home", action: () => onTogglePin(album) },
    ],
  });
  return (
    <div
      className="album-card"
      title={`${album.title} — ${subtitle}`}
      onContextMenu={(event) => openMenu(event, menuItems)}
    >
      {menuElement}
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

function ArtistCard({ artist, apiKey, onPlay, onPlayNext, onQueue, onOpen, pinned, onTogglePin, playlists, onAddToPlaylist, onResolveTracks }) {
  const cover = artistCoverUrl(artist, apiKey);
  const [openMenu, menuElement] = useMenuHost();
  const menuItems = playbackMenuItems({
    playlists,
    onAddToPlaylist,
    resolve: () => onResolveTracks?.(artist),
    onPlay: onPlay && (() => onPlay(artist)),
    onQueue: onQueue && (() => onQueue(artist)),
    afterPlay: [onPlayNext && { label: "Play next", action: () => onPlayNext(artist) }],
    extra: [
      onOpen && { label: "Open artist", action: () => onOpen(artist) },
      onTogglePin && { label: pinned ? "Unpin from Home" : "Pin to Home", action: () => onTogglePin(artist) },
    ],
  });
  return (
    <div
      className="album-card artist-card"
      title={artist.name}
      onContextMenu={(event) => openMenu(event, menuItems)}
    >
      {menuElement}
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

function AlbumDetailPage({ detail, api, apiKey, onBack, onPlayAlbum, onPlayAlbumNext, onQueueAlbum, onPlayTracks, onPlayNextTracks, onQueueTracks, pinned, onTogglePin, playlists, onAddToPlaylist }) {
  const [tracks, setTracks] = useState(null);
  const [openMenu, menuElement] = useMenuHost();
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
  const viewCtx = { openMenu, onPlay: onPlayTracks, onPlayNext: onPlayNextTracks, onQueue: onQueueTracks, canEditMetadata: false, canRemoveLibrary: false, canUsePlaylists: false };
  const albumObj = { id: detail.id, title: detail.title, _coverUrl: cover, tracks: tracks || [] };
  // The same rows the album's card offers on right-click, minus "Open album" — you are in it. The
  // tracks are already loaded here, so "Add to playlist" needs no fetch of its own.
  const overflowItems = playbackMenuItems({
    onPlay: () => onPlayAlbum(detail),
    onQueue: () => onQueueAlbum(detail),
    afterPlay: [onPlayAlbumNext && { label: "Play next", action: () => onPlayAlbumNext(detail) }],
    extra: [
      onTogglePin && {
        label: pinned ? "Unpin from Home" : "Pin to Home",
        action: () => onTogglePin(detail),
      },
    ],
    playlists,
    onAddToPlaylist,
    resolve: () => tracks || [],
  });
  const artistObj = { name: detail.artist_name };
  return (
    <div className="album-detail-overlay">
      {menuElement}
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
            <OverflowMenuButton openMenu={openMenu} items={overflowItems} />
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

function ArtistDetailPage({ detail, api, apiKey, onBack, onPlayArtist, onPlayArtistNext, onQueueArtist, onPlayTracks, onPlayNextTracks, onQueueTracks, onOpenAlbum, pinned, onTogglePin, library, playlists, onAddToPlaylist }) {
  const [openAlbums, setOpenAlbums] = useState(() => new Set());
  const [openMenu, menuElement] = useMenuHost();
  const node = useMemo(() => (library || []).find((a) => a.id === detail.id), [library, detail.id]);
  const albums = useMemo(() => {
    if (!node) return [];
    return node.albums
      .filter((al) => (al.tracks?.length || 0) > 0)
      .map((al) => ({ ...al, _coverUrl: albumCoverUrl(al, apiKey) }))
      .sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  }, [node, apiKey]);
  // Resolved from the local tree rather than the network — the artist's albums are already here.
  const overflowItems = playbackMenuItems({
    onPlay: () => onPlayArtist(detail),
    onQueue: () => onQueueArtist(detail),
    afterPlay: [onPlayArtistNext && { label: "Play next", action: () => onPlayArtistNext(detail) }],
    extra: [
      onTogglePin && {
        label: pinned ? "Unpin from Home" : "Pin to Home",
        action: () => onTogglePin(detail),
      },
    ],
    playlists,
    onAddToPlaylist,
    resolve: () => (node ? artistTracks(node) : []),
  });
  const cover = artistCoverUrl({ id: detail.id, cover_path: detail.cover_path }, apiKey)
    || `${API_BASE}/library/artists/${encodeURIComponent(detail.id)}/cover?api_key=${encodeURIComponent(apiKey)}`;
  const viewCtx = {
    openMenu,
    onPlay: onPlayTracks, onPlayNext: onPlayNextTracks, onQueue: onQueueTracks,
    canEditMetadata: false, canRemoveLibrary: false, canUsePlaylists: false,
    openAlbums, setOpenAlbums,
    onOpenAlbum,
  };
  return (
    <div className="album-detail-overlay">
      {menuElement}
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
            <OverflowMenuButton openMenu={openMenu} items={overflowItems} />
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

// Home rows, in their default web order. The same set of rows as the iOS home screen, but the
// ORDER is stored separately per client: iOS owns `home_layout`, web owns `home_layout_web`, so
// rearranging one never disturbs the other. (The server treats both blobs as opaque.)
const HOME_ROW_IDS = [
  "chips",
  "playlists",
  "artists",
  "albums",
  "podcasts",
  "recently_added",
  "recent_plays",
  "recently_approved",
];

const HOME_ROW_TITLES = {
  chips: "Quick play",
  playlists: "Favorites & pinned playlists",
  artists: "Pinned artists",
  albums: "Pinned albums",
  podcasts: "Pinned podcasts",
  recently_added: "Recently added",
  recent_plays: "Recent plays",
  recently_approved: "Recently approved",
};

// Merge a saved order with the current row set: unknown ids (a row removed in a later build) are
// dropped, and rows the saved order predates are appended rather than vanishing — so adding a new
// home row never makes it invisible to everyone who has already arranged their home screen.
function resolveHomeOrder(stored) {
  const saved = Array.isArray(stored) ? stored.filter((id) => HOME_ROW_IDS.includes(id)) : [];
  const seen = new Set(saved);
  return [...saved, ...HOME_ROW_IDS.filter((id) => !seen.has(id))];
}

function HomeView({ api, apiKey, onPlayAlbum, onPlayAlbumNext, onQueueAlbum, onPlayPlaylist, onOpenAlbum, onPlayArtist, onPlayArtistNext, onOpenArtist, onQueueArtist, onPlayTracks, onPlayNextTracks, onQueueTracks, pinnedAlbumIds, onTogglePinAlbum, pinnedArtistIds, onTogglePinArtist, pinnedPodcastIds, onTogglePinPodcast, onOpenPodcast, homeVersion, onUnpinPlaylist, onPlayAll, onShuffleAll, homeLayout, onSaveHomeLayout, playlists, onAddToPlaylist }) {
  // On demand only — a Home row of pinned albums must not fetch a track list per card.
  const albumTrackRows = useCallback(async (album) => {
    const data = await api(`/library/tracks?album_id=${encodeURIComponent(album.id)}&page_size=500`);
    return data?.items || [];
  }, [api]);
  const artistTrackRows = useCallback(async (artist) => {
    const albums = await api(`/library/albums?artist_id=${encodeURIComponent(artist.id)}&page_size=500`);
    let rows = [];
    for (const album of albums?.items || []) {
      const data = await api(`/library/tracks?album_id=${encodeURIComponent(album.id)}&page_size=500`);
      rows = rows.concat(data?.items || []);
    }
    return rows;
  }, [api]);
  const [home, setHome] = useState(null);
  const [openMenu, menuElement] = useMenuHost();
  const [order, setOrder] = useState(() => resolveHomeOrder(homeLayout));
  const [dragId, setDragId] = useState(null);
  const [dropId, setDropId] = useState(null);
  // Re-resolve when the saved layout arrives (Home can render before /me settles) or changes.
  useEffect(() => { setOrder(resolveHomeOrder(homeLayout)); }, [homeLayout]);

  // Optimistic: reorder locally, then persist. A failed save leaves the on-screen order as the
  // user left it for this session rather than snapping back mid-drag.
  function moveRow(targetId) {
    if (!dragId || dragId === targetId) { setDragId(null); setDropId(null); return; }
    const next = order.filter((id) => id !== dragId);
    const at = next.indexOf(targetId);
    next.splice(at < 0 ? next.length : at, 0, dragId);
    setOrder(next);
    setDragId(null);
    setDropId(null);
    onSaveHomeLayout?.(next);
  }

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
      .catch(() => { if (active) setHome({ recently_added: [], recently_approved: [], recent_plays: [], favorites: null, pinned_playlists: [], pinned_albums: [], pinned_artists: [], pinned_podcasts: [] }); });
    return () => { active = false; };
  }, [api, homeVersion]);

  if (!home) return <div className="home-view"><p className="muted">Loading…</p></div>;

  const fmt = (iso) => (iso ? new Date(iso).toLocaleDateString() : "");

  async function playPinnedPodcast(podcast) {
    const data = await api(`/podcasts/${encodeURIComponent(podcast.id)}/episodes?page=1&page_size=500`);
    const queue = podcastPlayQueue(data?.items || [], podcast, apiKey);
    if (queue.length) onPlayTracks(queue, { keepLead: false });
  }

  async function queuePinnedPodcast(podcast) {
    const data = await api(`/podcasts/${encodeURIComponent(podcast.id)}/episodes?page=1&page_size=500`);
    const queue = (data?.items || []).map((episode) => episodeToPlayable(episode, podcast, apiKey));
    if (queue.length) onQueueTracks(queue);
  }

  // Only the episode the show resumes to — see podcastResumeEpisode.
  async function playPinnedPodcastNext(podcast) {
    const data = await api(`/podcasts/${encodeURIComponent(podcast.id)}/episodes?page=1&page_size=500`);
    const lead = podcastResumeEpisode(data?.items || []);
    if (lead) onPlayNextTracks?.([episodeToPlayable(lead, podcast, apiKey)]);
  }

  // Row content, keyed by row id. Returning null hides a row entirely (empty pinned grids), but
  // the id stays in `order` so its position survives until something is pinned again.
  function renderRow(id) {
    switch (id) {
      case "chips":
        return (
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
          </div>
        );
      case "playlists":
        return (
          <div className="home-pin-row">
            <button className="home-pin-card" onClick={() => onPlayPlaylist("favorites")}>
              <Heart size={16} />
              <span className="home-list-main">Favorites</span>
              <span className="home-list-sub">{home.favorites ? `${home.favorites.track_count} tracks` : "—"}</span>
            </button>
            {home.pinned_playlists.map((p) => (
              <div
                key={p.playlist_id}
                className="home-pin-card-wrap"
                onContextMenu={(event) => openMenu(event, [
                  { label: "Play", action: () => onPlayPlaylist(p.playlist_id) },
                  onUnpinPlaylist && { label: "Unpin from Home", action: () => onUnpinPlaylist(p.playlist_id) },
                ].filter(Boolean))}
              >
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
        );
      case "artists":
        if (!home.pinned_artists?.length) return null;
        return (
          <div className="home-album-grid home-pinned-grid">
            {home.pinned_artists.map((ar) => (
              <ArtistCard key={ar.id} artist={ar} apiKey={apiKey} onPlay={onPlayArtist} onPlayNext={onPlayArtistNext} onQueue={onQueueArtist} onOpen={onOpenArtist} pinned={pinnedArtistIds?.has(ar.id)} onTogglePin={onTogglePinArtist} playlists={playlists} onAddToPlaylist={onAddToPlaylist} onResolveTracks={artistTrackRows} />
            ))}
          </div>
        );
      case "albums":
        if (!home.pinned_albums?.length) return null;
        return (
          <div className="home-album-grid home-pinned-grid">
            {home.pinned_albums.map((al) => (
              <AlbumCard key={al.id} album={al} apiKey={apiKey} onPlay={onPlayAlbum} onPlayNext={onPlayAlbumNext} onQueue={onQueueAlbum} onOpen={onOpenAlbum} pinned={pinnedAlbumIds?.has(al.id)} onTogglePin={onTogglePinAlbum} playlists={playlists} onAddToPlaylist={onAddToPlaylist} onResolveTracks={albumTrackRows} />
            ))}
          </div>
        );
      case "podcasts":
        if (!home.pinned_podcasts?.length) return null;
        return (
          <div className="home-album-grid home-pinned-grid">
            {home.pinned_podcasts.map((podcast) => (
              <PodcastCard
                key={podcast.id}
                podcast={podcast}
                apiKey={apiKey}
                onPlay={() => playPinnedPodcast(podcast)}
                onPlayNext={() => playPinnedPodcastNext(podcast)}
                onQueue={() => queuePinnedPodcast(podcast)}
                onOpen={() => onOpenPodcast?.(podcast)}
                pinned={pinnedPodcastIds?.has(podcast.id)}
                onTogglePin={() => onTogglePinPodcast?.(podcast)}
              />
            ))}
          </div>
        );
      case "recently_added":
        return home.recently_added.length === 0 ? (
          <p className="muted">Nothing added yet.</p>
        ) : (
          <div className="home-album-grid">
            {home.recently_added.map((al) => (
              <AlbumCard key={al.id} album={al} apiKey={apiKey} onPlay={onPlayAlbum} onPlayNext={onPlayAlbumNext} onQueue={onQueueAlbum} onOpen={onOpenAlbum} pinned={pinnedAlbumIds?.has(al.id)} onTogglePin={onTogglePinAlbum} playlists={playlists} onAddToPlaylist={onAddToPlaylist} onResolveTracks={albumTrackRows} />
            ))}
          </div>
        );
      case "recent_plays":
        return home.recent_plays.length === 0 ? (
          <p className="muted">No plays yet.</p>
        ) : (
          <ul className="home-list">
            {home.recent_plays.map((p, i) => (
              <li
                key={`${p.track_id}-${i}`}
                className="home-list-row"
                onContextMenu={p.track_id ? (event) => openMenu(event, playbackMenuItems({
                  onPlay: onPlayTracks && (() => onPlayTracks([recentToTrack(p)])),
                  onQueue: onQueueTracks && (() => onQueueTracks([recentToTrack(p)])),
                  afterPlay: [onPlayNextTracks && { label: "Play next", action: () => onPlayNextTracks([recentToTrack(p)]) }],
                  extra: [p.album_id && onOpenAlbum && { label: "Open album", action: () => onOpenAlbum({ id: p.album_id, title: p.album, artist_name: p.artist }) }],
                  playlists,
                  onAddToPlaylist,
                  resolve: () => [{ id: p.track_id }],
                })) : undefined}
              >
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
        );
      case "recently_approved":
        return home.recently_approved.length === 0 ? (
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
        );
      default:
        return null;
    }
  }

  return (
    <div className="home-view">
      {menuElement}
      {order.map((id) => {
        const content = renderRow(id);
        if (content === null) return null;
        return (
          <section
            key={id}
            className={`home-section home-row${dragId === id ? " dragging" : ""}${dropId === id ? " drop-target" : ""}`}
            onDragOver={(event) => { if (dragId) { event.preventDefault(); setDropId(id); } }}
            onDragLeave={() => setDropId((current) => (current === id ? null : current))}
            onDrop={(event) => { event.preventDefault(); moveRow(id); }}
          >
            <div className="home-row-header">
              {/* Only the handle is draggable, so selecting text or clicking a card inside the
                  row never starts a drag. */}
              <span
                className="home-row-handle"
                draggable
                title="Drag to reorder"
                aria-label={`Reorder ${HOME_ROW_TITLES[id]}`}
                onDragStart={(event) => { setDragId(id); event.dataTransfer.effectAllowed = "move"; }}
                onDragEnd={() => { setDragId(null); setDropId(null); }}
              >
                <GripVertical size={15} />
              </span>
              <h2>{HOME_ROW_TITLES[id]}</h2>
            </div>
            {content}
          </section>
        );
      })}
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

function ConnectionsStatus({ api, user }) {
  const [connections, setConnections] = useState({ slskd: "checking", jellyfin: "checking" });
  useEffect(() => {
    let active = true;
    const load = () => api("/settings/connections").then((data) => { if (active && data) setConnections(data); }).catch(() => {});
    load();
    const id = setInterval(load, 20000);
    return () => { active = false; clearInterval(id); };
  }, []);
  return (
    <div className="inspector-section">
      <div className="inspector-section-label">Status</div>
      <div className="status-list">
        <span>User</span>
        <strong>{user?.display_name || "Signed in"}</strong>
        <span>API</span>
        <strong style={{ color: "#37c871" }}>Connected</strong>
        <span>slskd</span>
        <strong style={connectionStyle(connections.slskd)}>{connectionLabel(connections.slskd)}</strong>
        <span>Jellyfin</span>
        <strong style={connectionStyle(connections.jellyfin)}>{connectionLabel(connections.jellyfin)}</strong>
      </div>
    </div>
  );
}

function Inspector({
  page,
  mobileOpen,
  onCloseMobile,
  api,
  user,
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
  approvalsActions,
  playlistActions,
  podcastActions,
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
    user,
    playlists,
    queueItemCount,
    queueSelectionCount,
    tasks,
    mappingSyncStats,
  });
  return (
    <aside className={`panel inspector${mobileOpen ? " mobile-open" : ""}`}>
      <div className="inspector-mobile-head">
        <h2>Inspector</h2>
        <button className="icon-button mobile-inspector-close" onClick={onCloseMobile} title="Close" aria-label="Close">
          <X size={18} />
        </button>
      </div>
      {page === "Library" && <LibraryTopStats api={api} />}
      {page === "Automations" && <AutomationsInspector api={api} />}
      {page === "Podcasts" && podcastActions && (
        <>
          {podcastActions.mode === "detail" ? (
            <>
              <div className="inspector-actions">
                <button className="secondary" onClick={podcastActions.onBack}><ArrowLeft size={16} /> All podcasts</button>
                <button className="secondary" onClick={podcastActions.onScan} disabled={podcastActions.scanning}>
                  <RefreshCw size={16} className={podcastActions.scanning ? "spin-icon" : ""} /> {podcastActions.scanning ? "Checking…" : "Check for new"}
                </button>
              </div>
              <div className="inspector-actions">
                <button className="secondary" onClick={podcastActions.onMarkAllToggle} disabled={podcastActions.markAllBusy}>
                  <CheckCircle size={16} /> {podcastActions.allPlayed ? "Mark all unplayed" : "Mark all played"}
                </button>
                {podcastActions.canMarkBeforeOldest && (
                  <button className="secondary" onClick={podcastActions.onMarkBeforeOldestPlayed} disabled={podcastActions.markBeforeOldestBusy}>
                    <Check size={16} /> Mark before oldest played
                  </button>
                )}
              </div>
              {/* Nothing to configure here any more: episodes stream from the publisher, and
                  keeping them offline is a per-device setting the native apps own. */}
            </>
          ) : (
            <div className="inspector-actions">
              <button className="primary" onClick={podcastActions.onAdd}><Plus size={16} /> Add podcast</button>
              <button className="secondary" onClick={podcastActions.onScan} disabled={podcastActions.scanning}>
                <RefreshCw size={16} className={podcastActions.scanning ? "spin-icon" : ""} /> {podcastActions.scanning ? "Checking…" : "Check all feeds"}
              </button>
            </div>
          )}
          <div className="metadata-grid inspector-stats">
            {podcastActions.mode !== "detail" && <><label>Podcasts</label><strong>{podcastActions.podcastCount || 0}</strong></>}
            <label>Episodes</label><strong>{podcastActions.episodeCount || 0}</strong>
            {podcastActions.mode === "detail" && <><label>Unplayed</label><strong>{podcastActions.unplayedCount || 0}</strong></>}
          </div>
        </>
      )}
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
      {page === "Approvals" && approvalsActions && (
        <div className="inspector-actions">
          <button className="primary" onClick={approvalsActions.onSubmitSelected} disabled={approvalsActions.selectedCount === 0}>
            <ListChecks size={16} />
            Add selected to task queue
          </button>
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
      {page === "Tools" && <ConnectionsStatus api={api} user={user} />}
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
  user = null,
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
    const own = user ? wishlist.filter((item) => item.user_id === user.id) : wishlist;
    return { summary: "", rows: musicStatRows(countWishlistMusic(own)) };
  }
  if (page === "Approvals") {
    const others = user ? wishlist.filter((item) => item.user_id !== user.id) : wishlist;
    return { summary: "", rows: musicStatRows(countWishlistMusic(others)) };
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
      const s = stallsRef.current;

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
        <strong style={{ fontSize: 11, letterSpacing: 0.3, flex: 1 }}>Stats for geeks</strong>
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
  headerActions,
  controlRef,
  equalizer,
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
  deviceMenuItems,
  onEnded,
  onSkipBack,
  onSkipForward,
  shuffle = false,
  repeat = "off",
  onToggleShuffle,
  onCycleRepeat,
  playlists,
  onAddToPlaylist,
  onPlaybackState,
  onPlaybackError,
  onDockChange,
  onClose,
  crossfadeDuration = 0.5,
  apiKey,
  diagnostics = false,
  // Non-null means the audio is coming out of ANOTHER of this account's sessions. The audio engine
  // below simply stays idle; everything the user sees and touches is resolved from these instead.
  remote = null,
  remotePosition = 0,
  remoteQueue = [],
  onRemoteCommand,
  onRemoteMode,
  onRemoteQueueJump,
  onRemoteLive,
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
      // ONE equalizer chain shared by both buffers, sitting after the per-element ReplayGain
      // nodes and before the limiter: element gain(A|B) → EQ band 1 → … → band 10 → limiter.
      // Shared rather than per-element because during a crossfade both elements are audible at
      // once and must be shaped identically — and because peaking filters at 0 dB are exactly
      // transparent, "EQ off" is just a flat curve, so the graph never needs rewiring.
      const eqInput = ctx.createGain();
      let tail = eqInput;
      eqBandsRef.current = EQ_FREQUENCIES.map((frequency) => {
        const band = ctx.createBiquadFilter();
        band.type = "peaking";
        band.frequency.value = frequency;
        band.Q.value = EQ_Q;
        band.gain.value = 0;
        tail.connect(band);
        tail = band;
        return band;
      });
      tail.connect(limiter);
      eqInputRef.current = eqInput;
      for (const [key, el] of [["a", elA], ["b", elB]]) {
        const source = ctx.createMediaElementSource(el);
        const gain = ctx.createGain();
        gain.gain.value = 1;
        source.connect(gain);
        gain.connect(eqInput);
        gainNodesRef.current[key] = gain;
      }
      audioCtxRef.current = ctx;
      audioGraphReadyRef.current = true;
      ctx.resume().catch(() => {});
    } catch {
      audioGraphReadyRef.current = false;
    }
  }

  // Push the current EQ curve into the filter chain. A disabled EQ — or a podcast episode while
  // "apply to podcasts" is off, matching iOS's default exemption for spoken word — is a flat
  // curve rather than a bypass, since a peaking filter at 0 dB is already transparent.
  function applyEqualizer() {
    const bands = eqBandsRef.current;
    if (!bands || !bands.length) return;
    const isEpisode = currentTrack?._kind === "episode";
    const active = Boolean(equalizer?.enabled) && (!isEpisode || Boolean(equalizer?.appliesToPodcasts));
    const gains = active ? normalizeEqGains(equalizer?.gains) : EQ_FLAT_GAINS;
    const ctx = audioCtxRef.current;
    bands.forEach((band, index) => {
      const value = gains[index];
      try {
        // Ramp rather than jump: stepping a filter's gain mid-playback clicks audibly.
        if (ctx) band.gain.setTargetAtTime(value, ctx.currentTime, 0.02);
        else band.gain.value = value;
      } catch {
        try { band.gain.value = value; } catch { /* ignore */ }
      }
    });
  }

  useEffect(() => {
    applyEqualizer();
  }, [equalizer, currentTrack?._kind]);

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
  const eqBandsRef = useRef([]);
  const eqInputRef = useRef(null);
  const dockRef = useRef(null);
  const coreRef = useRef(null);
  const trackCopyRef = useRef(null);
  const pipTrackCopyRef = useRef(null);
  const pipWindowRef = useRef(null);
  const playerContainerRef = useRef(null);
  const reopenPipAfterFullscreen = useRef(false);
  const lastPlaybackReportSecond = useRef(-1);
  const crossfading = useRef(false);
  const crossfadePreparingRef = useRef(false);
  const crossfadeAttemptRef = useRef(0);
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
  const [openDeviceMenu, deviceMenuElement] = useMenuHost(pipContainer);
  const [fullscreenPlayer, setFullscreenPlayer] = useState(false);
  // A phone-width "Now Playing" — reuses the exact same fixed-overlay surface as the real
  // browser Fullscreen API path (fullscreenPlayer) below, but iOS/Android Safari don't support
  // requestFullscreen() on an arbitrary element (only <video>), so that path silently no-ops on
  // mobile. This is a separate CSS-only toggle for the same markup, opened by tapping the
  // compact dock's art/title instead of the (mobile-hidden) pop-out button.
  const [mobileExpanded, setMobileExpanded] = useState(false);
  // The queue renders into the popped-out window when there is one, so the menu has to portal into
  // that window's themed root rather than this document's — and dismiss on that window's clicks.
  const [openQueueMenu, queueMenuElement] = useMenuHost(pipContainer);
  // Same reasoning for the "Add to playlist" picker, which the button below can open from either
  // surface.
  const [openAddToPlaylistMenu, addToPlaylistMenuElement] = useMenuHost(pipContainer);
  const upcomingQueue = queue.slice(Math.max(currentIndex + 1, 0));
  const nextTrack = upcomingQueue[0];
  const progress = duration ? (currentTime / duration) * 100 : 0;
  const nearEndThreshold = duration ? Math.min(30, Math.max(8, duration * 0.15)) : 0;
  const showUpNext = Boolean(nextTrack && duration && duration - currentTime <= nearEndThreshold);
  const cover = playerCoverUrl(currentTrack, apiKey);
  const canSkipForward = currentIndex >= 0 && (currentIndex < queue.length - 1 || repeat === "all");

  // ── What the player SHOWS and what its controls DO ────────────────────────────────────────
  //
  // One resolution point for both, so the markup below is written once and does not branch. When
  // `remote` is set these describe another session; otherwise they are the local values unchanged.
  // Everything above this line is the audio engine and stays local-only.
  const isRemote = Boolean(remote);
  // Latest `isRemote` for the gesture listener below, which mounts once (empty deps) and must
  // not close over a stale value.
  const isRemoteRef = useRef(isRemote);
  isRemoteRef.current = isRemote;
  const remoteCover = remote?.podcast_id
    ? `${API_BASE}/podcasts/${encodeURIComponent(remote.podcast_id)}/cover?api_key=${encodeURIComponent(apiKey)}`
    : remote?.album_id
      ? `${API_BASE}/library/albums/${encodeURIComponent(remote.album_id)}/cover?api_key=${encodeURIComponent(apiKey)}`
      : "";
  const viewTitle = isRemote ? (remote.title || "") : (currentTrack?.title || "Local player");
  const viewSubtitle = isRemote
    ? ([remote.artist, remote.album].filter(Boolean).join(" / ") || "")
    : ([currentTrack?._artist, currentTrack?._album].filter(Boolean).join(" / ") || "Ready");
  const viewCover = isRemote ? remoteCover : cover;
  const viewPlaying = isRemote ? remote.status === "playing" : playing;
  const viewDuration = isRemote ? (remote.duration_seconds || 0) : duration;
  const viewTime = isRemote ? Math.min(remotePosition || 0, viewDuration || Infinity) : currentTime;
  const viewProgress = viewDuration ? (viewTime / viewDuration) * 100 : 0;
  const viewShuffle = isRemote ? Boolean(remote.shuffle) : shuffle;
  const viewRepeat = isRemote ? (remote.repeat || "off") : repeat;
  const viewQueue = isRemote ? remoteQueue : queue;
  const viewHasContent = isRemote ? true : Boolean(currentTrack);
  const viewCanSkipForward = isRemote ? true : canSkipForward;
  const doToggle = isRemote ? () => onRemoteCommand?.(viewPlaying ? "pause" : "resume") : togglePlayback;
  const doSkipBack = isRemote ? () => onRemoteCommand?.("previous") : handleSkipBack;
  const doSkipForward = isRemote ? () => onRemoteCommand?.("next") : onSkipForward;
  // ⚠ A range input fires `onChange` on every pointer move, so sending the seek from there would
  // post a command per pixel dragged. The thumb follows the drag locally and the command is sent
  // once, on release — which is also what the apps do. Held independently of play state: dragging
  // the scrubber means the same thing whether the far end is playing or paused.
  const [remoteScrub, setRemoteScrub] = useState(null);
  const doSeek = isRemote ? (event) => setRemoteScrub(Number(event.target.value)) : seek;
  const commitRemoteSeek = () => {
    if (remoteScrub === null) return;
    onRemoteCommand?.("seek", remoteScrub);
    setRemoteScrub(null);
  };
  const seekValue = isRemote && remoteScrub !== null ? remoteScrub : viewTime;
  const seekProgress = viewDuration ? (seekValue / viewDuration) * 100 : 0;
  const seekBarProps = isRemote
    ? { onPointerUp: commitRemoteSeek, onKeyUp: commitRemoteSeek, onBlur: commitRemoteSeek }
    : {};
  const doToggleShuffle = isRemote ? () => onRemoteMode?.({ shuffle: !viewShuffle }) : onToggleShuffle;
  const doCycleRepeat = isRemote
    ? () => onRemoteMode?.({ loop: viewRepeat === "off" ? "all" : viewRepeat === "all" ? "one" : "off" })
    : onCycleRepeat;
  // Closing a player stops what it is playing. That is as true of another device's as of this tab's.
  const doClose = isRemote ? () => onRemoteCommand?.("stop") : onClose;

  // Add to playlist targets a TRACK only — there is no episode→playlist relationship — and, unlike
  // Favorite before it, doesn't need the audio engine, so it isn't disabled just because playback is
  // on another session: a remote session's PlayerSessionOut carries track_id/episode_id same as a
  // local track carries id/_kind.
  const addToPlaylistTrackId = isRemote
    ? (remote?.episode_id ? null : remote?.track_id || null)
    : (currentTrack?._kind === "episode" ? null : currentTrack?.id || null);
  const addToPlaylistItems = addToPlaylistTrackId && (playlists || []).length
    ? playlists.map((playlist) => ({
        label: playlist.name,
        action: () => onAddToPlaylist?.(playlist.id, [addToPlaylistTrackId]),
      }))
    : [];
  const doAddToPlaylist = addToPlaylistItems.length
    ? (event) => openAddToPlaylistMenu(event, addToPlaylistItems)
    : null;

  // Registers as a live viewer so the sessions poll runs fast while a remote player is on screen.
  useEffect(() => {
    if (!isRemote) return undefined;
    onRemoteLive?.(1);
    return () => onRemoteLive?.(-1);
  }, [isRemote, onRemoteLive]);

  const renderShuffle = (size) => (doToggleShuffle ? (
    <button className={`player-icon-button${viewShuffle ? " active" : ""}`} onClick={doToggleShuffle} title={viewShuffle ? "Shuffle on" : "Shuffle off"}>
      <Shuffle size={size} />
    </button>
  ) : null);
  const renderRepeat = (size) => (doCycleRepeat ? (
    <button className={`player-icon-button${viewRepeat !== "off" ? " active" : ""}`} onClick={doCycleRepeat} title={viewRepeat === "one" ? "Repeat one" : viewRepeat === "all" ? "Repeat all" : "Repeat off"}>
      {viewRepeat === "one" ? <Repeat1 size={size} /> : <Repeat size={size} />}
    </button>
  ) : null);

  // Load + play the current track on the active element. If the upcoming track was
  // already preloaded on the OTHER element, swap to it instead of reloading (gapless).
  useEffect(() => {
    crossfadeAttemptRef.current += 1;
    crossfadePreparingRef.current = false;
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
    // The graph may have only just been built, after the settings effect already ran.
    applyEqualizer();
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
  }, [currentTrack, queue, lyricsOpen, fullscreenPlayer, mobileExpanded, pipContainer, showUpNext]);

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
  // ⚠ Skipped while this tab is only DISPLAYING another device's remote session (`isRemote`):
  // this player mounts as soon as any of the account's other sessions starts playing (line
  // ~3171, `playerOpen || activeRemoteSession`), so without this guard, merely opening the web
  // app and clicking anywhere while e.g. the iPhone plays creates and resumes a real
  // AudioContext wired to this machine's output — nothing audible plays (both <audio> elements
  // stay srcless), but macOS still sees it as a live audio client and can silently switch a
  // shared Bluetooth output (AirPods) onto the Mac. Once this tab has an actual local track
  // (`isRemote` false), the graph builds normally on the next gesture.
  useEffect(() => {
    const handler = () => {
      if (isRemoteRef.current) return;
      ensureAudioGraph();
      applyEqualizer();
    };
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
    if (!fadeSec || !duration || !nextAudioUrl || crossfading.current || crossfadePreparingRef.current) return;
    if (repeat === "one") return; // repeating the same track: nothing to fade into
    const remaining = duration - currentTime;
    if (remaining > fadeSec || remaining <= 0) return;
    const otherKey = activeKeyRef.current === "a" ? "b" : "a";
    if (loadedUrlRef.current[otherKey] !== nextAudioUrl) return; // next not preloaded yet
    const active = activeAudio();
    const next = inactiveAudio();
    if (!active || !next || next.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) return;
    const attempt = ++crossfadeAttemptRef.current;
    const activeKeyAtStart = activeKeyRef.current;
    crossfadePreparingRef.current = true;
    try { next.currentTime = 0; } catch { /* ignore */ }
    next.volume = 0;
    applyReplayGain(otherKey, nextTrack);
    next.play().then(() => {
      if (
        attempt !== crossfadeAttemptRef.current
        || activeKeyAtStart !== activeKeyRef.current
        || loadedUrlRef.current[otherKey] !== nextAudioUrl
      ) return;
      const audibleRemaining = Math.max(0, (active.duration || duration) - active.currentTime);
      if (audibleRemaining <= 0) {
        crossfadePreparingRef.current = false;
        return;
      }
      // Never lower the current element until the browser has actually started the next one.
      // If preparation consumed part of the overlap window, use the remaining time rather than
      // chopping the outgoing track at the originally scheduled boundary.
      const actualFadeSec = Math.max(0.05, Math.min(fadeSec, audibleRemaining));
      crossfadePreparingRef.current = false;
      crossfading.current = true;
      const startTime = performance.now();
      crossfadeIntervalRef.current = setInterval(() => {
        const elapsed = (performance.now() - startTime) / 1000;
        const frac = Math.min(elapsed / actualFadeSec, 1);
        const phase = frac * Math.PI / 2;
        active.volume = Math.max(0, Math.cos(phase));
        next.volume = Math.min(1, Math.sin(phase));
        if (frac >= 1) {
          clearInterval(crossfadeIntervalRef.current);
          crossfadeIntervalRef.current = null;
          // App updates audioUrl, promoting `next` (already audible) without reloading it.
          onEndedRef.current?.();
        }
      }, 30);
    }).catch(() => {
      if (attempt === crossfadeAttemptRef.current) {
        crossfadePreparingRef.current = false;
        next.volume = 0;
      }
    });
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
  // coreRef/dockRef point at a different DOM node depending on which surface is showing
  // (compact dock vs. the fullscreen/mobileExpanded overlay) — re-attach the observer to
  // whichever node is current whenever that switches, or a stale height (measured from a
  // now-unmounted node) sticks around and mispositions the mobile tab bar.
  }, [onDockChange, pipContainer, currentTrack?.id, fullscreenPlayer, mobileExpanded]);

  function cancelCrossfadeForTransport() {
    crossfadeAttemptRef.current += 1;
    crossfadePreparingRef.current = false;
    crossfading.current = false;
    if (crossfadeIntervalRef.current) {
      clearInterval(crossfadeIntervalRef.current);
      crossfadeIntervalRef.current = null;
    }
    const active = activeAudio();
    const inactive = inactiveAudio();
    if (active) active.volume = 1;
    if (inactive) {
      inactive.pause();
      inactive.volume = 0;
      try { inactive.currentTime = 0; } catch { /* not seekable yet */ }
    }
  }

  function togglePlayback() {
    const audio = activeAudio();
    if (!audio) return;
    if (audio.paused) {
      audio.play().catch(() => {});
    } else {
      cancelCrossfadeForTransport();
      audio.pause();
    }
  }

  // Skip-back restarts the current track; pressing again while still near the
  // start (< 1s in) jumps to the previous track. Playback position doubles as
  // the "pressed again quickly" timer.
  function handleSkipBack() {
    const audio = activeAudio();
    if (audio && audio.currentTime > 1) {
      cancelCrossfadeForTransport();
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
      pause: () => { cancelCrossfadeForTransport(); activeAudio()?.pause(); },
      resume: () => activeAudio()?.play().catch(() => {}),
      stop: () => {
        cancelCrossfadeForTransport();
        const a = activeAudio();
        if (a) { a.pause(); try { a.currentTime = 0; } catch { /* ignore */ } }
      },
      // Seek the active element; returns false until it's seekable (metadata loaded) so
      // callers (podcast resume) can retry. Used to restore an episode's saved position.
      seek: (seconds) => {
        const a = activeAudio();
        if (!a || !a.duration || Number.isNaN(a.duration)) return false;
        cancelCrossfadeForTransport();
        try { a.currentTime = Math.max(0, Math.min(seconds, a.duration - 1)); return true; } catch { return false; }
      },
      // `playing` and the elapsed clock live in this component, but the queue snapshot is built one
      // level up in App. Read them through the handle rather than lifting the state: a handoff needs
      // the value at the instant it is sent, and re-rendering App twice a second to mirror a clock
      // it only reads on demand is pure churn.
      isPlaying: () => { const a = activeAudio(); return !!a && !a.paused && !a.ended; },
      position: () => { const a = activeAudio(); const t = a?.currentTime; return Number.isFinite(t) ? t : 0; },
    };
    return () => { if (controlRef) controlRef.current = null; };
  }, [controlRef]);

  // ⚠ A WALL-CLOCK heartbeat that KEEPS RUNNING WHILE PAUSED.
  //
  // The only other recurring report rides on `onTimeUpdate`, and a paused media element fires no
  // timeupdate at all — so a paused tab used to report its state once and then go silent forever.
  // The server decides whether "playing" is still believable from when a session last described
  // itself, and a paused tab at someone's desk is the likeliest place they will want to move
  // playback TO, so it has to keep saying it is there. 15s matches the iOS client's own loop.
  useEffect(() => {
    const timer = setInterval(() => {
      notifyPlaybackState(playing ? "playing" : "paused");
    }, 15000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, currentTrack?.id]);

  // Same reasoning as the command poll's visibility handler in App: a hidden tab is throttled, so
  // say where we are the moment the tab is looked at again.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") {
        notifyPlaybackState(playing ? "playing" : "paused");
      }
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, currentTrack?.id]);

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

  // The media element couldn't load/decode the track. Only the ACTIVE element should
  // surface — the inactive one fires the same event while preloading the next track.
  function handleTrackError(key) {
    if (key !== activeKeyRef.current) return;
    const el = activeAudio();
    const code = el?.error?.code;
    // MediaError: 2=network, 3=decode, 4=src not supported (missing file / unplayable format)
    const reason =
      code === 4 ? "the file is missing, or its format can't be played in this browser"
      : code === 3 ? "the audio couldn't be decoded"
      : code === 2 ? "a network error interrupted playback"
      : "it couldn't be loaded";
    setPlaying(false);
    onPlaybackError?.(currentTrack, reason);
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
        onError={() => handleTrackError(key)}
      />
    );
  }

  function seek(event) {
    const audio = activeAudio();
    if (!audio || !duration) return;
    cancelCrossfadeForTransport();
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
    // A remote queue is listed AND jumpable (via the "jump" command, `queue_index` into the far
    // end's own queue) — but never reordered/removed from here: those actions (move/remove) would
    // still appear to work locally and be contradicted by the next poll, which a plain jump is not
    // (the far end reports its new current track, which is exactly what this view then reflects).
    if (isRemote) {
      return remoteQueue
        .filter((track) => !track._remoteCurrent)
        .map((track, index) => (
          <div className="queue-entry" key={`${track.id}:${index}`}>
            {onRemoteQueueJump ? (
              <button className="queue-play-btn" onClick={() => onRemoteQueueJump(track._remoteIndex)}>
                <strong>{track.title}</strong>
                <small>{track._artist || ""}</small>
              </button>
            ) : (
              <span className="queue-play-btn is-static">
                <strong>{track.title}</strong>
                <small>{track._artist || ""}</small>
              </span>
            )}
          </div>
        ));
    }
    return upcomingQueue.map((track, index) => (
      <div
        className="queue-entry"
        key={`${track.id}:${index}`}
        onContextMenu={(event) => openQueueMenu(event, [
          { label: "Play", action: () => onPlayTrack(track) },
          onRemoveFromQueue && { label: "Remove from queue", danger: true, action: () => onRemoveFromQueue(index) },
        ].filter(Boolean))}
      >
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
    const pipLayout = popped || fullscreenPlayer || mobileExpanded;
    const docked = !pipLayout;

    if (docked) {
      return (
        <div className="audio-player topbar" ref={(el) => { dockRef.current = el; playerContainerRef.current = el; }}>
          {/* Two independent layouts, not one reflowed by CSS: .topbar-player-row is the desktop
              player (below, unchanged); .mobile-mini-player is a from-scratch compact bar built
              for a phone, styled after the native app's own mini player rather than a squeezed
              copy of the desktop one. Only one is ever visible (media query), but both stay
              mounted so coreRef — wrapping both — measures whichever one actually has size. */}
          <div className="topbar-player-core" ref={coreRef}>
          <div className="topbar-player-row">
            <div className="player-art" onClick={() => setMobileExpanded(true)} role="button" tabIndex={0} title="Now playing">
              {viewCover ? <img src={viewCover} alt="" /> : <Music size={18} />}
            </div>
            <div className="topbar-track-copy" ref={trackCopyRef} onClick={() => setMobileExpanded(true)} role="button" tabIndex={0} title="Now playing">
              <strong>{viewTitle}</strong>
              <small>{viewSubtitle}</small>
            </div>
            <div className="topbar-controls">
              <span className="topbar-secondary-control">{renderShuffle(16)}</span>
              <button className="player-icon-button" onClick={doSkipBack} disabled={!viewHasContent} title="Previous">
                <SkipBack size={16} />
              </button>
              <button className="player-play-button compact" onClick={doToggle} title={viewPlaying ? "Pause" : "Play"}>
                {viewPlaying ? <Pause size={17} /> : <Play size={17} />}
              </button>
              <button className="player-icon-button" onClick={doSkipForward} disabled={!viewCanSkipForward} title="Next">
                <SkipForward size={16} />
              </button>
              <span className="topbar-secondary-control">{renderRepeat(16)}</span>
              {/* Add to playlist is an ACCOUNT fact like Favourites was, so it means the same thing
                  over a track playing elsewhere — but only once we know which track that is (an
                  episode has no playlist relationship, so it disables itself rather than open an
                  empty menu). */}
              <button className="topbar-secondary-control player-icon-button" onClick={doAddToPlaylist} disabled={!doAddToPlaylist} title="Add to playlist">
                <ListPlus size={16} />
              </button>
            </div>
            <input className="player-progress topbar-progress" type="range" min="0" max={viewDuration || 0} value={seekValue} onChange={doSeek} {...seekBarProps} style={{ "--progress": `${seekProgress}%` }} />
            <div className="topbar-actions">
              {/* Move playback to another of this account's sessions. Reuses the same menu machinery
                  and the same item builder as the idle dock's picker, so the two can never offer
                  different things. */}
              {/* The same control the apps carry, in the same place and with the same meaning: its
                  own devices glyph, not an ellipsis, and lit while the audio is on another device —
                  which is the one thing that distinguishes a remote player from a local one. */}
              <span className="topbar-secondary-control">
                <OverflowMenuButton
                  openMenu={openDeviceMenu}
                  items={deviceMenuItems ? deviceMenuItems() : []}
                  className="player-icon-button"
                  label="Playback devices"
                  icon={MonitorSpeaker}
                  iconSize={16}
                  active={isRemote}
                  alwaysShow
                />
              </span>
              <button className="player-icon-button" onClick={() => setQueueOpen((v) => !v)} title="Queue">
                <Menu size={16} />
              </button>
              {/* Popping out is exactly as valid while controlling another session as while playing
                  locally — the popped surface just keeps rendering whichever `remote`/local state
                  this component already resolves everywhere else. */}
              <button className="row-icon-button topbar-secondary-control" onClick={openPictureInPicture} disabled={!viewHasContent} title={viewHasContent ? "Pop out" : "Queue is empty"}>
                <PictureInPicture2 size={14} />
              </button>
              <button className="row-icon-button" onClick={doClose} title={isRemote ? "Stop playback" : "Close player"}>
                <X size={14} />
              </button>
            </div>
            {headerActions && <div className="topbar-side topbar-side-right in-player">{headerActions}</div>}
          </div>

          {/* Floats above the tab bar rather than living in the topbar — every native mini
              player (this app's own iOS one included) docks at the bottom, not the top, and
              trying to cram prev/next/queue/close/progress into a phone-width topbar was the
              thing actually being reflowed here before, not designed for it. Just art, title,
              and play/pause; everything else is one tap away via the same tap-to-expand Now
              Playing view the art/title already open. */}
          <div className="mobile-mini-player" onClick={() => setMobileExpanded(true)} role="button" tabIndex={0} title="Now playing">
            <div className="mini-progress-track"><div className="mini-progress-fill" style={{ width: `${seekProgress}%` }} /></div>
            <div className="mini-art">
              {viewCover ? <img src={viewCover} alt="" /> : <Music size={20} />}
            </div>
            <div className="mini-copy">
              <strong>{viewTitle}</strong>
              <small>{viewSubtitle}</small>
            </div>
            <button
              className="mini-play-btn"
              onClick={(event) => { event.stopPropagation(); doToggle(); }}
              title={viewPlaying ? "Pause" : "Play"}
            >
              {viewPlaying ? <Pause size={19} /> : <Play size={19} />}
            </button>
            <button
              className="mini-next-btn"
              onClick={(event) => { event.stopPropagation(); doSkipForward(); }}
              disabled={!viewCanSkipForward}
              title="Next"
            >
              <SkipForward size={17} />
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

    const closeAction = popped
      ? () => pipWindowRef.current?.close?.()
      : mobileExpanded ? () => setMobileExpanded(false) : toggleFullscreenPlayer;
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
        style={viewCover ? { "--fullscreen-art": `url(${viewCover})` } : undefined}
      >
        <div className="player-core" ref={(el) => { fsCoreRef.current = el; if (!popped) coreRef.current = el; }}>
          <div className="audio-header">
            <div className="player-art" ref={fsArtRef}>{viewCover ? <img src={viewCover} alt="" /> : <Music size={34} />}</div>
            <div className="audio-track-copy" ref={pipTrackCopyRef}>
              <span className="playing-from">Playing from library</span>
              <strong>{viewTitle}</strong>
              <small>{viewSubtitle || currentTrack?.path || "Ready"}</small>
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
              {/* The docked topbar carries this same control (§ above) — the popped-out/fullscreen
                  surface had none at all, which is what made switching playback locations from the
                  popout impossible rather than merely broken. Same menu host, same item builder. */}
              <OverflowMenuButton
                openMenu={openDeviceMenu}
                items={deviceMenuItems ? deviceMenuItems() : []}
                className="row-icon-button"
                label="Playback devices"
                icon={MonitorSpeaker}
                iconSize={14}
                active={isRemote}
                alwaysShow
              />
              <button
                className="row-icon-button"
                onClick={mobileExpanded ? () => setMobileExpanded(false) : toggleFullscreenPlayer}
                title={(fullscreenPlayer || mobileExpanded) ? "Exit fullscreen" : "Fullscreen"}
              >
                {(fullscreenPlayer || mobileExpanded) ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
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
            <input className="player-progress" type="range" min="0" max={viewDuration || 0} value={seekValue} onChange={doSeek} {...seekBarProps} style={{ "--progress": `${seekProgress}%` }} />
            <div className="player-controls">
              <button className={`player-icon-button${lyricsOpen ? " active" : ""}`} onClick={() => setLyricsOpen((v) => !v)} disabled={isRemote} title="Lyrics">
                <Mic2 size={19} className="lyric-icon-on" />
                <Ban size={19} className="lyric-icon-off" />
              </button>
              {renderShuffle(18)}
              <button className="player-icon-button" onClick={doSkipBack} disabled={!viewHasContent} title="Previous">
                <SkipBack size={18} />
              </button>
              <button className="player-play-button" onClick={doToggle} title={viewPlaying ? "Pause" : "Play"}>
                {viewPlaying ? <Pause size={21} /> : <Play size={21} />}
              </button>
              <button className="player-icon-button" onClick={doSkipForward} disabled={!viewCanSkipForward} title="Next">
                <SkipForward size={18} />
              </button>
              {renderRepeat(18)}
              <button className="player-icon-button" onClick={doAddToPlaylist} disabled={!doAddToPlaylist} title="Add to playlist">
                <ListPlus size={19} />
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <>
      {queueMenuElement}
      {deviceMenuElement}
      {addToPlaylistMenuElement}
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
  // ⚠ NEVER merge across different batch_ids for the download group: approveItems groups the
  // descendant ids of whatever the admin selects by their real batch_id and calls
  // /approvals/{batchId}/approve per batch, so a merged artist/album row spanning two unrelated
  // batches (e.g. one admin's own request and a different user's separate wishlist request that
  // happen to share an artist name) would silently approve BOTH batches when the admin only meant
  // to review one — a real cross-user approval leak. Other kinds (metadata/import_files/lyrics)
  // aren't per-user requests, so merging those across batches stays safe.
  for (const group of groups.values()) {
    const mergeAcrossBatches = group.id !== "type:download";
    const itemIdSet = new Set(group.items.map((i) => i.id));
    const titleToId = new Map();
    const idRemap = new Map();
    for (const item of group.items) {
      if (item.parent_id && itemIdSet.has(item.parent_id)) continue; // not a root
      const mergeKey = mergeAcrossBatches ? item.title : `${item.batch_id}:${item.title}`;
      if (!titleToId.has(mergeKey)) {
        titleToId.set(mergeKey, item.id);
      } else {
        idRemap.set(item.id, titleToId.get(mergeKey));
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
