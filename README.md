# iPhoneCam: use your iPhone as a USB webcam

Two parts:

- `ios/iPhoneCamApp.swift` is a small iPhone app that captures the camera and serves JPEG frames on port 5005.
- `host/iphonecam.py` runs on your computer, tunnels to that port over USB, and feeds a virtual camera.

## 1. Build the iPhone app (needs a Mac with Xcode, once)

1. Xcode > File > New > Project > iOS App (SwiftUI), name it `iPhoneCam`.
2. Delete the generated `iPhoneCamApp.swift` and `ContentView.swift`, then drag in `ios/iPhoneCamApp.swift`.
3. In the target's Info tab, add **Privacy - Camera Usage Description** (any text, e.g. "Streams video to your computer").
4. Pick your iPhone as the run destination, set your Apple ID under Signing & Capabilities, and press Run. A free Apple ID works, but the app must be re-installed every 7 days.
5. On the phone: Settings > General > VPN & Device Management > trust your developer profile.

No Mac? There is no way to build a native iOS app without one. An alternative is the paid app *Camo* or *EpocCam*, which speak their own protocols.

## 2. Set up the computer

```
pip install -r host/requirements.txt
```

Plus one virtual camera backend:

| OS | What to install |
|---|---|
| Windows | [OBS Studio](https://obsproject.com) (open it once, click Start Virtual Camera, then stop it) and iTunes or the Apple Devices app (provides the USB driver) |
| macOS | OBS Studio, same one-time step |
| Linux | `sudo apt install v4l2loopback-dkms usbmuxd` then `sudo modprobe v4l2loopback exclusive_caps=1` |

## 3. Run

1. Plug in the iPhone, unlock it, tap **Trust**.
2. Open the iPhoneCam app and leave it in the foreground.
3. `python host/iphonecam.py --preview`
4. In Zoom, Discord, OBS, a browser, etc., choose **OBS Virtual Camera** (or the v4l2loopback device) as the camera.

Options: `--flip`, `--rotate 90`, `--fps 60`, `--list`, `--device N`, `--backend`.

## Troubleshooting

- *No iPhone found*: unlock the phone, accept the Trust prompt, try another cable.
- *usbmuxd refused connection*: the app is not open in the foreground.
- *Image sideways*: use `--rotate 90` (or 270).
- *Virtual camera failed on Windows/macOS*: OBS is not installed, or its virtual camera was never started once.
- *Apps do not list the camera*: restart the app after starting the script, since some read the device list only at launch.
- Other apps that need to see the camera before it streams (Zoom on some systems) work best if you start the script first.
