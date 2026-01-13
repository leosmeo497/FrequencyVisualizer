import tkinter as tk
import pyaudio
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from tkinter import filedialog
import threading, time
import sys
import os
import json
from datetime import datetime
from pathlib import Path
from scipy.signal import butter, filtfilt

def get_config_path():
    """Get path to config file in user's AppData."""
    appdata = os.environ.get('APPDATA', os.path.expanduser('~'))
    config_dir = Path(appdata) / "FrequencyVisualizer"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"

def get_windows_desktop():
    """Get the actual Windows Desktop path (handles OneDrive redirection)."""
    try:
        import ctypes
        from ctypes import wintypes
        
        # CSIDL_DESKTOP = 0x0000, CSIDL_DESKTOPDIRECTORY = 0x0010
        CSIDL_DESKTOPDIRECTORY = 0x0010
        SHGFP_TYPE_CURRENT = 0
        
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, SHGFP_TYPE_CURRENT, buf)
        
        if buf.value:
            return Path(buf.value)
    except Exception:
        pass
    
    # Fallback to common OneDrive paths then default
    home = Path(os.path.expanduser("~"))
    onedrive_desktop = home / "OneDrive" / "Desktop"
    if onedrive_desktop.exists():
        return onedrive_desktop
    
    # Check for OneDrive with organization name
    for item in home.iterdir():
        if item.is_dir() and item.name.startswith("OneDrive"):
            od_desktop = item / "Desktop"
            if od_desktop.exists():
                return od_desktop
    
    return home / "Desktop"

def search_for_folder(folder_name="SavedFrequencies"):
    """Search common locations for the SavedFrequencies folder."""
    home = Path(os.path.expanduser("~"))
    desktop = get_windows_desktop()
    
    # Common locations to search
    search_locations = [
        desktop,
        home / "Desktop",
        home / "Documents",
        home / "Pictures",
        home / "Downloads",
        home,
        Path("C:/"),
        Path("D:/"),
        Path("E:/"),
    ]
    
    # Also check OneDrive folders
    for item in home.iterdir():
        if item.is_dir() and item.name.startswith("OneDrive"):
            search_locations.insert(1, item / "Desktop")
            search_locations.insert(2, item / "Documents")
    
    for location in search_locations:
        if not location.exists():
            continue
        # Check direct child
        target = location / folder_name
        if target.exists() and target.is_dir():
            return target
        # Search one level deeper
        try:
            for subdir in location.iterdir():
                if subdir.is_dir():
                    target = subdir / folder_name
                    if target.exists() and target.is_dir():
                        return target
        except PermissionError:
            continue
    
    return None

def get_save_folder():
    """Get the save folder path, searching for it or creating on Desktop if needed."""
    config_path = get_config_path()
    
    # Try to load existing folder path from config
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                saved_path = Path(config.get('save_folder', ''))
                if saved_path.exists():
                    return saved_path
        except (json.JSONDecodeError, IOError):
            pass
    
    # Folder not at saved location - search for it
    found_folder = search_for_folder("SavedFrequencies")
    if found_folder:
        # Update config with new location
        with open(config_path, 'w') as f:
            json.dump({'save_folder': str(found_folder)}, f)
        return found_folder
    
    # Not found anywhere - create new folder on actual Desktop
    desktop = get_windows_desktop() / "SavedFrequencies"
    desktop.mkdir(parents=True, exist_ok=True)
    
    # Save path to config
    with open(config_path, 'w') as f:
        json.dump({'save_folder': str(desktop)}, f)
    
    return desktop

def update_save_folder(new_path):
    """Update the saved folder path in config."""
    config_path = get_config_path()
    with open(config_path, 'w') as f:
        json.dump({'save_folder': str(new_path)}, f)

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    if hasattr(sys, '_MEIPASS'):
        # Running as compiled exe
        return os.path.join(sys._MEIPASS, relative_path)
    # Running as script
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

# =========================
# CONSTANTS
# =========================
RATE = 44100
CHUNK = 1024
FRAME = 2048
HOP = 512

SPEED_OF_SOUND = 343.0
A4 = 440.0
NOTE_NAMES = ['C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B']

CURSOR_DT = 0.01
CURSOR_DF = 5.0
DRAG_THRESHOLD = 0.01

MAX_FREQ = 20000
LOW_RANGE_MAX = 2000

# =========================
# AUDIO DEVICE
# =========================
def get_input_device():
    p = pyaudio.PyAudio()
    try:
        default_index = p.get_default_input_device_info()['index']
        p.terminate()
        return default_index
    except Exception:
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                p.terminate()
                return i
        p.terminate()
        return None

# =========================
# DSP
# =========================
def bandpass(x, fmax=MAX_FREQ):
    b, a = butter(4, [20/(RATE/2), fmax/(RATE/2)], btype='band')
    return filtfilt(b, a, x)

def yin_pitch(x, sr=RATE, fmin=50, fmax=2000, threshold=0.15):
    """
    YIN fundamental frequency estimator.
    
    Design decisions:
    - Uses difference function instead of correlation to avoid harmonic bias
    - Cumulative mean normalized difference (CMND) provides reliable peak detection
    - Parabolic interpolation for sub-sample accuracy
    - Threshold-based confidence rejection of unreliable estimates
    - Energy gating prevents spurious detection in silence
    
    Args:
        x: audio frame (1D array)
        sr: sample rate
        fmin: minimum expected frequency (Hz)
        fmax: maximum expected frequency (Hz)
        threshold: CMND threshold for acceptance (0.1-0.2 typical)
    
    Returns:
        Frequency in Hz, or 0.0 if no reliable pitch detected
    """
    # Energy-based rejection: skip silent/noisy frames
    x = x - np.mean(x)
    rms = np.sqrt(np.mean(x**2))
    if rms < 0.01:  # Silence threshold
        return 0.0
    
    # Compute lag bounds
    tau_min = int(sr / fmax)
    tau_max = int(sr / fmin)
    
    if tau_max >= len(x):
        tau_max = len(x) - 1
    
    # Step 1: Difference function
    # d(tau) = sum((x[j] - x[j+tau])^2)
    df = np.zeros(tau_max + 1)
    for tau in range(1, tau_max + 1):
        df[tau] = np.sum((x[:len(x)-tau] - x[tau:]) ** 2)
    
    # Step 2: Cumulative mean normalized difference (CMND)
    # This is the key innovation of YIN - reduces harmonic locking
    cmndf = np.ones_like(df)
    cmndf[0] = 1.0
    cumsum = 0.0
    for tau in range(1, len(df)):
        cumsum += df[tau]
        cmndf[tau] = df[tau] / (cumsum / tau) if cumsum > 0 else 1.0
    
    # Step 3: Absolute threshold search
    # Find first tau where CMND drops below threshold
    tau_candidates = []
    for tau in range(tau_min, tau_max):
        if cmndf[tau] < threshold:
            # Look for local minimum after threshold crossing
            while tau + 1 < tau_max and cmndf[tau + 1] < cmndf[tau]:
                tau += 1
            tau_candidates.append(tau)
            break
    
    if not tau_candidates:
        return 0.0
    
    tau_estimate = tau_candidates[0]
    
    # Step 4: Parabolic interpolation for sub-sample precision
    if 0 < tau_estimate < len(cmndf) - 1:
        alpha = cmndf[tau_estimate - 1]
        beta = cmndf[tau_estimate]
        gamma = cmndf[tau_estimate + 1]
        
        # Parabolic peak location
        if (2 * beta - alpha - gamma) != 0:  # Avoid division by zero
            delta = (alpha - gamma) / (2 * (2 * beta - alpha - gamma))
            if abs(delta) < 1.0:  # Sanity check
                tau_estimate = tau_estimate + delta
    
    # Convert lag to frequency
    f0 = sr / tau_estimate
    
    # Final sanity checks: reject if outside expected range
    if f0 < fmin or f0 > fmax:
        return 0.0
    
    # Additional validation: ensure sufficient cycles in the frame
    # At least 2 complete cycles needed for reliable detection
    min_cycles = 2.0
    frame_duration = len(x) / sr
    if f0 * frame_duration < min_cycles:
        return 0.0
    
    # Confidence check: CMND value should be sufficiently low
    if tau_estimate < len(cmndf) and cmndf[int(tau_estimate)] > threshold * 2:
        return 0.0
    
    return f0

