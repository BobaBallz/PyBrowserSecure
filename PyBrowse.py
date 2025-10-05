import os
import sys
import shutil
import shutil as _shutil
from pathlib import Path

# === Configuration (set env var BEFORE importing QtWebEngine) ===
TOR_SOCKS = os.environ.get("TOR_SOCKS", "127.0.0.1:9050")  # e.g. 127.0.0.1:9050 or 127.0.0.1:9150
QT_CHROMIUM_ARGS = (
    f"--proxy-server=socks5://{TOR_SOCKS} "
    "--disable-webrtc "
    "--no-sandbox "
    "--disable-gpu "
    "--disable-remote-fonts"
)
os.environ["QTWEBENGINE_CHROMIUM_ARGUMENTS"] = QT_CHROMIUM_ARGS

# Optional: for NEWNYM support (only if you configure Tor ControlPort and install 'stem')
try:
    from stem import Signal
    from stem.control import Controller
    STEM_AVAILABLE = True
except Exception:
    STEM_AVAILABLE = False

# Now safe to import PyQt5
from PyQt5 import QtWidgets, QtCore, QtGui, QtWebEngineWidgets
from PyQt5.QtCore import QUrl, QByteArray, QSize
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile, QWebEnginePage, QWebEngineScript, QWebEngineSettings

# === Profile / UA / Anti-fingerprinting JS ===
PROFILE_DIR = Path(".qt_temp_profile")
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEFAULT_LANG = "en-US,en;q=0.9"

TOR_CONTROL_HOST = "127.0.0.1"
TOR_CONTROL_PORT = 9051
TOR_CONTROL_PASSWORD = None

ANTI_FINGERPRINTING_JS = r"""
(function(){
  try {
    // Block or stub WebRTC
    Object.defineProperty(window, 'RTCPeerConnection', {get: function(){ return undefined; }});
    Object.defineProperty(window, 'webkitRTCPeerConnection', {get: function(){ return undefined; }});
    if (navigator && navigator.mediaDevices) {
      navigator.mediaDevices.enumerateDevices = function(){ return Promise.resolve([]); };
      navigator.mediaDevices.getUserMedia = function(){ return Promise.reject(new Error('getUserMedia disabled')); };
    }
    window.MediaStream = undefined;
  } catch(e){}

  try {
    // Canvas: return tiny deterministic image
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function() {
      const w = 1, h = 1;
      const c = document.createElement('canvas'); c.width = w; c.height = h;
      return origToDataURL.apply(c, arguments);
    };
    HTMLCanvasElement.prototype.getContext = (function(orig){
      return function(type, attrs){
        if(type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl') {
          return orig.call(this, '2d', attrs);
        }
        return orig.call(this, type, attrs);
      };
    })(HTMLCanvasElement.prototype.getContext);
  } catch(e){}

  try {
    if (window.WebGLRenderingContext) {
      const origGetParameter = WebGLRenderingContext.prototype.getParameter;
      WebGLRenderingContext.prototype.getParameter = function(p){
        if (p === 37445 || p === 37446) { return "Intel Inc."; }
        return origGetParameter.apply(this, arguments);
      };
    }
  } catch(e){}

  try { window.AudioContext = undefined; window.webkitAudioContext = undefined; } catch(e){}
  try {
    if (navigator.geolocation) navigator.geolocation.getCurrentPosition = function(){ throw new Error('geolocation disabled'); };
    if (navigator.getBattery) navigator.getBattery = undefined;
    if (window.DeviceOrientationEvent) window.DeviceOrientationEvent = undefined;
    if (window.DeviceMotionEvent) window.DeviceMotionEvent = undefined;
  } catch(e){}

  try { Object.defineProperty(navigator, 'plugins', {get: function(){ return []; }}); } catch(e){}
  try { Object.defineProperty(navigator, 'mimeTypes', {get: function(){ return []; }}); } catch(e){}
  try { Object.defineProperty(navigator, 'userAgent', {get: function(){ return "%s"; }}); } catch(e){}
  try { Object.defineProperty(navigator, 'languages', {get: function(){ return ["%s"]; }}); } catch(e){}
})();
""" % (DEFAULT_UA.replace('"', '\\"'), DEFAULT_LANG)

# === helpers ===
def ensure_profile_dir():
    if PROFILE_DIR.exists():
        _shutil.rmtree(PROFILE_DIR)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

def cleanup_profile_dir():
    try:
        if PROFILE_DIR.exists():
            _shutil.rmtree(PROFILE_DIR)
    except Exception:
        pass

