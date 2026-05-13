import React, { useEffect, useRef, useState } from "react";
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
  Moon,
  Music,
  Search,
  Settings,
  Shield,
  Sparkles,
  Sun,
  Users,
} from "lucide-react";
import "./styles.css";

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

const artists = [
  {
    name: "Japanese Breakfast",
    albums: [
      {
        name: "Jubilee",
        tracks: [
          ["01", "Paprika", "FLAC"],
          ["02", "Be Sweet", "FLAC"],
          ["03", "Kokomo, IN", "MP3 warning"],
        ],
      },
    ],
  },
  {
    name: "Alvvays",
    albums: [
      {
        name: "Blue Rev",
        tracks: [
          ["01", "Pharmacist", "FLAC"],
          ["02", "Easy On Your Own?", "FLAC"],
        ],
      },
    ],
  },
];

const proposals = [
  {
    id: "artist-1",
    depth: 0,
    title: "Japanese Breakfast",
    detail: "3 pending operations",
    selected: true,
  },
  {
    id: "album-1",
    depth: 1,
    title: "Jubilee",
    detail: "Exact release preferred",
    selected: true,
  },
  {
    id: "track-1",
    depth: 2,
    title: "03-Kokomo, IN.flac",
    detail: "Replace MP3 with FLAC",
    selected: true,
  },
  {
    id: "lyrics-1",
    depth: 2,
    title: "Kokomo, IN.lrc",
    detail: "Add synced lyrics",
    selected: false,
  },
  {
    id: "cover-1",
    depth: 1,
    title: "cover.jpg",
    detail: "Update album art",
    selected: true,
  },
];

function App() {
  const [page, setPage] = useState("Library");
  const [dark, setDark] = useState(false);
  const [trayOpen, setTrayOpen] = useState(false);
  const [toastVisible, setToastVisible] = useState(true);
  const [accentColor, setAccentColor] = useState("#356df3");
  const [backgroundTint, setBackgroundTint] = useState("#356df3");
  const [selected, setSelected] = useState(() => new Set(proposals.filter((item) => item.selected).map((item) => item.id)));
  const trayRef = useRef(null);

  const selectedCount = selected.size;
  const theme = dark ? "app dark" : "app";

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
    if (!toastVisible) return;
    const timeout = window.setTimeout(() => setToastVisible(false), 5200);
    return () => window.clearTimeout(timeout);
  }, [toastVisible]);

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
          <div className="notification-anchor" ref={trayRef}>
            <button className="icon-button" onClick={() => setTrayOpen((value) => !value)} title="Notifications">
              <Bell size={18} />
              <span className="badge">4</span>
            </button>
            {trayOpen && <NotificationTray />}
          </div>
        </header>

        <div className="content-grid">
          <section className="panel main-panel">
            <PanelHeader page={page} selectedCount={selectedCount} />
            {page === "Library" && <LibraryTree />}
            {page === "Approvals" && <Approvals selected={selected} setSelected={setSelected} />}
            {page === "Import" && <ImportWizard />}
            {page === "Settings" && (
              <SettingsPanel
                accentColor={accentColor}
                setAccentColor={setAccentColor}
                backgroundTint={backgroundTint}
                setBackgroundTint={setBackgroundTint}
              />
            )}
            {!["Library", "Approvals", "Import", "Settings"].includes(page) && <Placeholder page={page} />}
          </section>

          <aside className="panel inspector">
            <h2>Inspector</h2>
            <div className="diff">
              <span>Current</span>
              <p>Artist/Jubilee/03-Kokomo, IN.mp3</p>
              <span>Proposed</span>
              <p>Artist/Jubilee/03-Kokomo, IN.flac</p>
            </div>
            <div className="metadata-grid">
              <label>Format</label>
              <strong>FLAC</strong>
              <label>Match</label>
              <strong>Acoustic + MBID</strong>
              <label>Risk</label>
              <strong>Low</strong>
            </div>
          </aside>
        </div>
        {toastVisible && (
          <Toast
            title="Approval needed"
            body="Import folder review has 12 selectable changes."
            onClose={() => setToastVisible(false)}
          />
        )}
      </section>
    </main>
  );
}