def freq_to_note(f):
    if f <= 0:
        return None, None
    n = 12 * np.log2(f / A4) + 69
    idx = int(round(n))
    cents = (n - idx) * 100
    return f"{NOTE_NAMES[idx % 12]}{idx//12-1}", cents

# =========================
# APP
# =========================
class App:
    def __init__(self, root):
        self.root = root
        root.title("HZMeter")

        self.audio = None
        self.markers = []
        self.show_harmonics = True
        self.cursor_t = 0.0
        self.cursor_f = 440.0
        self.dragging = False
        self.drag_start = None
        self.drag_xlim = None
        self.drag_ylim = None
        self.max_t = 1.0
        self.max_f = 1000.0
        self.hover_marker_idx = None  # Track marker being hovered over
        self.cursor_vline = None  # Vertical cursor line
        self.cursor_hline = None  # Horizontal cursor line
        self.marker_connections = []  # Store marker connection pairs for hover detection

        self.view_var = tk.StringVar(value="linear")
        self.mode_var = tk.StringVar(value="Low Range Mode")
        self.harmonics_var = tk.BooleanVar(value=self.show_harmonics)
        self.show_wavelength_in_png = tk.BooleanVar(value=True)
        self.show_note_in_png = tk.BooleanVar(value=True)
        self.dark_mode = False  # Track dark mode state
        
        # Store references to UI elements for theming
        self.ui_elements = []

        # ===== UI =====
        self.duration_label = tk.Label(root, text="Duration (s)")
        self.duration_label.pack()
        self.dur = tk.Entry(root)
        self.dur.insert(0, "5")
        self.dur.pack()

        self.btn = tk.Button(root, text="Record [R]", command=self.start)
        self.btn.pack()

        self.info = tk.Label(root, text="")
        self.info.pack()

        self.fig, self.ax = plt.subplots()
        self.canvas = FigureCanvasTkAgg(self.fig, root)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # ===== Events =====
        self.canvas.mpl_connect("motion_notify_event", self.on_motion)
        self.canvas.mpl_connect("button_press_event", self.on_press)
        self.canvas.mpl_connect("button_release_event", self.on_release)
        self.canvas.mpl_connect("scroll_event", self.zoom)

        # ===== Keybinds =====
        root.bind_all("r", lambda e: self.start())
        root.bind_all("R", lambda e: self.start())
        root.bind_all("l", lambda e: self.set_view("linear"))
        root.bind_all("L", lambda e: self.set_view("linear"))
        root.bind_all("s", lambda e: self.set_view("spec"))
        root.bind_all("S", lambda e: self.set_view("spec"))
        root.bind_all("h", lambda e: self.toggle_harmonics())
        root.bind_all("H", lambda e: self.toggle_harmonics())
        root.bind_all("z", lambda e: self.reset_zoom())
        root.bind_all("Z", lambda e: self.reset_zoom())
        root.bind_all("<Left>", lambda e: self.pan_view(-0.1, 0))
        root.bind_all("<Right>", lambda e: self.pan_view(0.1, 0))
        root.bind_all("<Up>", lambda e: self.pan_view(0, 0.1))
        root.bind_all("<Down>", lambda e: self.pan_view(0, -0.1))
        root.bind_all("<Escape>", lambda e: root.quit())
        root.bind_all("c", lambda e: self.clear_markers())
        root.bind_all("C", lambda e: self.clear_markers())
        root.bind_all("d", lambda e: self.toggle_dark_mode())
        root.bind_all("D", lambda e: self.toggle_dark_mode())

        # ===== Menus =====
        self.menubar = tk.Menu(root)
        root.config(menu=self.menubar)

        # File menu
        self.file_menu = tk.Menu(self.menubar, tearoff=0)
        self.menubar.add_cascade(label="File", menu=self.file_menu)
        self.file_menu.add_command(label="Save as PNG", command=self.save_png)
        self.file_menu.add_separator()
        self.file_menu.add_checkbutton(label="Show Wavelength in PNG", variable=self.show_wavelength_in_png)
        self.file_menu.add_checkbutton(label="Show Musical Note in PNG", variable=self.show_note_in_png)

        # View menu
        self.view_menu = tk.Menu(self.menubar, tearoff=0)
        self.menubar.add_cascade(label="View", menu=self.view_menu)
        self.view_menu.add_radiobutton(label="Linear [L]", variable=self.view_var, value="linear", command=lambda: self.set_view("linear"))
        self.view_menu.add_radiobutton(label="Spectrogram [S]", variable=self.view_var, value="spec", command=lambda: self.set_view("spec"))
        self.view_menu.add_checkbutton(label="Show Harmonics [H]", variable=self.harmonics_var, command=self.toggle_harmonics)
        self.view_menu.add_separator()
        self.view_menu.add_command(label="Reset Zoom [Z]", command=self.reset_zoom)

        # Options menu
        self.opt_menu = tk.Menu(self.menubar, tearoff=0)
        self.menubar.add_cascade(label="Options", menu=self.opt_menu)
        self.opt_menu.add_command(label="Clear Markers [C]", command=self.clear_markers)
        self.opt_menu.add_separator()
        self.opt_menu.add_radiobutton(label="Low Range Mode", variable=self.mode_var, value="Low Range Mode", command=lambda: self.set_mode("Low Range Mode"))
        self.opt_menu.add_radiobutton(label="Full Range Mode", variable=self.mode_var, value="Full Range Mode", command=lambda: self.set_mode("Full Range Mode"))
        self.opt_menu.add_separator()
        # Dark mode toggle - index 5 in menu
        self.opt_menu.add_command(label="Dark Mode [D]", command=self.toggle_dark_mode)
        
        # Misc menu (rightmost - added last to appear on far right)
        self.misc_menu = tk.Menu(self.menubar, tearoff=0)
        self.menubar.add_cascade(label="Misc", menu=self.misc_menu)
        self.misc_menu.add_command(label="Keybinds", command=self.show_keybinds)
        self.misc_menu.add_command(label="Help", command=self.show_help)
        self.misc_menu.add_command(label="Documentation", command=self.show_documentation)
        self.misc_menu.add_command(label="Credits", command=self.show_credits)

    # =========================
    # POPUPS
    # =========================
    def show_documentation(self):
        """Display development documentation."""
        doc_win = tk.Toplevel(self.root)
        doc_win.title("Documentation - Development Process")
        doc_win.geometry("800x700")
        
        doc_text = (
            "INTRODUCTION\n\n"
            "This application was developed as part of a Gymnasiearbete (Upper Secondary "
            "School Project) with the overall theme \"Sound Frequencies and Interaction.\"\n\n"
            "The project focuses on understanding how sound frequencies behave, interact, "
            "and influence one another in both physical and digital environments.\n\n"
            "Rather than relying exclusively on existing audio analysis software, this "
            "project includes the development of a custom-built frequency analysis tool, "
            "designed specifically to support the experiments and analysis required for "
            "the study.\n\n"
            "Although professional and more advanced tools already exist, creating a "
            "self-developed application allowed for greater control, deeper understanding, "
            "and optimization for the specific needs of the project and its author.\n\n\n"
            
            "PURPOSE OF THE APPLICATION\n\n"
            "The primary purpose of the application is to visualize and analyze sound "
            "frequencies over time, enabling investigation of how multiple frequencies "
            "interact when played simultaneously and how environmental factors affect "
            "sound behavior.\n\n"
            "The application was created to support experimental work related to:\n"
            "  • Frequency interaction and interference\n"
            "  • Harmonics and beat frequencies\n"
            "  • The influence of room acoustics and physical objects\n"
            "  • Volume balance and perceived sound clarity\n"
            "  • Practical applications in music production, stage technology, and sound engineering\n\n"
            "By designing the tool personally, it became possible to tailor the interface, "
            "controls, and visualizations to match how the data needed to be interpreted "
            "during the experiments.\n\n\n"
            
            "MOTIVATION FOR A SELF-BUILT TOOL\n\n"
            "While software such as Audacity, REAPER, and professional spectrum analyzers "
            "provide powerful features, they are designed for general-purpose use. For "
            "this project, a custom solution was preferred for several reasons:\n\n"
            "  • To fully understand how audio analysis works internally\n"
            "  • To remove unnecessary features and focus only on relevant data\n"
            "  • To optimize frequency ranges and visual scaling for specific experiments\n"
            "  • To directly connect theoretical physics concepts with real-time visual feedback\n"
            "  • To adapt the tool continuously as the project evolved\n\n"
            "Developing the application became an integral part of the learning process "
            "and contributed directly to the understanding of sound behavior.\n\n\n"
            
            "USE OF AI TOOLS\n\n"
            "AI tools were used as supportive learning and development aids, not as "
            "replacements for understanding or decision-making.\n\n"
            "ChatGPT was used for:\n"
            "  • Conceptual explanations of sound analysis\n"
            "  • Suggestions for visualization ideas and interface design\n"
            "  • Understanding why certain frequency artifacts appear\n"
            "  • Exploring possible improvements and extensions\n\n"
            "GitHub Copilot was used mainly for:\n"
            "  • Debugging syntax errors\n"
            "  • Fixing small logical mistakes\n"
            "  • Improving code readability\n"
            "  • Speeding up repetitive coding tasks\n\n"
            "All AI-generated suggestions were reviewed, tested, and adjusted manually to "
            "ensure correctness and learning value.\n\n\n"
            
            "CURRENT STATE AND LIMITATIONS\n\n"
            "The application functions as an experimental analysis tool rather than a "
            "professional measurement instrument. Known limitations include:\n\n"
            "  • Sensitivity to background noise\n"
            "  • Dependence on microphone quality and placement\n"
            "  • Reduced accuracy for very complex or noisy signals\n"
            "  • Trade-offs between smoothing and responsiveness\n\n"
            "Despite these limitations, the application fulfills its intended role as a "
            "learning-oriented, experiment-focused analysis tool.\n\n\n"
            
            "CONCLUSION\n\n"
            "This project represents a combined effort in physics, programming, and "
            "experimental analysis, developed as part of a Gymnasiearbete."
        )
        
        # Use Text widget instead of Label for better text handling
        text_widget = tk.Text(doc_win, wrap=tk.WORD, font=("Courier", 9), 
                             padx=15, pady=15, bg="white", fg="black")
        scrollbar = tk.Scrollbar(doc_win, command=text_widget.yview)
        text_widget.config(yscrollcommand=scrollbar.set)
        
        text_widget.insert("1.0", doc_text)
        text_widget.config(state=tk.DISABLED)  # Make read-only
        
        text_widget.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
    
    def show_credits(self):
        """Display credits with clickable links."""
        credits_win = tk.Toplevel(self.root)
        credits_win.title("Credits")
        credits_win.geometry("600x400")
        
        # Main frame with padding
        main_frame = tk.Frame(credits_win, padx=20, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        tk.Label(main_frame, text="Credits", font=("Arial", 16, "bold")).pack(pady=10)
        
        # Developer
        tk.Label(main_frame, text="Student:", font=("Arial", 10, "bold")).pack(anchor="w")
        tk.Label(main_frame, text="Thor Leopold Hammerstein Werner", font=("Arial", 9)).pack(anchor="w", padx=20)
        school_link = tk.Label(main_frame, text="Tyska Skolan Stockholm", 
                              fg="blue", cursor="hand2", font=("Arial", 10, "underline"))
        school_link.pack(anchor="w", padx=20)
        school_link.bind("<Button-1>", lambda e: self.open_url("https://tyskaskolan.se/sv/home-svenska/"))
        
        # Supervisor
        tk.Label(main_frame, text="\nEducation & Mentorship:", font=("Arial", 10, "bold")).pack(anchor="w")
        tk.Label(main_frame, text="Tekn. Dr. Peter Hammerstein", font=("Arial", 9)).pack(anchor="w", padx=20)
        profession_frame = tk.Frame(main_frame)
        profession_frame.pack(anchor="w", padx=20)
        tk.Label(profession_frame, text="Profession: ", font=("Arial", 9)).pack(side="left")
        theory_link = tk.Label(profession_frame, text="Theoretical Physics In Chaos Theory", 
                              fg="blue", cursor="hand2", font=("Arial", 9, "underline"))
        theory_link.pack(side="left")
        theory_link.bind("<Button-1>", lambda e: self.open_url("https://hammerstein.se/PH_professional.html"))
        
        # AI Tools
        tk.Label(main_frame, text="\nAI Tools Used:", font=("Arial", 10, "bold")).pack(anchor="w")
        chatgpt_link = tk.Label(main_frame, text="ChatGPT", 
                               fg="blue", cursor="hand2", font=("Arial", 10, "underline"))
        chatgpt_link.pack(anchor="w", padx=20)
        chatgpt_link.bind("<Button-1>", lambda e: self.open_url("https://openai.com/"))
        
        copilot_link = tk.Label(main_frame, text="GitHub Copilot", 
                               fg="blue", cursor="hand2", font=("Arial", 10, "underline"))
        copilot_link.pack(anchor="w", padx=20)
        copilot_link.bind("<Button-1>", lambda e: self.open_url("https://github.com/features/copilot"))
        
        # Development Environment
        tk.Label(main_frame, text="\nDevelopment Environment:", font=("Arial", 10, "bold")).pack(anchor="w")
        vscode_link = tk.Label(main_frame, text="Visual Studio Code™", 
                              fg="blue", cursor="hand2", font=("Arial", 10, "underline"))
        vscode_link.pack(anchor="w", padx=20)
        vscode_link.bind("<Button-1>", lambda e: self.open_url("https://code.visualstudio.com/"))
    
    def open_url(self, url):
        """Open URL in default browser."""
        import webbrowser
        webbrowser.open(url)
    
    def show_help(self):
        help_win = tk.Toplevel(self.root)
        help_win.title("Help - How to Use HZMeter")
        help_win.geometry("700x600")
        
        # Create scrollable frame
        canvas = tk.Canvas(help_win)
        scrollbar = tk.Scrollbar(help_win, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        help_text = (
            "═══════════════════════════════════════════════════════════\n"
            "                    HZMETER - AUDIO ANALYZER\n"
            "═══════════════════════════════════════════════════════════\n\n"
            
            "OVERVIEW\n"
            "────────────────────────────────────────────────────────\n"
            "HZMeter is a real-time audio analysis tool that records microphone\n"
            "input and displays frequency information in two ways:\n"
            "  • Linear View: Continuous pitch tracking over time\n"
            "  • Spectrogram: Full frequency spectrum visualization\n\n"
            
            "HOW TO USE\n"
            "────────────────────────────────────────────────────────\n"
            "1. Set Recording Duration: Enter desired seconds (e.g., 5)\n"
            "2. Click 'Record' or press 'R' to start recording\n"
            "3. Make sound into your microphone (speak, sing, play instrument)\n"
            "4. Wait for analysis to complete\n"
            "5. View results in Linear or Spectrogram mode\n\n"
            
            "FUNCTIONS\n"
            "────────────────────────────────────────────────────────\n"
            "• Recording: Captures audio from default microphone\n"
            "• Linear View [L]: Shows fundamental frequency (pitch) over time\n"
            "  - Displays 2nd and 3rd harmonics (toggle with 'H')\n"
            "  - Smooth continuous curves for accurate pitch tracking\n"
            "• Spectrogram [S]: Shows all frequencies as a heat map\n"
            "  - Time on X-axis, Frequency on Y-axis\n"
            "  - Color intensity shows signal strength\n"
            "• Markers: Click to place markers for measurement\n"
            "  - Displays time, frequency, wavelength, and musical note\n"
            "  - Multiple markers connect to nearest neighbor\n"
            "  - Hover over markers to see detailed info\n"
            "  - Right-click to remove nearest marker\n"
            "  - Clear all markers with 'C'\n"
            "• Zoom: Mouse scroll to zoom in/out\n"
            "  - Y-axis unlimited when zooming out\n"
            "  - X-axis limited to recording duration\n"
            "• Pan: Arrow keys to move view\n"
            "• Reset Zoom [Z]: Reset view to default\n"
            "• Dark Mode [D]: Toggle dark/light theme\n"
            "• Modes:\n"
            "  - Low Range (50-2000 Hz): Optimized for human voice\n"
            "  - Full Range (50-20000 Hz): For instruments and higher pitches\n\n"
            
            "OPTIMAL RECORDING TIPS\n"
            "────────────────────────────────────────────────────────\n"
            "✓ Record in a quiet environment (minimize background noise)\n"
            "✓ Keep microphone 6-12 inches from sound source\n"
            "✓ Speak/sing clearly and steadily for best pitch tracking\n"
            "✓ Use Low Range Mode for speech and singing\n"
            "✓ Use Full Range Mode for instruments (flute, violin, etc.)\n"
            "✓ For sustained notes, record at least 2-3 seconds\n"
            "✓ Avoid clipping (very loud signals) - keep volume moderate\n\n"
            
            "ACCURACY & LIMITATIONS\n"
            "────────────────────────────────────────────────────────\n"
            "• Pitch Detection Accuracy: Typically <1% error on clean signals\n"
            "• Works best with monophonic (single note) sources\n"
            "• Polyphonic (multiple simultaneous notes) may show mixed results\n"
            "• Very short sounds (<100ms) may not be detected reliably\n"
            "• Background noise reduces accuracy - use quiet environment\n"
            "• Frequency range limited by mode selection\n\n"
            
            "POTENTIAL ISSUES & SOLUTIONS\n"
            "────────────────────────────────────────────────────────\n"
            "Issue: No pitch detected (flat line at 0 Hz)\n"
            "  → Solution: Increase volume, reduce background noise\n"
            "  → Check microphone is working and selected properly\n\n"
            
            "Issue: Erratic pitch jumps\n"
            "  → Solution: Use steady tone, avoid vibrato initially\n"
            "  → Switch to Low Range Mode for voice\n\n"
            
            "Issue: Pitch shows as double (octave error)\n"
            "  → Solution: This is rare with YIN algorithm, but try recording\n"
            "             in a quieter environment or closer to microphone\n\n"
            
            "Issue: Spectrogram is too bright/dark\n"
            "  → Solution: Adjust microphone input volume in system settings\n\n"
            
            "Issue: Application freezes during recording\n"
            "  → Solution: Recording happens in background - wait for completion\n"
            "             Reduce duration if system is slow\n\n"
            
            "TECHNICAL SPECIFICATIONS\n"
            "────────────────────────────────────────────────────────\n"
            "• Sample Rate: 44,100 Hz (CD quality)\n"
            "• Pitch Algorithm: YIN (robust fundamental frequency estimator)\n"
            "• Frequency Resolution: ~10.8 Hz (spectrogram)\n"
            "• Temporal Resolution: ~11.6 ms per frame\n"
            "• Detection Range: 50-20000 Hz (mode dependent)\n"
            "• Analysis: Offline (post-recording)\n\n"
            
            "═══════════════════════════════════════════════════════════\n"
            "For all keybinds, see Misc → Keybinds\n"
            "═══════════════════════════════════════════════════════════"
        )
        
        tk.Label(scrollable_frame, text=help_text, justify="left", 
                padx=20, pady=20, font=("Courier", 9)).pack()
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
    
    def show_keybinds(self):
        kb_win = tk.Toplevel(self.root)
        kb_win.title("Keybinds")
        kb_text = (
            "R : Record\n"
            "L : Linear view\n"
            "S : Spectrogram view\n"
            "H : Toggle 2nd/3rd harmonics\n"
            "Z : Reset zoom\n"
            "C : Clear markers\n"
            "D : Toggle Dark/Light mode\n"
            "Arrow Left : Pan view left\n"
            "Arrow Right : Pan view right\n"
            "Arrow Up : Pan view up\n"
            "Arrow Down : Pan view down\n"
            "Escape : Quit\n"
            "Left Click : Add marker\n"
            "Right Click : Remove nearest marker\n"
            "Scroll Up : Zoom in\n"
            "Scroll Down : Zoom out"
        )
        tk.Label(kb_win, text=kb_text, justify="left", padx=10, pady=10).pack()
    
    def toggle_dark_mode(self):
        """Toggle between dark and light mode."""
        self.dark_mode = not self.dark_mode
        
        if self.dark_mode:
            # Dark mode colors
            bg_color = "#171717"       # App background
            ui_bg = "#252424BD"          # UI element backgrounds (slightly darker than graph)
            graph_bg = "#1F1F1F"       # Inside graph
            fig_bg = "#1B1B1B"         # Outside graph (figure)
            text_color = "#818181"     # Text color
            
            # Update menu label
            self.opt_menu.entryconfig(5, label="Light Mode [D]")
        else:
            # Light mode colors (default)
            bg_color = 'SystemButtonFace'
            ui_bg = 'SystemButtonFace'
            graph_bg = 'white'
            fig_bg = 'white'
            text_color = 'black'
            
            # Update menu label
            self.opt_menu.entryconfig(5, label="Dark Mode [D]")
        
        # Apply to root window
        self.root.configure(bg=bg_color)
        
        # Apply to menu bar and dropdown menus
        menu_bg = ui_bg if self.dark_mode else 'SystemButtonFace'
        menu_fg = text_color
        self.menubar.configure(bg=menu_bg, fg=menu_fg, activebackground='#3D3D3D' if self.dark_mode else 'SystemHighlight', activeforeground=menu_fg)
        for menu in [self.file_menu, self.view_menu, self.opt_menu, self.misc_menu]:
            menu.configure(bg=menu_bg, fg=menu_fg, activebackground='#3D3D3D' if self.dark_mode else 'SystemHighlight', activeforeground=menu_fg)
        
        # Apply to tkinter widgets
        for widget in self.root.winfo_children():
            self._apply_theme_to_widget(widget, bg_color, ui_bg, text_color)
        
        # Apply to matplotlib figure
        self.fig.set_facecolor(fig_bg)
        if hasattr(self, 'ax') and self.ax is not None:
            self.ax.set_facecolor(graph_bg)
            # Update axis colors
            self.ax.spines['bottom'].set_color(text_color)
            self.ax.spines['top'].set_color(text_color)
            self.ax.spines['left'].set_color(text_color)
            self.ax.spines['right'].set_color(text_color)
            self.ax.xaxis.label.set_color(text_color)
            self.ax.yaxis.label.set_color(text_color)
            self.ax.tick_params(axis='x', colors=text_color)
            self.ax.tick_params(axis='y', colors=text_color)
            self.ax.title.set_color(text_color)
            # Update legend if exists
            legend = self.ax.get_legend()
            if legend:
                legend.get_frame().set_facecolor(graph_bg)
                for text in legend.get_texts():
                    text.set_color(text_color)
        
        self.canvas.draw()
    
    def _apply_theme_to_widget(self, widget, bg_color, ui_bg, text_color):
        """Recursively apply theme colors to a widget and its children."""
        widget_type = widget.winfo_class()
        
        try:
            if widget_type in ('Label', 'Button'):
                widget.configure(bg=ui_bg, fg=text_color)
            elif widget_type == 'Entry':
                widget.configure(bg=ui_bg, fg=text_color, insertbackground=text_color)
            elif widget_type == 'Frame':
                widget.configure(bg=bg_color)
            elif widget_type == 'Canvas':
                # This is likely the matplotlib canvas container
                widget.configure(bg=bg_color)
        except tk.TclError:
            pass  # Some widgets may not support all options
        
        # Recursively apply to children
        for child in widget.winfo_children():
            self._apply_theme_to_widget(child, bg_color, ui_bg, text_color)

    # =========================
    # FILE SAVE
    # =========================
    def save_png(self):
        if self.audio is None:
            return
        
        # Get save folder (persists even if user moves it)
        save_folder = get_save_folder()
        
        # Generate unique filename with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
        filename = save_folder / f"FrequencyCapture_{timestamp}.png"
        
        # Fixed graph dimensions: 1800x750 pixels each at 150 dpi
        # 1800px / 150dpi = 12 inches wide, 750px / 150dpi = 5 inches tall per graph
        graph_width_inches = 12  # 1800px at 150dpi
        graph_height_inches = 5  # 750px at 150dpi (per graph)
        total_graph_height = graph_height_inches * 2  # Two graphs stacked
        
        # Calculate how many marker columns we need (10 markers per column)
        markers_per_column = 10
        num_markers = len(self.markers)
        num_marker_columns = max(1, (num_markers + markers_per_column - 1) // markers_per_column) if num_markers > 0 else 0
        
        # Legend column width
        marker_column_width = 3  # Width per marker column in inches
        legend_width = marker_column_width * num_marker_columns if num_markers > 0 else 0
        total_width = graph_width_inches + legend_width
        
        # Create figure with space for marker legend on the right
        if num_markers > 0:
            # Use gridspec for flexible layout - graphs stay fixed size
            save_fig = plt.figure(figsize=(total_width, total_graph_height))
            gs = save_fig.add_gridspec(2, 1 + num_marker_columns, 
                                       width_ratios=[graph_width_inches] + [marker_column_width] * num_marker_columns,
                                       height_ratios=[1, 1])
            ax_spec = save_fig.add_subplot(gs[0, 0])
            ax_linear = save_fig.add_subplot(gs[1, 0])
        else:
            save_fig, (ax_spec, ax_linear) = plt.subplots(2, 1, figsize=(graph_width_inches, total_graph_height))
        
        # Determine frequency range based on mode
        fmax_mode = 2000 if self.mode_var.get() == "Low Range Mode" else MAX_FREQ
        
        # Use hybrid pitch detection for better coverage (same as analyze)
        t, f0 = self._hybrid_pitch_detection(fmax_mode)
        h2 = 2 * f0
        h3 = 3 * f0
        
        max_t = t[-1] if len(t) > 0 else 1.0
        valid_f0 = f0[~np.isnan(f0)]
        max_f = (np.max(valid_f0) + 200) if len(valid_f0) > 0 else 1000
        if self.mode_var.get() == "Low Range Mode":
            max_f = min(max_f, 5000)
        
        # Draw spectrogram (top)
        spectrum, freqs, t_spec, im = ax_spec.specgram(
            self.audio, 
            Fs=RATE, 
            NFFT=4096,
            noverlap=4096-256,
            scale='dB',
            cmap='viridis',
            vmin=-120,
            mode='magnitude'
        )
        im.set_interpolation('bilinear')
        ax_spec.set_ylim(0, max_f)
        ax_spec.set_xlim(0, max_t)
        ax_spec.set_ylabel("Frequency (Hz)")
        ax_spec.set_xlabel("Time (s)")
        ax_spec.set_title("Spectrogram")
        
        # Draw linear view (bottom)
        ax_linear.plot(t, f0, label="Fundamental", linewidth=1.2)
        if self.show_harmonics:
            ax_linear.plot(t, h2, "--", label="2nd Harmonic", linewidth=1, alpha=0.7)
            ax_linear.plot(t, h3, "--", label="3rd Harmonic", linewidth=1, alpha=0.7)
        ax_linear.set_ylabel("Frequency (Hz)")
        ax_linear.set_xlabel("Time (s)")
        ax_linear.set_ylim(0, max_f)
        ax_linear.set_xlim(0, max_t)
        ax_linear.set_title("Pitch Tracking (Linear)")
        ax_linear.legend()
        
        # Draw markers on both plots
        marker_color = 'black'
        sorted_indices = sorted(range(len(self.markers)), key=lambda i: self.markers[i][0])
        
        # Draw connection lines
        for i in range(len(sorted_indices) - 1):
            idx1 = sorted_indices[i]
            idx2 = sorted_indices[i + 1]
            x1, y1 = self.markers[idx1]
            x2, y2 = self.markers[idx2]
            if y1 is not None and y2 is not None:
                ax_spec.plot([x1, x2], [y1, y2], color=marker_color, linewidth=1, alpha=0.5, linestyle='--')
                ax_linear.plot([x1, x2], [y1, y2], color=marker_color, linewidth=1, alpha=0.5, linestyle='--')
        
        # Draw marker points
        for i, (x, y) in enumerate(self.markers):
            if y is not None:
                ax_spec.scatter(x, y, marker='x', color=marker_color, s=78, linewidths=1.5)
                ax_spec.annotate(str(i + 1), (x, y), textcoords="offset points", xytext=(5, 5), fontsize=8, color=marker_color)
                ax_linear.scatter(x, y, marker='x', color=marker_color, s=78, linewidths=1.5)
                ax_linear.annotate(str(i + 1), (x, y), textcoords="offset points", xytext=(5, 5), fontsize=8, color=marker_color)
        
        # Add marker legend on the right side
        if num_markers > 0:
            for col in range(num_marker_columns):
                # Create a text axis for this column (spans both rows)
                ax_legend = save_fig.add_subplot(gs[:, 1 + col])
                ax_legend.axis('off')
                
                # Build marker text for this column
                start_idx = col * markers_per_column
                end_idx = min((col + 1) * markers_per_column, num_markers)
                
                legend_text = "MARKER LEGEND\n" + "─" * 30 + "\n\n" if col == 0 else ""
                
                for i in range(start_idx, end_idx):
                    x, y = self.markers[i]
                    if y is not None and y > 0:
                        note, cents = freq_to_note(y)
                        lam = SPEED_OF_SOUND / y
                        note_str = f"{note} ({cents:+.1f}¢)" if note else "N/A"
                        legend_text += f"MARKER {i + 1}:\n"
                        legend_text += f"  Time = {x:.3f} s\n"
                        legend_text += f"  Freq = {y:.2f} Hz\n"
                        if self.show_wavelength_in_png.get():
                            legend_text += f"  λ = {lam:.3f} m\n"
                        if self.show_note_in_png.get():
                            legend_text += f"  Note = {note_str}\n"
                        legend_text += "\n"
                    else:
                        legend_text += f"MARKER {i + 1}:\n"
                        legend_text += f"  Time = {x:.3f} s\n"
                        legend_text += f"  Freq = N/A\n\n"
                
                ax_legend.text(0.05, 0.98, legend_text, transform=ax_legend.transAxes,
                              fontsize=9, fontfamily='monospace', verticalalignment='top',
                              bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
        
        save_fig.tight_layout()
        save_fig.savefig(filename, dpi=150, bbox_inches='tight')
        plt.close(save_fig)

    # =========================
    # MODE / VIEW
    # =========================
    def set_mode(self, mode):
        self.mode_var.set(mode)
        if self.audio is not None:
            self.analyze()

    def set_view(self, view):
        self.view_var.set(view)
        if self.audio is not None:
            self.analyze()

    # =========================
    # RECORDING
    # =========================
    def start(self):
        self.btn.config(state="disabled")
        threading.Thread(target=self.record, daemon=True).start()

    def record(self):
        dur = float(self.dur.get())
        p = pyaudio.PyAudio()
        stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=RATE,
            input=True,
            input_device_index=get_input_device(),
            frames_per_buffer=CHUNK
        )

        frames = []
        t0 = time.time()
        while time.time() - t0 < dur:
            frames.append(stream.read(CHUNK, exception_on_overflow=False))

        stream.stop_stream()
        stream.close()
        p.terminate()

        audio = np.frombuffer(b"".join(frames), np.int16).astype(np.float32) / 32768
        fmax = 5000 if self.mode_var.get() == "Low Range Mode" else MAX_FREQ
        self.audio = bandpass(audio, fmax=fmax)
        self.root.after(0, self.analyze)

    # =========================
    # UTILITY METHODS
    # =========================
    def get_complementary_color(self):
        """Get complementary color based on current plot background."""
        # Get background color of the axes
        bg_color = self.ax.get_facecolor()
        
        # Convert to RGB (matplotlib returns RGBA with values 0-1)
        r, g, b = bg_color[0], bg_color[1], bg_color[2]
        
        # Calculate complementary color (invert RGB)
        comp_r = 1.0 - r
        comp_g = 1.0 - g
        comp_b = 1.0 - b
        
        return (comp_r, comp_g, comp_b)
    
    def find_closest_marker(self, idx):
        """Find the closest marker to the given marker index."""
        if len(self.markers) < 2:
            return None
        
        x1, y1 = self.markers[idx]
        min_dist = float('inf')
        closest_idx = None
        
        for i, (x2, y2) in enumerate(self.markers):
            if i != idx:
                # Normalize distance in both dimensions
                dx = (x2 - x1) / self.max_t if self.max_t > 0 else 0
                dy = (y2 - y1) / self.max_f if self.max_f > 0 else 0
                dist = np.sqrt(dx**2 + dy**2)
                
                if dist < min_dist:
                    min_dist = dist
                    closest_idx = i
        
        return closest_idx
    
    def is_near_marker(self, x, y, threshold=0.02):
        """Check if cursor is near any marker and return index if so."""
        if not self.markers:
            return None
        
        for i, (mx, my) in enumerate(self.markers):
            # Normalize distance
            dx = abs(x - mx) / self.max_t if self.max_t > 0 else 0
            dy = abs(y - my) / self.max_f if self.max_f > 0 else 0
            dist = np.sqrt(dx**2 + dy**2)
            
            if dist < threshold:
                return i
        
        return None
    
    def is_near_connection_line(self, x, y, threshold=0.015):
        """Check if cursor is near any marker connection line and return the pair indices if so."""
        if not self.marker_connections:
            return None
        
        for idx1, idx2 in self.marker_connections:
            x1, y1 = self.markers[idx1]
            x2, y2 = self.markers[idx2]
            
            if y1 is None or y2 is None:
                continue
            
            # Normalize coordinates
            nx = x / self.max_t if self.max_t > 0 else 0
            ny = y / self.max_f if self.max_f > 0 else 0
            nx1 = x1 / self.max_t if self.max_t > 0 else 0
            ny1 = y1 / self.max_f if self.max_f > 0 else 0
            nx2 = x2 / self.max_t if self.max_t > 0 else 0
            ny2 = y2 / self.max_f if self.max_f > 0 else 0
            
            # Calculate distance from point to line segment
            line_len_sq = (nx2 - nx1)**2 + (ny2 - ny1)**2
            if line_len_sq == 0:
                continue
            
            # Parameter t for closest point on line
            t = max(0, min(1, ((nx - nx1) * (nx2 - nx1) + (ny - ny1) * (ny2 - ny1)) / line_len_sq))
            
            # Closest point on line segment
            closest_x = nx1 + t * (nx2 - nx1)
            closest_y = ny1 + t * (ny2 - ny1)
            
            # Distance to closest point
            dist = np.sqrt((nx - closest_x)**2 + (ny - closest_y)**2)
            
            if dist < threshold:
                return (idx1, idx2)
        
        return None
    
    # =========================
    # ANALYSIS / DRAW
    # =========================
    def _extract_spectrogram_peaks(self, fmin=50, fmax=2000, threshold_db=-60):
        """
        Extract dominant frequency from spectrogram at each time point.
        Used to fill gaps where YIN pitch detection fails.
        
        Returns:
            t_spec: time array
            f_peaks: dominant frequency at each time point
            spectrum: the spectrogram data (for reuse)
            freqs: frequency bins
        """
        from scipy.signal import find_peaks
        
        # Compute spectrogram
        NFFT = 4096
        noverlap = NFFT - 256
        
        # Use matplotlib's specgram to get spectrum data
        spectrum, freqs, t_spec = plt.mlab.specgram(
            self.audio, 
            Fs=RATE, 
            NFFT=NFFT,
            noverlap=noverlap,
            mode='magnitude'
        )
        
        # Convert to dB
        spectrum_db = 20 * np.log10(spectrum + 1e-10)
        
        # Limit frequency range
        freq_mask = (freqs >= fmin) & (freqs <= fmax)
        freqs_limited = freqs[freq_mask]
        spectrum_limited = spectrum_db[freq_mask, :]
        
        # Extract peak frequency for each time slice
        f_peaks = np.zeros(len(t_spec))
        
        for i in range(len(t_spec)):
            col = spectrum_limited[:, i]
            max_val = np.max(col)
            
            # Only detect if signal is above threshold
            if max_val > threshold_db:
                # Find peaks in this time slice
                peaks, properties = find_peaks(col, height=threshold_db, prominence=3)
                
                if len(peaks) > 0:
                    # Get the strongest peak (likely fundamental or dominant frequency)
                    peak_heights = col[peaks]
                    best_peak_idx = peaks[np.argmax(peak_heights)]
                    f_peaks[i] = freqs_limited[best_peak_idx]
                else:
                    # Fallback to simple max
                    f_peaks[i] = freqs_limited[np.argmax(col)]
            else:
                f_peaks[i] = np.nan
        
        return t_spec, f_peaks, spectrum, freqs
    
    def _hybrid_pitch_detection(self, fmax_mode):
        """
        Hybrid pitch detection: combines YIN for accuracy with spectrogram peaks for coverage.
        
        Strategy:
        1. Run YIN pitch detection (accurate but may have gaps)
        2. Extract spectrogram peaks (comprehensive but may include harmonics)
        3. Use YIN where available, fill gaps with spectrogram peaks
        4. Apply smoothing to blend results
        """
        # Step 1: YIN pitch detection
        t_yin, f0_yin = [], []
        for i in range(0, len(self.audio) - FRAME, HOP):
            frame = self.audio[i:i+FRAME]
            f = yin_pitch(frame, sr=RATE, fmin=50, fmax=fmax_mode, threshold=0.15)
            t_yin.append(i / RATE)
            f0_yin.append(f if f > 0 else np.nan)
        
        t_yin = np.array(t_yin)
        f0_yin = np.array(f0_yin)
        
        # Step 2: Extract spectrogram peaks
        t_spec, f_spec, _, _ = self._extract_spectrogram_peaks(fmin=50, fmax=fmax_mode, threshold_db=-80)
        
        # Step 3: Interpolate spectrogram peaks to YIN time grid
        f_spec_interp = np.interp(t_yin, t_spec, f_spec)
        
        # Step 4: Hybrid merge - prefer YIN, fill gaps with spectrogram
        f0_hybrid = np.copy(f0_yin)
        
        # Fill NaN gaps with spectrogram data
        nan_mask = np.isnan(f0_hybrid)
        f0_hybrid[nan_mask] = f_spec_interp[nan_mask]
        
        # Step 5: Validate spectrogram-filled values
        # If spectrogram value is close to a harmonic of nearby YIN values, adjust
        for i in range(len(f0_hybrid)):
            if nan_mask[i] and not np.isnan(f0_hybrid[i]):
                # Look for nearby valid YIN values
                nearby_yin = []
                for j in range(max(0, i-10), min(len(f0_yin), i+10)):
                    if not np.isnan(f0_yin[j]):
                        nearby_yin.append(f0_yin[j])
                
                if nearby_yin:
                    ref_freq = np.median(nearby_yin)
                    current = f0_hybrid[i]
                    
                    # Check if current is a harmonic (2x, 3x, 4x) of reference
                    for harmonic in [2, 3, 4]:
                        if abs(current - ref_freq * harmonic) < ref_freq * 0.1:
                            # It's likely a harmonic, divide down to fundamental
                            f0_hybrid[i] = current / harmonic
                            break
                        # Also check if reference is harmonic of current
                        if abs(ref_freq - current * harmonic) < current * 0.1:
                            # Reference might be harmonic, keep current as fundamental
                            break
        
        # Step 6: Apply smoothing
        f0_cleaned = self._median_filter(f0_hybrid, window=5)
        f0_final = self._moving_average(f0_cleaned, window=3)
        
        return t_yin, f0_final
    
    def analyze(self):
        if self.audio is None:
            return
        self.fig.clear()
        self.ax = self.fig.add_subplot(111)
        
        # Apply dark mode colors if enabled
        if self.dark_mode:
            graph_bg = '#2D2D2D'
            fig_bg = '#171616'
            text_color = '#ADADAD'
            self.fig.set_facecolor(fig_bg)
            self.ax.set_facecolor(graph_bg)
            self.ax.spines['bottom'].set_color(text_color)
            self.ax.spines['top'].set_color(text_color)
            self.ax.spines['left'].set_color(text_color)
            self.ax.spines['right'].set_color(text_color)
            self.ax.xaxis.label.set_color(text_color)
            self.ax.yaxis.label.set_color(text_color)
            self.ax.tick_params(axis='x', colors=text_color)
            self.ax.tick_params(axis='y', colors=text_color)
            self.ax.title.set_color(text_color)

        # Determine frequency range based on mode
        fmax_mode = 2000 if self.mode_var.get() == "Low Range Mode" else MAX_FREQ
        
        # Use hybrid pitch detection for better coverage
        t, f0 = self._hybrid_pitch_detection(fmax_mode)
        
        # Compute harmonics
        h2 = 2 * f0
        h3 = 3 * f0

        self.max_t = t[-1] if len(t) > 0 else 1.0
        
        # Compute max frequency from valid (non-NaN) values
        valid_f0 = f0[~np.isnan(f0)]
        self.max_f = (np.max(valid_f0) + 200) if len(valid_f0) > 0 else 1000
        
        # Apply mode-based cap
        if self.mode_var.get() == "Low Range Mode":
            self.max_f = min(self.max_f, 5000)

        if self.view_var.get() == "linear":
            # Plot the hybrid pitch tracking result
            self.ax.plot(t, f0, label="Fundamental", linewidth=1.2)
            if self.show_harmonics:
                self.ax.plot(t, h2, "--", label="2nd Harmonic", linewidth=1, alpha=0.7)
                self.ax.plot(t, h3, "--", label="3rd Harmonic", linewidth=1, alpha=0.7)
            self.ax.set_ylabel("Hz")
            self.ax.set_ylim(0, self.max_f)
            self.ax.set_xlim(0, self.max_t)
            legend = self.ax.legend()
            # Apply dark mode to legend if enabled
            if self.dark_mode:
                legend.get_frame().set_facecolor('#2D2D2D')
                legend.get_frame().set_edgecolor('#ADADAD')
                for text in legend.get_texts():
                    text.set_color('#ADADAD')
        else:
            # High-resolution spectrogram with smooth rendering
            # cmap='viridis' for green-blue gradient, vmin=-120 for light filtering
            spectrum, freqs, t_spec, im = self.ax.specgram(
                self.audio, 
                Fs=RATE, 
                NFFT=4096,
                noverlap=4096-256,
                scale='dB',
                cmap='viridis',
                vmin=-120,
                mode='magnitude'
            )
            im.set_interpolation('bilinear')
            self.ax.set_ylim(0, self.max_f)
            self.ax.set_xlim(0, self.max_t)

        # Get complementary color for markers
        marker_color = self.get_complementary_color()
        
        # Draw connecting lines between markers (connect to nearest on x-axis)
        # Store connection pairs for hover detection
        self.marker_connections = []
        if len(self.markers) >= 2:
            # Sort markers by x-axis position for nearest-x connections
            sorted_indices = sorted(range(len(self.markers)), key=lambda i: self.markers[i][0])
            for i in range(len(sorted_indices) - 1):
                idx1 = sorted_indices[i]
                idx2 = sorted_indices[i + 1]
                self.marker_connections.append((idx1, idx2))
                x1, y1 = self.markers[idx1]
                x2, y2 = self.markers[idx2]
                if y1 is not None and y2 is not None:
                    self.ax.plot([x1, x2], [y1, y2], color=marker_color, 
                               linewidth=1, alpha=0.5, linestyle='--')
        
        # Draw markers (size: 78 = 130% of 60, finer lines: 1.2)
        for i, (x, y) in enumerate(self.markers):
            if y is not None:
                self.ax.scatter(x, y, marker='x', 
                              color=marker_color, s=78, linewidths=1.5)
                # Add marker number label
                self.ax.annotate(str(i + 1), (x, y), textcoords="offset points",
                               xytext=(5, 5), fontsize=8, color=marker_color)

        if self.view_var.get() == "linear":
            self.cursor_vline = self.ax.axvline(self.cursor_t, color="red", linestyle=":", linewidth=0.5, alpha=0.6)
            self.cursor_hline = self.ax.axhline(self.cursor_f, color="red", linestyle=":", linewidth=0.5, alpha=0.6)

        self.ax.set_xlabel("Time (s)")
        self.canvas.draw()
        self.btn.config(state="normal")

    # =========================
    # INTERACTION
    # =========================
    def on_press(self, e):
        if e.button == 3 and self.markers and e.inaxes:
            idx = min(range(len(self.markers)), key=lambda i: abs(self.markers[i][0] - e.xdata))
            self.markers.pop(idx)
            self.analyze()

    def on_motion(self, e):
        if not e.inaxes:
            return

        # Update cursor position based on mouse position
        if e.xdata is not None and e.ydata is not None:
            self.cursor_t = e.xdata
            self.cursor_f = e.ydata

        # Check if hovering over a marker
        if e.xdata is not None and e.ydata is not None:
            marker_idx = self.is_near_marker(e.xdata, e.ydata)
            
            if marker_idx is not None:
                # Display marker information
                mx, my = self.markers[marker_idx]
                note, cents = freq_to_note(my)
                lam = SPEED_OF_SOUND / my if my > 0 else 0
                txt = f"MARKER {marker_idx + 1}: Time={mx:.3f}s | Freq={my:.2f}Hz | λ={lam:.3f}m"
                if note:
                    txt += f" | {note} ({cents:+.1f}c)"
                self.info.config(text=txt)
                self.hover_marker_idx = marker_idx
                return
            
            # Check if hovering over a connection line
            connection = self.is_near_connection_line(e.xdata, e.ydata)
            if connection is not None:
                idx1, idx2 = connection
                x1, y1 = self.markers[idx1]
                x2, y2 = self.markers[idx2]
                
                # Calculate differences
                dt = abs(x2 - x1)
                df = abs(y2 - y1)
                
                # Determine which marker is first/second by time
                if x1 <= x2:
                    freq_change = y2 - y1
                else:
                    freq_change = y1 - y2
                
                freq_sign = "+" if freq_change >= 0 else ""
                txt = f"LINE: ΔTime={dt:.3f}s | ΔFreq={freq_sign}{freq_change:.2f}Hz (|{df:.2f}|Hz)"
                self.info.config(text=txt)
                return
        
        self.hover_marker_idx = None
        
        if e.ydata and e.ydata > 0:
            note, cents = freq_to_note(e.ydata)
            lam = SPEED_OF_SOUND / e.ydata
            txt = f"{e.ydata:.1f} Hz | λ={lam:.2f} m"
            if note:
                txt += f" | {note} ({cents:+.1f}c)"
            self.info.config(text=txt)
        
        # Update cursor lines in real-time
        if self.view_var.get() == "linear":
            self.update_cursor_display()

    def on_release(self, e):
        if e.button == 1 and e.inaxes and e.xdata is not None and e.ydata is not None:
            self.markers.append((e.xdata, e.ydata))
            self.analyze()

    def zoom(self, e):
        scale = 0.9 if e.button == 'up' else 1.1
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()

        x_center = (x0 + x1)/2
        y_center = (y0 + y1)/2
        
        # X-axis: limited to max_t (recording time)
        x_half = min((x1 - x0)/2 * scale, self.max_t/2)
        
        # Y-axis: unlimited when zooming out (no max cap), but limited to max_f when zooming in
        y_half_new = (y1 - y0)/2 * scale
        if e.button == 'up':
            # Zooming in - can't exceed current max_f
            y_half = min(y_half_new, self.max_f/2)
        else:
            # Zooming out - unlimited, allows seeing beyond initial max_f
            y_half = y_half_new

        x0_new = max(0, x_center - x_half)
        x1_new = min(self.max_t, x_center + x_half)
        y0_new = max(0, y_center - y_half)
        y1_new = y_center + y_half  # No upper limit when zooming out

        self.ax.set_xlim(x0_new, x1_new)
        self.ax.set_ylim(y0_new, y1_new)
        self.canvas.draw()

    def reset_zoom(self):
        if self.audio is not None:
            self.analyze()

    def pan_view(self, dx_frac, dy_frac):
        """Pan the view by a fraction of the current view range."""
        if self.audio is None:
            return
        
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        
        dx = (x1 - x0) * dx_frac
        dy = (y1 - y0) * dy_frac
        
        x0_new = max(0, min(x0 + dx, self.max_t - (x1 - x0)))
        x1_new = x0_new + (x1 - x0)
        y0_new = max(0, min(y0 + dy, self.max_f - (y1 - y0)))
        y1_new = y0_new + (y1 - y0)
        
        self.ax.set_xlim(x0_new, x1_new)
        self.ax.set_ylim(y0_new, y1_new)
        self.canvas.draw()

    def clear_markers(self):
        self.markers.clear()
        if self.audio is not None:
            self.analyze()

    def toggle_harmonics(self):
        self.show_harmonics = not self.show_harmonics
        self.harmonics_var.set(self.show_harmonics)
        if self.audio is not None:
            self.analyze()
    
    def update_cursor_display(self):
        """Update cursor crosshair position efficiently without redrawing entire plot."""
        if self.audio is None or self.view_var.get() != "linear":
            return
        
        # Update cursor line positions if they exist
        if self.cursor_vline is not None:
            self.cursor_vline.set_xdata([self.cursor_t, self.cursor_t])
        if self.cursor_hline is not None:
            self.cursor_hline.set_ydata([self.cursor_f, self.cursor_f])
        
        # Redraw only the cursor lines, not the entire plot
        self.canvas.draw_idle()
    
    # =========================
    # SMOOTHING UTILITIES
    # =========================
    def _median_filter(self, data, window=5):
        """
        Apply median filter to reject outliers while preserving edges.
        NaN values are ignored in the median computation.
        """
        from scipy.ndimage import median_filter
        
        # Create a copy to avoid modifying original
        filtered = np.copy(data)
        
        # Find valid (non-NaN) segments
        valid_mask = ~np.isnan(data)
        
        if np.sum(valid_mask) == 0:
            return filtered
        
        # Apply median filter only to valid regions
        # For simplicity, interpolate NaN, filter, then restore NaN
        temp = np.copy(data)
        
        # Simple median filter implementation that handles NaN
        half_window = window // 2
        for i in range(len(data)):
            start = max(0, i - half_window)
            end = min(len(data), i + half_window + 1)
            window_data = data[start:end]
            valid_window = window_data[~np.isnan(window_data)]
            if len(valid_window) > 0:
                filtered[i] = np.median(valid_window)
        
        return filtered
    
    def _moving_average(self, data, window=3):
        """
        Apply moving average for light temporal smoothing.
        Preserves natural pitch modulation.
        """
        smoothed = np.copy(data)
        half_window = window // 2
        
        for i in range(len(data)):
            start = max(0, i - half_window)
            end = min(len(data), i + half_window + 1)
            window_data = data[start:end]
            valid_window = window_data[~np.isnan(window_data)]
            if len(valid_window) > 0:
                smoothed[i] = np.mean(valid_window)
        
        return smoothed

# =========================
# RUN
# =========================
if __name__ == "__main__":
    root = tk.Tk()
    try:
        root.iconbitmap(resource_path("FV_icon.ico"))
    except Exception:
        pass  # Icon not found, continue without it
    app = App(root)
    root.mainloop()
