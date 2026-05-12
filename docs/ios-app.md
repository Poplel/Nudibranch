# iOS Companion App

The iOS app should be built with SwiftUI and use the REST API for every action.
It does not need to play music; playback remains the job of Jellyfin clients.

## Capabilities

- connect to a Nudibranch server
- PIN login
- store API key in Keychain
- browse library tree
- manage personal wishlist
- view admin/global wishlists if permitted
- approve/reject selected proposal items
- view task queue
- register APNs token
- open approval queue from notifications

## Notifications

The app should register for APNs and send the device token to:

```txt
POST /api/v1/notifications/devices
```

Notification payloads should deep link to:

```txt
/approvals
/tasks
/wishlist
/settings
```

The server owns notification state. The app should refresh notification history
from `/api/v1/notifications` on launch and foreground.

