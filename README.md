# Nudibranch

Nudibranch is a self-hosted music library and download manager. It may be operated as a standalone
music player and downloader, or alongside an existing [Jellyfin](https://jellyfin.org) server.
Nudibranch is split into two parts, a server that manages your library and downloads, and a client application for controlling the server and playing music. 
For install instructions, and additional info, visit [nudibranch.poplel.xyz](https://nudibranch.poplel.xyz)

## About the name

Nudibranchs (/ˈnjuːdɪbræŋk/) are soft-bodied marine gastropod molluscs of the order Nudibranchia,
commonly known as sea slugs. The program is named after *Glaucus atlanticus*, a nudibranch whose
*jelly*-like cerata are arranged along its back like wings or *fins*. The name was chosen both because
the program was designed with Jellyfin in mind and because the animal is cool

## Features

- Import and repair your music library
- Search for and download music through slskd (Soulseek), with yt-dlp as a fallback source.
- Enrich albums with cover art and lyrics.
- Every web interface action is exposed through the API, fully client agnostic server.
- Manage and play your library in the web ui or from the iOS app, lossless with an equalizer, playlists, queue etc.
- Jellyfin server connection to automatically sync playlists and your library.
- Remote playback controls similar to Spotify Connect, allowing you to play music on one device and manage playback on another.