function NotificationTray() {
  return (
    <div className="notification-tray">
      <h2>Notifications</h2>
      <TrayItem tone="urgent" title="Approval needed" body="Import folder review has 12 selectable changes." />
      <TrayItem title="Wishlist finished" body="3 FLAC candidates found from slskd." />
      <TrayItem title="Low quality fallback" body="One track only has MP3 candidates." />
      <TrayItem title="Jellyfin synced" body="Library scan completed successfully." />
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
      {page === "Approvals" && (
        <div className="approval-actions">
          <button className="secondary">Reject selected</button>
          <button className="primary">
            <Check size={16} />
            Approve selected
          </button>
        </div>
      )}
    </div>
  );
}

function LibraryTree() {
  const [openArtists, setOpenArtists] = useState(() => new Set(["Japanese Breakfast", "Alvvays"]));
  const [openAlbums, setOpenAlbums] = useState(() => new Set(["Jubilee", "Blue Rev"]));

  return (
    <div className="tree">
      {artists.map((artist) => (
        <div key={artist.name}>
          <TreeRow
            icon={Folder}
            open={openArtists.has(artist.name)}
            title={artist.name}
            meta={`${artist.albums.length} album`}
            onToggle={() => toggleSet(setOpenArtists, artist.name)}
          />
          {openArtists.has(artist.name) &&
            artist.albums.map((album) => (
              <div key={album.name}>
                <TreeRow
                  depth={1}
                  icon={Folder}
                  open={openAlbums.has(album.name)}
                  title={album.name}
                  meta={`${album.tracks.length} tracks`}
                  onToggle={() => toggleSet(setOpenAlbums, album.name)}
                />
                {openAlbums.has(album.name) &&
                  album.tracks.map(([number, title, format]) => (
                    <TreeRow
                      key={title}
                      depth={2}
                      icon={FileAudio}
                      title={`${number}-${title}`}
                      meta={format}
                      warning={format.includes("warning")}
                    />
                  ))}
              </div>
            ))}
        </div>
      ))}
    </div>
  );
}

function Approvals({ selected, setSelected }) {
  const allSelected = selected.size === proposals.length;

  return (
    <div className="approval-tree">
      <div className="bulk-row">
        <label>
          <input
            type="checkbox"
            checked={allSelected}
            onChange={(event) => setSelected(event.target.checked ? new Set(proposals.map((item) => item.id)) : new Set())}
          />
          Select all visible
        </label>
        <span>{selected.size} selected</span>
      </div>
      {proposals.map((proposal) => (
        <label className="proposal-row" style={{ "--depth": proposal.depth }} key={proposal.id}>
          <input
            type="checkbox"
            checked={selected.has(proposal.id)}
            onChange={(event) => {
              const next = new Set(selected);
              if (event.target.checked) next.add(proposal.id);
              else next.delete(proposal.id);
              setSelected(next);
            }}
          />
          <span className="proposal-title">{proposal.title}</span>
          <small>{proposal.detail}</small>
        </label>
      ))}
    </div>
  );
}

function ImportWizard() {
  return (
    <div className="wizard">
      {["Scan /app/import", "Fingerprint", "Group", "Preview changes", "Approve import"].map((step, index) => (
        <div className={index === 0 ? "wizard-step current" : "wizard-step"} key={step}>
          <span>{index + 1}</span>
          <strong>{step}</strong>
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

function SettingsPanel({ accentColor, setAccentColor, backgroundTint, setBackgroundTint }) {
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
          <span>API</span>
          <strong>Connected</strong>
          <span>Worker</span>
          <strong>Running</strong>
          <span>slskd</span>
          <strong>Configured</strong>
        </div>
      </section>
    </div>
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

function toggleSet(setter, value) {
  setter((current) => {
    const next = new Set(current);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    return next;
  });
}

createRoot(document.getElementById("root")).render(<App />);
