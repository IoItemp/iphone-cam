# Getting the IPA onto your iPhone from Windows (no Mac)

The IPA is built by GitHub's free cloud Macs, unsigned. Sideloadly (or AltStore) signs it with your own Apple ID when it installs.

## A. Build the IPA on GitHub

1. Create a free account at github.com and a **new public repository** (public repos get free build minutes; private ones burn them 10x faster on macOS).
2. Upload the contents of this `iphone-cam` folder so that `.github/`, `ios/`, and `host/` sit at the repository root. Easiest: on the repo page, "Add file > Upload files" and drag everything in. If the web uploader skips the hidden `.github` folder, use GitHub Desktop or `git push` instead.
3. Open the **Actions** tab > **Build IPA** > **Run workflow**. It takes a few minutes.
4. When it turns green, open the run and download the **iPhoneCam-ipa** artifact at the bottom. Unzip it to get `iPhoneCam.ipa`.

If the build fails, open the failed step and send me the error text. The Swift code has not been compiled yet, so a small fix may be needed.

## B. Install with Sideloadly (Windows)

1. Install iTunes and iCloud from **apple.com / the Sideloadly site's download links**, not the Microsoft Store versions. Sideloadly needs the non-Store builds.
2. Install Sideloadly from sideloadly.io.
3. On the iPhone: Settings > Privacy & Security > **Developer Mode** > On (it restarts the phone).
4. Plug in the iPhone, tap Trust, open Sideloadly, drag in `iPhoneCam.ipa`, enter your Apple ID, and click Start.
5. On the iPhone: Settings > General > VPN & Device Management > trust your Apple ID profile.
6. Open iPhoneCam, allow camera access, then run `python host/iphonecam.py --preview` on the PC.

## Limits

- A free Apple ID signature lasts **7 days**. Re-run Sideloadly to refresh. A free ID also allows only 3 sideloaded apps at once.
- AltStore (altstore.io) can refresh automatically over Wi-Fi while AltServer runs on your PC.
- Use an Apple ID you are comfortable with. Sideloadly sends credentials to Apple only, but a secondary account is a common precaution.