def normalize_url(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    # do not add http for data:, about:, file:, chrome:, qrc: and full urls
    if any(lowered.startswith(s) for s in ("http://", "https://", "file:", "about:", "data:")):
        return text
    # if it looks like a host or onion, add http
    return "http://" + text

# === Qt window ===
class ResourceBlocker(QtCore.QObject):
    """Placeholder if you want to implement QWebEngineUrlRequestInterceptor later."""
    def __init__(self, profile):
        super().__init__()
        self.profile = profile

class BrowserWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tuff Sigma Browser")
        self.resize(1200, 800)

        # Styling (dark)
        self.setStyleSheet("""
            QMainWindow { background-color: #2b2b2b; color: #e0e0e0; }
            QLineEdit { background-color: #333333; color: #e0e0e0; border: 1px solid #555; border-radius: 4px; padding: 6px 8px; font-size: 14px;}
            QPushButton { background-color: #404040; color: #e0e0e0; border: 1px solid #555; border-radius: 4px; padding: 6px 10px; }
            QPushButton#newnym { background-color: #2d5b8a; }
            QLabel#title { color: #4a9eff; font-size: 16px; font-weight: bold; }
            QProgressBar { border: 0; background-color: transparent; height: 6px; }
            QProgressBar::chunk { background-color: #4a9eff; }
        """)

        # Create ephemeral profile
        self.profile = QWebEngineProfile(str(PROFILE_DIR.resolve()), self)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        self.profile.setHttpUserAgent(DEFAULT_UA)
        self.profile.setHttpAcceptLanguage(DEFAULT_LANG)

        # Minimal privacy-oriented settings
        QWebEngineSettings.globalSettings().setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, False)
        QWebEngineSettings.globalSettings().setAttribute(QWebEngineSettings.PluginsEnabled, False)
        QWebEngineSettings.globalSettings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, False)

        # Page & view
        self.page = QWebEnginePage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)

        # Inject anti-fingerprinting JS early
        try:
            script = QWebEngineScript()
            script.setName("antifp")
            script.setSourceCode(ANTI_FINGERPRINTING_JS)
            script.setInjectionPoint(QWebEngineScript.DocumentCreation)
            # Use MainWorld to override navigator etc.
            script.setWorldId(QWebEngineScript.MainWorld)
            script.setRunsOnSubFrames(True)
            self.profile.scripts().insert(script)
        except Exception as e:
            print("Failed to inject anti-fingerprinting script:", e, file=sys.stderr)

        # Build UI
        self.create_ui()
        self.connect_signals()

        # Start page
        self.load_url("https://google.com")

        # cleanup on quit, use QApplication.instance() to avoid referencing global 'app' here
        inst = QtWidgets.QApplication.instance()
        if inst:
            inst.aboutToQuit.connect(self._cleanup_on_quit)

    def create_ui(self):
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Toolbar
        toolbar = QtWidgets.QWidget()
        toolbar.setFixedHeight(70)
        toolbar.setStyleSheet("background-color: #353535; border-bottom: 1px solid #555;")
        toolbar_layout = QtWidgets.QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 5, 10, 5)
        toolbar_layout.setSpacing(8)

        title_label = QtWidgets.QLabel("Secure Tor Browser")
        title_label.setObjectName("title")

        # nav buttons using standard icons (reliable)
        style = QtWidgets.QApplication.style()
        back_btn = QtWidgets.QPushButton()
        back_btn.setIcon(style.standardIcon(QtWidgets.QStyle.SP_ArrowBack))
        back_btn.setFixedSize(36, 36)

        forward_btn = QtWidgets.QPushButton()
        forward_btn.setIcon(style.standardIcon(QtWidgets.QStyle.SP_ArrowForward))
        forward_btn.setFixedSize(36, 36)

        reload_btn = QtWidgets.QPushButton()
        reload_btn.setIcon(style.standardIcon(QtWidgets.QStyle.SP_BrowserReload))
        reload_btn.setFixedSize(36, 36)

        home_btn = QtWidgets.QPushButton()
        home_btn.setIcon(style.standardIcon(QtWidgets.QStyle.SP_DialogOpenButton))
        home_btn.setFixedSize(36, 36)

        nav_widget = QtWidgets.QWidget()
        nav_layout = QtWidgets.QHBoxLayout(nav_widget)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(6)
        nav_layout.addWidget(back_btn)
        nav_layout.addWidget(forward_btn)
        nav_layout.addWidget(reload_btn)
        nav_layout.addWidget(home_btn)

        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("Enter URL (onion sites supported, e.g. exampleonion.onion)")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setMinimumWidth(400)

        go_btn = QtWidgets.QPushButton("Go")
        newnym_btn = QtWidgets.QPushButton("New Identity")
        newnym_btn.setObjectName("newnym")

        status_widget = QtWidgets.QWidget()
        status_layout = QtWidgets.QHBoxLayout(status_widget)
        status_layout.setContentsMargins(0, 0, 0, 0)
        self.status = QtWidgets.QLabel("Ready")
        self.status.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        status_layout.addWidget(self.status)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)

        toolbar_layout.addWidget(title_label)
        toolbar_layout.addSpacing(8)
        toolbar_layout.addWidget(nav_widget)
        toolbar_layout.addSpacing(6)
        toolbar_layout.addWidget(self.url_edit, 1)
        toolbar_layout.addWidget(go_btn)
        toolbar_layout.addWidget(newnym_btn)
        toolbar_layout.addSpacing(12)
        toolbar_layout.addWidget(status_widget, 0)

        main_layout.addWidget(toolbar)
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.view, 1)

        # store refs
        self.back_btn = back_btn
        self.forward_btn = forward_btn
        self.reload_btn = reload_btn
        self.home_btn = home_btn
        self.go_btn = go_btn
        self.newnym_btn = newnym_btn

        # optionally disable default context menu on the page (improves privacy slightly)
        self.view.setContextMenuPolicy(QtCore.Qt.NoContextMenu)

    def connect_signals(self):
        self.back_btn.clicked.connect(self.view.back)
        self.forward_btn.clicked.connect(self.view.forward)
        self.reload_btn.clicked.connect(self.view.reload)
        self.home_btn.clicked.connect(self.go_home)
        self.go_btn.clicked.connect(self.load_url_from_edit)
        self.url_edit.returnPressed.connect(self.load_url_from_edit)
        self.newnym_btn.clicked.connect(self.request_newnym)

        self.view.urlChanged.connect(self.url_changed)
        self.view.loadStarted.connect(self.load_started)
        self.view.loadProgress.connect(self.load_progress)
        self.view.loadFinished.connect(self.load_finished)
        self.view.loadFinished.connect(self.update_navigation_buttons)

    def load_url(self, url):
        norm = normalize_url(url)
        if not norm:
            return
        self.url_edit.setText(norm)
        try:
            qurl = QUrl(norm)
            self.view.setUrl(qurl)
        except Exception as e:
            self.status.setText("Invalid URL")
            print("Invalid URL:", e, file=sys.stderr)

    def load_url_from_edit(self):
        self.load_url(self.url_edit.text())

    def go_home(self):
        self.load_url("https://check.torproject.org/")

    def url_changed(self, url: QUrl):
        self.url_edit.setText(url.toString())

    def load_started(self):
        self.status.setText("Loading...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

    def load_progress(self, progress: int):
        self.progress_bar.setValue(progress)

    def load_finished(self, success: bool):
        self.progress_bar.setVisible(False)
        if success:
            self.status.setText("Loaded")
        else:
            self.status.setText("Failed to load")

    def update_navigation_buttons(self):
        self.back_btn.setEnabled(self.view.history().canGoBack())
        self.forward_btn.setEnabled(self.view.history().canGoForward())

    def request_newnym(self):
        if not STEM_AVAILABLE:
            QtWidgets.QMessageBox.warning(self, "NEWNYM", "Stem library not installed. Install 'stem' to enable NEWNYM.")
            return
        try:
            with Controller.from_port(address=TOR_CONTROL_HOST, port=TOR_CONTROL_PORT) as controller:
                if TOR_CONTROL_PASSWORD:
                    controller.authenticate(password=TOR_CONTROL_PASSWORD)
                else:
                    controller.authenticate()
                controller.signal(Signal.NEWNYM)
            QtWidgets.QMessageBox.information(self, "NEWNYM", "Requested a new Tor circuit (NEWNYM).")
            self.status.setText("New identity requested")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "NEWNYM failed", f"Could not request NEWNYM: {e}")
            self.status.setText("NEWNYM failed")

    def _cleanup_on_quit(self):
        try:
            # Clear some stored data
            try:
                self.profile.clearHttpCache()
                self.profile.clearAllVisitedLinks()
            except Exception:
                pass
        except Exception:
            pass
        cleanup_profile_dir()

def validate_tor_socks(val: str) -> bool:
    if ":" not in val:
        return False
    host, port = val.split(":", 1)
    return host and port.isdigit()

# === Main entry ===
if __name__ == "__main__":
    if not validate_tor_socks(TOR_SOCKS):
        print(f"Warning: TOR_SOCKS format looks wrong: '{TOR_SOCKS}'. Expected host:port (e.g. 127.0.0.1:9050).")

    ensure_profile_dir()
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Segoe UI", 10))

    # set default UA on default profile as a best effort
    try:
        QWebEngineProfile.defaultProfile().setHttpUserAgent(DEFAULT_UA)
    except Exception:
        pass

    w = BrowserWindow()
    w.show()
    try:
        ret = app.exec_()
    finally:
        cleanup_profile_dir()
    sys.exit(ret)
