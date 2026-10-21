# Ultimate Assistant for Android

The phone app for Ultimate Assistant. The assistant itself runs on your computer; the phone shows
the conversation and the assistant's canvas, takes voice, and lets the assistant act on the phone
when you allow it. Android 8.0 (API 26) and newer.

## Pairing

1. Install Tailscale on the computer and the phone and sign in to the same account.
2. On the computer, ask the assistant to pair your phone. It shows a QR code (a one-time
   invitation, valid for 5 minutes).
3. Scan it with the phone's camera. The app opens with the address and invitation filled in;
   tap **Pair**.

You can also enter the address and invitation by hand in **Phone setup > Backend settings**.
The phone keeps a device credential the assistant can revoke; no account password is stored.

## What it can do

- **Conversation and voice**: the same conversation as on the computer; it can be your default
  assistant (long-press home or the side key), voice first.
- **Canvas**: cards, drawings and images the assistant shows over your screen, with only the
  interactive parts taking touches.
- **Acting on the phone**, each only after you grant it in Android settings: open apps and links,
  read and act on the screen (accessibility), share and read files from folders you connect,
  notifications. The app reports what's actually granted; the assistant never assumes.
- **Optional privileged control** through [Shizuku](https://shizuku.rikka.app/) if you install it.

Commands from the assistant are queued, leased and acknowledged, so a dropped connection never
repeats an action. Android can delay background work; the app retries with WorkManager.

## Build

Requires JDK 17, the Android SDK (platform 34, build-tools 34) and Gradle 8.2:

```
gradle :app:assembleDebug
gradle :app:lintDebug
```

Source layout: `app/src/main/java/io/github/kianjross/herald/assistant/`. `CommandDispatcher` runs
allowlisted commands, `SyncWorker` polls and acknowledges them, `BridgeClient` talks to the
assistant's `/api/mobile/v1` routes (served by `ultimate_assistant/phone/` on the computer).
