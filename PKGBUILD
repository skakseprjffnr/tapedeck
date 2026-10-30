# Personal package. Build and install with: makepkg -si
pkgname=tapedeck
pkgver=0.5.0
pkgrel=1
pkgdesc="Personal YouTube playlist builder: categories, drag-to-reorder, audio playback via mpv"
arch=('any')
license=('MIT')
depends=('python' 'pyside6' 'yt-dlp' 'yt-dlp-ejs' 'mpv')
optdepends=('ttf-ibm-plex: the typeface the interface is designed around'
            'mpv-mpris: media keys and desktop now-playing controls'
            'qt6-wayland: run natively on Wayland instead of through XWayland')
source=('tapedeck.py' 'tapedeck.desktop' 'tapedeck.svg')
sha256sums=('SKIP' 'SKIP' 'SKIP')

package() {
  install -Dm755 tapedeck.py "$pkgdir/usr/bin/tapedeck"
  install -Dm644 tapedeck.desktop "$pkgdir/usr/share/applications/tapedeck.desktop"
  install -Dm644 tapedeck.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/tapedeck.svg"
}
