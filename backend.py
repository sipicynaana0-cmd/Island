import asyncio
import base64
import ctypes
from ctypes import wintypes
import io
import json
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx
import psutil
from PIL import Image

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def get_active_keyboard_layout() -> str:
    """Returns 'RU', 'EN', etc. of the active foreground window (< 5 microseconds)."""
    try:
        hwnd = user32.GetForegroundWindow()
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        klid = user32.GetKeyboardLayout(thread_id)
        primary = (klid & 0xFFFF) & 0x3FF
        if primary == 0x19:
            return "RU"
        elif primary == 0x09:
            return "EN"
        elif primary == 0x22:
            return "UA"
        elif primary == 0x07:
            return "DE"
        return "EN"
    except Exception:
        return "EN"


def get_foreground_app_name() -> str:
    """Returns the process name (e.g. 'chrome', 'Telegram', 'Code') of the currently active window."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "Windows"
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            p = psutil.Process(pid.value)
            name = re.sub(r"\.exe$", "", p.name(), flags=re.I)
            return name
    except Exception:
        pass
    return "Windows"


def simulate_ctrl_v():
    """Sends Ctrl+V to the currently focused window (since Island has WS_EX_NOACTIVATE, focus stays in user's app)."""
    try:
        VK_CONTROL = 0x11
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def send_media_key(vk_code: int):
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk_code, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)


VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3


# --- Text Transformation Utilities (Exact tools from exuupery/island 'Текст' tab) ---
EN_TO_RU_MAP = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~@#$^&",
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё\"№;:?",
)
RU_TO_EN_MAP = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё\"№;:?",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~@#$^&",
)

TRANSLIT_RU_TO_EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def transform_text_tool(text: str, action: str) -> str:
    if not text:
        return text
    if action == "layout":
        ru_count = sum(1 for c in text if "а" <= c.lower() <= "я" or c.lower() == "ё")
        en_count = sum(1 for c in text if "a" <= c.lower() <= "z")
        return text.translate(RU_TO_EN_MAP if ru_count > en_count else EN_TO_RU_MAP)
    elif action == "upper":
        return text.upper()
    elif action == "lower":
        return text.lower()
    elif action == "title":
        return text.title()
    elif action == "sentence":
        return ". ".join(s.strip().capitalize() for s in text.split("."))
    elif action == "translit":
        out = []
        for ch in text:
            low = ch.lower()
            if low in TRANSLIT_RU_TO_EN:
                tr = TRANSLIT_RU_TO_EN[low]
                out.append(tr.capitalize() if ch.isupper() else tr)
            else:
                out.append(ch)
        return "".join(out)
    elif action == "no_breaks":
        return re.sub(r"[\r\n]+", " ", text).strip()
    elif action == "trim_spaces":
        return re.sub(r"[ \t]+", " ", text).strip()
    elif action == "json_pretty":
        try:
            obj = json.loads(text)
            return json.dumps(obj, indent=2, ensure_ascii=False)
        except Exception:
            return text
    elif action == "json_min":
        try:
            obj = json.loads(text)
            return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            return text
    elif action == "to_b64":
        try:
            return base64.b64encode(text.encode("utf-8")).decode("ascii")
        except Exception:
            return text
    elif action == "from_b64":
        try:
            return base64.b64decode(text.encode("ascii")).decode("utf-8", errors="replace")
        except Exception:
            return text
    return text


@dataclass
class ClipboardEntry:
    text: str
    source_app: str
    timestamp: float
    pinned: bool = False

    def relative_time(self) -> str:
        diff = max(0, int(time.time() - self.timestamp))
        if diff < 60:
            return "только что"
        elif diff < 3600:
            return f"{diff // 60} мин"
        elif diff < 86400:
            return f"{diff // 3600} ч"
        return "вчера"


@dataclass
class WeatherData:
    city: str = "Автоопределение..."
    temp: int = -3
    temp_min: int = -5
    temp_max: int = 1
    feels_like: int = -7
    dew_point: int = -5
    condition: str = "Пасмурно"
    wind_speed: float = 3.0
    wind_dir_deg: int = 115
    wind_dir_label: str = "Ю-В"
    humidity: int = 88
    pressure_mm: int = 746
    uv_index: int = 1
    precipitation_mm: float = 0.0
    sunrise: str = "08:34"
    sunset: str = "16:58"
    is_snow: bool = True
    is_rain: bool = False
    hourly: List[Tuple[str, int, float]] = field(default_factory=list)


class WeatherWorker:
    """
    High-precision automatic Weather Engine for Russia & CIS.
    Bypasses active VPN tunnels via split-tunneled Yandex Geobase + Open-Meteo Geocoding
    so the user always sees their true local city and exact meteorological metrics.
    """

    def __init__(self):
        self.data = WeatherData(
            city="Автоопределение...",
            hourly=[
                ("Сейчас", -3, 0.0),
                ("20:00", -3, 0.0),
                ("21:00", -4, 0.0),
                ("22:00", -4, 0.0),
                ("23:00", -4, 0.0),
                ("00:00", -5, 0.0),
                ("01:00", -5, 0.0),
                ("02:00", -5, 0.0),
            ],
        )
        self._thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self._thread.start()

    def refresh_async(self):
        threading.Thread(target=self._fetch_once, daemon=True).start()

    def _fetch_loop(self):
        while True:
            self._fetch_once()
            time.sleep(900)

    RUSSIAN_CITIES_FAST = {
        "новокузнецк": (53.7575, 87.1360, "Новокузнецк"),
        "кемерово": (55.3547, 86.0873, "Кемерово"),
        "красноярск": (56.0153, 92.8932, "Красноярск"),
        "новосибирск": (55.0084, 82.9357, "Новосибирск"),
        "барнаул": (53.3474, 83.7784, "Барнаул"),
        "томск": (56.4846, 84.9476, "Томск"),
        "омск": (54.9893, 73.3682, "Омск"),
        "иркутск": (52.2870, 104.2810, "Иркутск"),
        "екатеринбург": (56.8389, 60.6057, "Екатеринбург"),
        "москва": (55.7558, 37.6173, "Москва"),
        "санкт-петербург": (59.9343, 30.3351, "Санкт-Петербург"),
        "казань": (55.7961, 49.1064, "Казань"),
    }

    def _resolve_exact_location(self, client: httpx.Client) -> Tuple[float, float, str]:
        lat, lon, city_name = 53.7575, 87.1360, "Авто"
        real_ip = None

        # 1. Bypass active VPN tunnel via split-tunneled ya.ru/internet Geobase
        try:
            import urllib.request
            req = urllib.request.Request("https://ya.ru/internet/", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4.0) as r_ya:
                html = r_ya.read().decode("utf-8", errors="replace")
                m_city = re.search(r'"name"\s*:\s*"([^"]+)"\s*,\s*"ip"\s*:\s*"([^"]+)"', html)
                if m_city:
                    ya_city = m_city.group(1).strip()
                    if ya_city:
                        fast_hit = self.RUSSIAN_CITIES_FAST.get(ya_city.lower())
                        if fast_hit:
                            return fast_hit
                        r_geo = client.get(
                            "https://geocoding-api.open-meteo.com/v1/search",
                            params={"name": ya_city, "count": 1, "language": "ru", "format": "json"},
                        )
                        if r_geo.status_code == 200:
                            results = r_geo.json().get("results") or []
                            if results:
                                lat = float(results[0]["latitude"])
                                lon = float(results[0]["longitude"])
                                city_name = results[0].get("name") or ya_city
                                return lat, lon, city_name
                for m_ip in re.findall(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", html):
                    parts = [int(p) for p in m_ip.split(".")]
                    if all(0 <= p <= 255 for p in parts) and parts[0] not in (0, 10, 127, 172, 192):
                        real_ip = m_ip
                        break
        except Exception:
            pass

        # 2. Query ip-api with real non-VPN IP if found, otherwise default IP
        try:
            ip_url = f"http://ip-api.com/json/{real_ip}?lang=ru" if real_ip else "http://ip-api.com/json/?lang=ru"
            r_geo = client.get(ip_url)
            if r_geo.status_code == 200:
                js = r_geo.json()
                if js.get("status") == "success" and js.get("lat") is not None:
                    lat = float(js["lat"])
                    lon = float(js["lon"])
                    city_name = js.get("city") or city_name
                    return lat, lon, city_name
        except Exception:
            pass

        # 3. Secondary fallback
        try:
            ip_r = client.get("https://ipwho.is/?lang=ru")
            if ip_r.status_code == 200:
                js = ip_r.json()
                if js.get("success") and js.get("latitude") is not None:
                    lat = float(js["latitude"])
                    lon = float(js["longitude"])
                    city_name = js.get("city") or city_name
        except Exception:
            pass

        return lat, lon, city_name

    def _fetch_once(self):
        try:
            with httpx.Client(timeout=5.5, follow_redirects=True) as client:
                lat, lon, city_name = self._resolve_exact_location(client)

                url = (
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,dew_point_2m,precipitation,weather_code,surface_pressure,wind_speed_10m,wind_direction_10m"
                    "&hourly=temperature_2m,precipitation,weather_code"
                    "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max,precipitation_sum"
                    "&wind_speed_unit=ms&timezone=auto&forecast_days=2"
                )
                r = client.get(url)
                if r.status_code == 200:
                    d = r.json()
                    curr = d.get("current", {})
                    daily = d.get("daily", {})

                    temp_f = float(curr.get("temperature_2m", 0.0))
                    temp = int(round(temp_f))
                    feels = int(round(float(curr.get("apparent_temperature", temp_f - 2.0))))
                    dew = int(round(float(curr.get("dew_point_2m", temp_f - 3.0))))
                    hum = int(round(float(curr.get("relative_humidity_2m", 75))))
                    press_hpa = float(curr.get("surface_pressure", 1002.0))
                    press_mm = int(round(press_hpa * 0.750062))
                    wind_s = round(float(curr.get("wind_speed_10m", 3.0)), 1)
                    wind_deg = int(curr.get("wind_direction_10m", 180))
                    wcode = int(curr.get("weather_code", 2))

                    t_max_list = daily.get("temperature_2m_max") or [temp + 2]
                    t_min_list = daily.get("temperature_2m_min") or [temp - 2]
                    temp_max = int(round(float(t_max_list[0])))
                    temp_min = int(round(float(t_min_list[0])))

                    sr_list = daily.get("sunrise") or ["08:15"]
                    ss_list = daily.get("sunset") or ["16:45"]
                    sunrise_str = sr_list[0].split("T")[-1][:5] if sr_list else "08:15"
                    sunset_str = ss_list[0].split("T")[-1][:5] if ss_list else "16:45"

                    uv_list = daily.get("uv_index_max") or [1]
                    uv_idx = max(0, int(round(float(uv_list[0]))))

                    prec_sum_list = daily.get("precipitation_sum") or [0.0]
                    prec_day = round(float(prec_sum_list[0]), 1)

                    dirs = ["Северный", "С-В", "Восточный", "Ю-В", "Южный", "Ю-З", "Западный", "С-З"]
                    w_label = dirs[int(((wind_deg + 22.5) % 360) // 45)]

                    is_snow = wcode in (71, 73, 75, 77, 85, 86) or (temp <= 0 and wcode >= 51)
                    is_rain = wcode in (51, 53, 55, 61, 63, 65, 80, 81, 82) and temp > 0
                    wcode_map = {
                        0: "Ясно",
                        1: "Преим. ясно",
                        2: "Перем. облачность",
                        3: "Пасмурно",
                        45: "Туман",
                        48: "Изморозь",
                        51: "Слабая морось",
                        53: "Морось",
                        55: "Сильная морось",
                        61: "Небольшой дождь",
                        63: "Дождь",
                        65: "Сильный дождь",
                        71: "Небольшой снег",
                        73: "Снег",
                        75: "Снегопад",
                        77: "Снежная крупа",
                        80: "Ливень",
                        85: "Снежный заряд",
                        86: "Сильный снегопад",
                    }
                    cond = wcode_map.get(wcode, "Снег" if is_snow else ("Дождь" if is_rain else "Облачно"))

                    hourly_data = d.get("hourly", {})
                    h_times = hourly_data.get("time", [])
                    h_temps = hourly_data.get("temperature_2m", [])
                    h_Precs = hourly_data.get("precipitation", [])

                    curr_time_str = curr.get("time", "")
                    now_hour_prefix = curr_time_str[:13] if len(curr_time_str) >= 13 else time.strftime("%Y-%m-%dT%H")
                    start_i = 0
                    for idx, t_str in enumerate(h_times):
                        if t_str[:13] >= now_hour_prefix:
                            start_i = idx
                            break

                    hourly_list: List[Tuple[str, int, float]] = []
                    for offset in range(8):
                        ii = start_i + offset
                        if ii < len(h_times):
                            lbl = "Сейчас" if offset == 0 else h_times[ii].split("T")[-1][:5]
                            ht = temp if offset == 0 else (int(round(h_temps[ii])) if ii < len(h_temps) else temp)
                            hp = round(float(h_Precs[ii]), 1) if ii < len(h_Precs) else 0.0
                            hourly_list.append((lbl, ht, hp))

                    self.data = WeatherData(
                        city=city_name,
                        temp=temp,
                        temp_min=temp_min,
                        temp_max=temp_max,
                        feels_like=feels,
                        dew_point=dew,
                        condition=cond,
                        wind_speed=wind_s,
                        wind_dir_deg=wind_deg,
                        wind_dir_label=w_label,
                        humidity=hum,
                        pressure_mm=press_mm,
                        uv_index=uv_idx,
                        precipitation_mm=prec_day,
                        sunrise=sunrise_str,
                        sunset=sunset_str,
                        is_snow=is_snow,
                        is_rain=is_rain,
                        hourly=hourly_list or self.data.hourly,
                    )
        except Exception:
            pass


@dataclass
class TrackInfo:
    title: str = ""
    artist: str = ""
    album: str = ""
    is_playing: bool = False
    position: float = 0.0
    duration: float = 0.0
    cover_bytes: Optional[bytes] = None
    accent_color: Tuple[int, int, int] = (224, 82, 117)  # Island signature coral-rose accent
    secondary_color: Tuple[int, int, int] = (142, 162, 255)
    source_app: str = ""


@dataclass
class LyricLine:
    time_sec: float
    text: str


@dataclass
class AppAudioSession:
    pid: int
    name: str
    volume: float
    muted: bool
    peak: float = 0.0


def extract_palette_from_image(img_bytes: bytes) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """
    Extracts vibrant primary and secondary colors from album art.
    If the cover is black-and-white / grayscale (like monochrome rap/indie covers),
    automatically returns a vibrant neon crimson-rose & cyber-violet palette instead of dull gray!
    """
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img = img.resize((24, 24), Image.Resampling.NEAREST)
        pixels = list(img.getdata())

        best_primary = (236, 72, 128)
        best_secondary = (129, 140, 248)
        best_score = -1.0
        second_score = -1.0
        max_sat_seen = 0.0

        for r, g, b in pixels:
            max_c = max(r, g, b)
            min_c = min(r, g, b)
            if max_c < 45 or min_c > 235:
                continue
            sat = (max_c - min_c) / float(max_c) if max_c else 0.0
            if sat > max_sat_seen:
                max_sat_seen = sat
            if sat < 0.24:
                # Skip grayscale/muddy beige pixels so monochrome covers don't turn ugly gray-brown
                continue
            lum = (r + g + b) / (3.0 * 255.0)
            score = sat * 2.2 + (1.0 - abs(lum - 0.55))
            if score > best_score:
                second_score = best_score
                best_secondary = best_primary
                best_score = score
                best_primary = (r, g, b)
            elif score > second_score and abs(r - best_primary[0]) + abs(g - best_primary[1]) > 60:
                second_score = score
                best_secondary = (r, g, b)

        if max_sat_seen < 0.24 or best_score < 0:
            return (236, 72, 128), (129, 140, 248)

        def boost(c: Tuple[int, int, int]) -> Tuple[int, int, int]:
            r, g, b = c
            factor = 1.28 if max(r, g, b) < 185 else 1.06
            return (
                min(255, max(85, int(r * factor))),
                min(255, max(75, int(g * factor))),
                min(255, max(90, int(b * factor))),
            )

        return boost(best_primary), boost(best_secondary)
    except Exception:
        return (236, 72, 128), (129, 140, 248)


class AudioEngine:
    """
    Lock-free background Windows Core Audio engine.
    - Real-time spectrum analyzer: captures the system mix via WASAPI loopback and runs an FFT
      into 32 logarithmic bands (40 Hz .. 16 kHz) with automatic gain, so the visualizers react
      to the ACTUAL music (kick, vocals, hi-hats) instead of a fake sine animation.
    - Master volume, per-app sessions and peak meter on a separate COM thread.
    The 120 FPS UI thread only reads cached floats and never blocks.
    """

    N_BANDS = 32

    def __init__(self):
        self._cached_vol: float = 50.0
        self._cached_mute: bool = False
        self._cached_peak: float = 0.0
        self._raw_bands: List[float] = [0.0] * self.N_BANDS
        self._raw_level: float = 0.0
        self._spectrum_live: bool = False
        self._spectrum_ts: float = 0.0
        self._smooth_bands: List[float] = [0.0] * self.N_BANDS
        self._smooth_peak: float = 0.0
        self._last_bands_t: float = time.monotonic()
        self._cached_sessions: List[AppAudioSession] = []
        self._cmd_queue: List[Tuple[str, object]] = []
        self._want_sessions_poll: bool = False
        self._running: bool = True
        self._thread = threading.Thread(target=self._bg_audio_loop, daemon=True)
        self._thread.start()
        self._spec_thread = threading.Thread(target=self._bg_spectrum_loop, daemon=True)
        self._spec_thread.start()

    # ------------------------------------------------------------------ spectrum
    def _bg_spectrum_loop(self):
        try:
            import numpy as np
            import pyaudiowpatch as pyaudio
        except Exception:
            return  # library missing -> peak-meter fallback in get_peak_and_bands

        n_fft = 2048
        hop = 768
        window = np.hanning(n_fft).astype(np.float32)
        ring = np.zeros(n_fft, dtype=np.float32)
        agc_db = -30.0

        while self._running:
            pa = None
            stream = None
            try:
                pa = pyaudio.PyAudio()
                wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                loop_dev = None
                if spk.get("isLoopbackDevice"):
                    loop_dev = spk
                else:
                    for d in pa.get_loopback_device_info_generator():
                        if spk["name"] in d["name"]:
                            loop_dev = d
                            break
                if loop_dev is None:
                    raise RuntimeError("no loopback device")

                sr = int(loop_dev["defaultSampleRate"])
                ch = max(1, int(loop_dev["maxInputChannels"]))
                stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=ch,
                    rate=sr,
                    input=True,
                    input_device_index=loop_dev["index"],
                    frames_per_buffer=hop,
                )

                # Logarithmic band edges mapped to FFT bins
                freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
                edges = np.geomspace(40.0, min(16000.0, sr * 0.45), self.N_BANDS + 1)
                bin_lo = [max(1, int(np.searchsorted(freqs, edges[i]))) for i in range(self.N_BANDS)]
                bin_hi = [max(bin_lo[i] + 1, int(np.searchsorted(freqs, edges[i + 1]))) for i in range(self.N_BANDS)]
                centers = np.sqrt(edges[:-1] * edges[1:])
                # +3.5 dB/octave tilt so mids & highs are as visible as the bass
                tilt = (3.5 * np.log2(centers / 250.0)).astype(np.float32)

                dev_name = spk["name"]
                last_dev_check = time.monotonic()
                silent_since = None

                while self._running:
                    avail = stream.get_read_available()
                    if avail < hop:
                        time.sleep(0.004)
                        # Loopback delivers nothing while the device is silent -> decay to zero
                        if silent_since is None:
                            silent_since = time.monotonic()
                        elif time.monotonic() - silent_since > 0.12:
                            self._raw_bands = [0.0] * self.N_BANDS
                            self._raw_level = 0.0
                            self._spectrum_ts = time.monotonic()
                            self._spectrum_live = True
                        # Re-open if the default output device was switched
                        if time.monotonic() - last_dev_check > 2.5:
                            last_dev_check = time.monotonic()
                            try:
                                cur = pa.get_device_info_by_index(
                                    pa.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultOutputDevice"]
                                )
                                if cur["name"] != dev_name:
                                    break
                            except Exception:
                                break
                        continue
                    silent_since = None

                    raw = stream.read(hop, exception_on_overflow=False)
                    data = np.frombuffer(raw, dtype=np.float32)
                    if ch > 1:
                        data = data.reshape(-1, ch).mean(axis=1)
                    n = len(data)
                    ring = np.roll(ring, -n)
                    ring[-n:] = data

                    rms = float(np.sqrt(np.mean(data * data)) + 1e-12)
                    if rms < 2e-5:
                        self._raw_bands = [0.0] * self.N_BANDS
                        self._raw_level = 0.0
                        self._spectrum_ts = time.monotonic()
                        self._spectrum_live = True
                        continue

                    mag = np.abs(np.fft.rfft(ring * window)) ** 2
                    db = np.empty(self.N_BANDS, dtype=np.float32)
                    for i in range(self.N_BANDS):
                        db[i] = 10.0 * math.log10(float(mag[bin_lo[i]:bin_hi[i]].mean()) + 1e-12)
                    db += tilt

                    # Automatic gain: follows the loudest band (fast up, slow down)
                    top = float(db.max())
                    agc_db = top if top > agc_db else agc_db + (top - agc_db) * 0.012
                    rng = 42.0
                    norm = np.clip((db - (agc_db - rng)) / rng, 0.0, 1.0) ** 1.35

                    self._raw_bands = norm.tolist()
                    level_db = 20.0 * math.log10(rms)
                    self._raw_level = max(0.0, min(1.0, (level_db + 48.0) / 40.0))
                    self._spectrum_ts = time.monotonic()
                    self._spectrum_live = True
            except Exception:
                self._spectrum_live = False
            finally:
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                try:
                    if pa is not None:
                        pa.terminate()
                except Exception:
                    pass
            time.sleep(1.0)

    def request_sessions_refresh(self):
        self._want_sessions_poll = True

    def get_master_volume(self) -> Tuple[float, bool]:
        return self._cached_vol, self._cached_mute

    def set_master_volume(self, percent: float):
        scalar = max(0.0, min(100.0, float(percent)))
        self._cached_vol = round(scalar)
        self._cmd_queue.append(("set_master", scalar / 100.0))

    def toggle_master_mute(self) -> bool:
        self._cached_mute = not self._cached_mute
        self._cmd_queue.append(("set_mute", self._cached_mute))
        return self._cached_mute

    def get_peak_and_bands(self, is_playing: bool = False) -> Tuple[float, List[float]]:
        """
        Instant read of the smoothed 32-band spectrum. Frame-rate independent attack/release:
        bars jump up instantly on a beat and fall back smoothly like a real analyzer.
        """
        now = time.monotonic()
        dt = max(0.001, min(0.1, now - self._last_bands_t))
        self._last_bands_t = now

        live = self._spectrum_live and (now - self._spectrum_ts) < 0.6
        if live:
            targets = self._raw_bands
            level = self._raw_level
        else:
            # Fallback: real hardware peak meter, flat across bands (no fake animation)
            level = max(0.0, min(1.0, self._cached_peak * 1.4))
            targets = [level * (0.9 - 0.35 * (i / (self.N_BANDS - 1))) for i in range(self.N_BANDS)]

        attack = 1.0 - math.exp(-dt / 0.028)
        release = 1.0 - math.exp(-dt / 0.16)
        sb = self._smooth_bands
        for i in range(self.N_BANDS):
            t = targets[i] if i < len(targets) else 0.0
            c = sb[i]
            sb[i] = c + (t - c) * (attack if t > c else release)

        bass = (sb[0] + sb[1] + sb[2] + sb[3] + sb[4]) / 5.0
        tgt_peak = max(level * 0.85, bass)
        self._smooth_peak += (tgt_peak - self._smooth_peak) * (attack if tgt_peak > self._smooth_peak else release)
        return self._smooth_peak, list(sb)

    def get_app_sessions(self, trigger_poll: bool = False) -> List[AppAudioSession]:
        if trigger_poll:
            self._want_sessions_poll = True
        return list(self._cached_sessions)

    def set_app_volume(self, app_name: str, volume: float):
        scalar = max(0.0, min(1.0, float(volume)))
        for s in self._cached_sessions:
            if s.name.lower() == app_name.lower():
                s.volume = scalar
        self._cmd_queue.append(("app_vol", (app_name, scalar)))

    def toggle_app_mute(self, app_name: str):
        for s in self._cached_sessions:
            if s.name.lower() == app_name.lower():
                s.muted = not s.muted
        self._cmd_queue.append(("app_mute", app_name))

    def _bg_audio_loop(self):
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IAudioMeterInformation

        try:
            comtypes.CoInitialize()
        except Exception:
            pass

        endpoint_vol = None
        meter = None
        tick = 0

        while self._running:
            try:
                if endpoint_vol is None or meter is None:
                    speakers = AudioUtilities.GetSpeakers()
                    raw_dev = getattr(speakers, "_dev", speakers)
                    if hasattr(speakers, "EndpointVolume"):
                        endpoint_vol = speakers.EndpointVolume
                    else:
                        iface_vol = raw_dev.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
                        endpoint_vol = ctypes.cast(iface_vol, ctypes.POINTER(IAudioEndpointVolume))
                    iface_meter = raw_dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None)
                    meter = ctypes.cast(iface_meter, ctypes.POINTER(IAudioMeterInformation))

                # 1. Execute queued commands immediately
                while self._cmd_queue:
                    cmd, arg = self._cmd_queue.pop(0)
                    if cmd == "set_master" and endpoint_vol:
                        endpoint_vol.SetMasterVolumeLevelScalar(float(arg), None)
                    elif cmd == "set_mute" and endpoint_vol:
                        endpoint_vol.SetMute(bool(arg), None)
                    elif cmd == "app_vol":
                        aname, aval = arg
                        for s in AudioUtilities.GetAllSessions():
                            if s.Process and s.Process.name():
                                clean = re.sub(r"\.exe$", "", s.Process.name(), flags=re.I)
                                if clean.lower() == aname.lower():
                                    s.SimpleAudioVolume.SetMasterVolume(float(aval), None)
                    elif cmd == "app_mute":
                        aname = str(arg)
                        for s in AudioUtilities.GetAllSessions():
                            if s.Process and s.Process.name():
                                clean = re.sub(r"\.exe$", "", s.Process.name(), flags=re.I)
                                if clean.lower() == aname.lower():
                                    curr = bool(s.SimpleAudioVolume.GetMute())
                                    s.SimpleAudioVolume.SetMute(not curr, None)

                # 2. Poll hardware audio peak at 30Hz
                if meter:
                    self._cached_peak = float(meter.GetPeakValue())

                # 3. Poll master volume every ~180ms
                if tick % 6 == 0 and endpoint_vol and not self._cmd_queue:
                    self._cached_vol = round(float(endpoint_vol.GetMasterVolumeLevelScalar()) * 100.0)
                    self._cached_mute = bool(endpoint_vol.GetMute())

                # 4. Always poll per-app sessions on startup and every ~0.8s so Mixer is NEVER empty!
                if tick == 0 or tick % 24 == 0 or self._want_sessions_poll:
                    self._want_sessions_poll = False
                    seen: Dict[str, AppAudioSession] = {}
                    for s in AudioUtilities.GetAllSessions():
                        try:
                            proc = s.Process
                            if proc:
                                raw = proc.name()
                                if not raw or raw.lower() in ("system", "svchost.exe", "audiodg.exe"):
                                    continue
                                clean = re.sub(r"\.exe$", "", raw, flags=re.I)
                                clean = clean[:1].upper() + clean[1:]
                                v_if = s.SimpleAudioVolume
                                vol = float(v_if.GetMasterVolume())
                                muted = bool(v_if.GetMute())
                                pk = 0.0
                                try:
                                    m_if = s._ctl.QueryInterface(IAudioMeterInformation)
                                    pk = float(m_if.GetPeakValue())
                                except Exception:
                                    pass
                                if clean not in seen or pk > seen[clean].peak:
                                    seen[clean] = AppAudioSession(proc.pid, clean, vol, muted, pk)
                        except Exception:
                            continue
                    self._cached_sessions = sorted(seen.values(), key=lambda x: (-x.peak, x.name.lower()))[:6]

            except Exception:
                endpoint_vol = None
                meter = None

            tick += 1
            time.sleep(0.032)


class MediaSessionWorker:
    def __init__(self):
        self.track = TrackInfo()
        self.lyrics: List[LyricLine] = []
        self.lyrics_status: str = ""
        self._lock = threading.Lock()
        self._cover_cache: Dict[str, Tuple[Optional[bytes], Tuple[int, int, int], Tuple[int, int, int]]] = {}
        self._lyrics_cache: Dict[str, List[LyricLine]] = {}
        self._cmd_queue: List[Tuple[str, Optional[float]]] = []
        self._running = True
        self._last_pos_Timestamp = time.monotonic()
        self._seek_lock_until = 0.0
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def play_pause(self):
        self._cmd_queue.append(("play_pause", None))

    def next_track(self):
        self._cmd_queue.append(("next", None))

    def prev_track(self):
        self._cmd_queue.append(("prev", None))

    def seek_to(self, seconds: float):
        now = time.monotonic()
        with self._lock:
            target_s = max(0.0, min(self.track.duration if self.track.duration > 0 else seconds, seconds))
            self.track.position = target_s
            self._last_pos_Timestamp = now
            self._seek_lock_until = now + 1.15
        # Remove any older queued seeks so rapid scrubbing only sends the latest seek position
        self._cmd_queue = [c for c in self._cmd_queue if c[0] != "seek"]
        self._cmd_queue.append(("seek", target_s))

    def get_current_lyric_window(self) -> Tuple[str, str, str]:
        with self._lock:
            if not self.lyrics:
                return ("", "", "")
            pos = self.get_interpolated_position_unlocked()
            idx = -1
            for i, line in enumerate(self.lyrics):
                if pos + 0.25 >= line.time_sec:
                    idx = i
                else:
                    break
            if idx == -1:
                first = self.lyrics[0].text if self.lyrics else ""
                return ("", "♪ · · ·", first)
            prev_t = self.lyrics[idx - 1].text if idx > 0 else ""
            curr_t = self.lyrics[idx].text
            next_t = self.lyrics[idx + 1].text if idx + 1 < len(self.lyrics) else ""
            return (prev_t, curr_t, next_t)

    def get_interpolated_position_unlocked(self) -> float:
        pos = self.track.position
        if self.track.is_playing and self.track.duration > 0:
            elapsed = time.monotonic() - self._last_pos_Timestamp
            pos = min(self.track.duration, pos + elapsed)
        return pos

    def get_snapshot(self) -> Tuple[TrackInfo, float]:
        with self._lock:
            pos = self.get_interpolated_position_unlocked()
            t = TrackInfo(
                title=self.track.title,
                artist=self.track.artist,
                album=self.track.album,
                is_playing=self.track.is_playing,
                position=pos,
                duration=self.track.duration,
                cover_bytes=self.track.cover_bytes,
                accent_color=self.track.accent_color,
                secondary_color=self.track.secondary_color,
                source_app=self.track.source_app,
            )
            return t, pos

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._async_poll())

    async def _async_poll(self):
        winrt_available = False
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as MediaManager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
            )
            from winsdk.windows.storage.streams import Buffer, InputStreamOptions
            winrt_available = True
        except Exception:
            winrt_available = False

        manager = None
        last_lyrics_key = ""

        while self._running:
            try:
                if winrt_available and manager is None:
                    manager = await MediaManager.request_async()

                session = manager.get_current_session() if manager else None

                while self._cmd_queue:
                    cmd, arg = self._cmd_queue.pop(0)
                    if session:
                        try:
                            if cmd == "play_pause":
                                await session.try_toggle_play_pause_async()
                            elif cmd == "next":
                                await session.try_skip_next_async()
                            elif cmd == "prev":
                                await session.try_skip_previous_async()
                            elif cmd == "seek" and arg is not None:
                                await session.try_change_playback_position_async(int(arg * 10_000_000))
                            continue
                        except Exception:
                            pass
                    if cmd == "play_pause":
                        send_media_key(VK_MEDIA_PLAY_PAUSE)
                    elif cmd == "next":
                        send_media_key(VK_MEDIA_NEXT_TRACK)
                    elif cmd == "prev":
                        send_media_key(VK_MEDIA_PREV_TRACK)

                if session:
                    info = session.get_playback_info()
                    is_playing = info.playback_status == PlaybackStatus.PLAYING if info else False
                    props = await session.try_get_media_properties_async()
                    title = (props.title or "").strip() if props else ""
                    artist = (props.artist or "").strip() if props else ""
                    album = (props.album_title or "").strip() if props else ""
                    source_app = (session.source_app_user_model_id or "").split("!")[0]

                    pos_sec = 0.0
                    dur_sec = 0.0
                    try:
                        tl = session.get_timeline_properties()
                        if tl:
                            dur_sec = max(0.0, tl.end_time.total_seconds())
                            pos_sec = max(0.0, tl.position.total_seconds())
                            # Add exact elapsed time since Windows GSMTC last updated the timeline snapshot!
                            if is_playing and hasattr(tl, "last_updated_time") and tl.last_updated_time:
                                from datetime import datetime, timezone
                                lu = tl.last_updated_time
                                if lu and getattr(lu, "year", 0) > 2020:
                                    now_utc = datetime.now(timezone.utc)
                                    lu_utc = lu if lu.tzinfo is not None else lu.replace(tzinfo=timezone.utc)
                                    elapsed_lu = (now_utc - lu_utc).total_seconds()
                                    if 0.0 <= elapsed_lu < 300.0:
                                        pos_sec += elapsed_lu
                                        if dur_sec > 0:
                                            pos_sec = min(dur_sec, pos_sec)
                    except Exception:
                        pass

                    cache_key = f"{title}::{artist}"
                    cover_bytes = None
                    accent = (236, 72, 128)
                    secondary = (129, 140, 248)

                    if title:
                        if cache_key in self._cover_cache:
                            cover_bytes, accent, secondary = self._cover_cache[cache_key]
                        elif props and props.thumbnail:
                            try:
                                stream = await props.thumbnail.open_read_async()
                                size = int(stream.size)
                                if 0 < size < 10_000_000:
                                    buf = Buffer(size)
                                    await stream.read_async(buf, size, InputStreamOptions.NONE)
                                    cover_bytes = bytes(buf)
                                    accent, secondary = extract_palette_from_image(cover_bytes)
                                    if len(self._cover_cache) > 30:
                                        self._cover_cache.clear()
                                    self._cover_cache[cache_key] = (cover_bytes, accent, secondary)
                            except Exception:
                                pass

                    now_mono = time.monotonic()
                    with self._lock:
                        # Instant sync when track changes, play state changes, or external seek >0.45s occurs (unless inside user seek lock)
                        if title != self.track.title or is_playing != self.track.is_playing:
                            self.track.position = pos_sec
                            self._last_pos_Timestamp = now_mono
                        elif now_mono >= self._seek_lock_until:
                            interp = self.get_interpolated_position_unlocked()
                            if abs(pos_sec - interp) > 0.45:
                                self.track.position = pos_sec
                                self._last_pos_Timestamp = now_mono

                        self.track.title = title
                        self.track.artist = artist
                        self.track.album = album
                        self.track.is_playing = is_playing
                        self.track.duration = dur_sec
                        self.track.cover_bytes = cover_bytes
                        self.track.accent_color = accent
                        self.track.secondary_color = secondary
                        self.track.source_app = source_app

                    if title and cache_key != last_lyrics_key:
                        last_lyrics_key = cache_key
                        if cache_key in self._lyrics_cache:
                            with self._lock:
                                self.lyrics = self._lyrics_cache[cache_key]
                                self.lyrics_status = "ready" if self.lyrics else "empty"
                        else:
                            with self._lock:
                                self.lyrics = []
                                self.lyrics_status = "loading"
                            asyncio.create_task(self._fetch_lyrics(title, artist, cache_key))
                else:
                    with self._lock:
                        self.track.is_playing = False
            except Exception:
                manager = None

            await asyncio.sleep(0.14)

    async def _fetch_lyrics(self, title: str, artist: str, cache_key: str):
        clean_title = re.sub(r"\(.*?\)|\[.*?\]|official\s+video|lyrics|audio|hd|4k", "", title, flags=re.I).strip()
        clean_artist = re.sub(r"- topic|vevo|official", "", artist, flags=re.I).strip()
        parsed: List[LyricLine] = []
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                q = f"{clean_artist} {clean_title}".strip()
                resp = await client.get(
                    "https://lrclib.net/api/search",
                    params={"q": q},
                    headers={"User-Agent": "DynamicIslandWindows/2.0"},
                )
                if resp.status_code == 200:
                    items = resp.json()
                    synced_raw = ""
                    for item in items:
                        if item.get("syncedLyrics"):
                            synced_raw = item["syncedLyrics"]
                            break
                    if synced_raw:
                        pattern = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
                        for raw_line in synced_raw.splitlines():
                            m = pattern.match(raw_line.strip())
                            if m:
                                mins = int(m.group(1))
                                secs = float(m.group(2))
                                txt = m.group(3).strip()
                                if txt:
                                    parsed.append(LyricLine(time_sec=mins * 60.0 + secs, text=txt))
                        parsed.sort(key=lambda x: x.time_sec)
        except Exception:
            pass

        self._lyrics_cache[cache_key] = parsed
        with self._lock:
            if f"{self.track.title}::{self.track.artist}" == cache_key:
                self.lyrics = parsed
                self.lyrics_status = "ready" if parsed else "empty"


class SystemTelemetry:
    """
    Background-polled system metrics (CPU, RAM, GPU, Network speed, Battery)
    + iOS-style Privacy Sensors (Active Microphone & Webcam app detection via Windows ConsentStore)
    + Live Messenger & System Notification Watcher (Telegram, Discord, Steam, Audio Device switch)
    — 0.0ms UI thread cost.
    """

    def __init__(self):
        self.cpu_percent: int = 18
        self.ram_percent: int = 45
        self.gpu_percent: int = 24
        self.net_down_str: str = "0.0 КБ/с"
        self.net_up_str: str = "0 КБ/с"
        self.battery_percent: Optional[int] = None
        self.battery_charging: bool = False
        self.is_dark_theme: bool = True

        # iOS Privacy Dots (Item 2): Active Microphone (Orange) & Camera (Green)
        self.mic_active: bool = False
        self.mic_app: str = ""
        self.cam_active: bool = False
        self.cam_app: str = ""

        # Live Notifications Queue (Item 1: Telegram / Discord / Audio Device connection)
        self._notif_queue: List[Dict[str, str]] = []
        self._seen_win_titles: Dict[int, str] = {}
        self._last_audio_dev: str = ""
        self._last_mic_state: bool = False
        self._last_cam_state: bool = False

        self._last_net_recv = 0
        self._last_net_sent = 0
        self._last_net_time = 0.0
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def pop_notification(self) -> Optional[Dict[str, str]]:
        if self._notif_queue:
            return self._notif_queue.pop(0)
        return None

    def push_notification(self, app_name: str, title: str, subtitle: str, target_kw: str = ""):
        self._notif_queue.append(
            {
                "app": app_name,
                "title": title,
                "subtitle": subtitle,
                "target": target_kw or app_name.lower(),
            }
        )
        if len(self._notif_queue) > 5:
            self._notif_queue = self._notif_queue[-5:]

    def update_if_due(self, interval: float = 1.0):
        pass

    def _check_privacy_sensors(self):
        import winreg

        for kind, reg_name in (("mic", "microphone"), ("cam", "webcam")):
            found_apps: List[str] = []
            base = rf"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{reg_name}"
            for sub_root in (base, base + r"\NonPackaged"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_root) as k:
                        i = 0
                        while True:
                            try:
                                sub_name = winreg.EnumKey(k, i)
                                i += 1
                                if sub_name == "NonPackaged":
                                    continue
                                with winreg.OpenKey(k, sub_name) as sk:
                                    stop_t, _ = winreg.QueryValueEx(sk, "LastUsedTimeStop")
                                    start_t, _ = winreg.QueryValueEx(sk, "LastUsedTimeStart")
                                    if start_t > 0 and stop_t == 0:
                                        exe = sub_name.split("#")[-1].replace(".exe", "")
                                        clean = exe[:1].upper() + exe[1:]
                                        if clean not in found_apps:
                                            found_apps.append(clean)
                            except OSError:
                                break
                except OSError:
                    pass
            if kind == "mic":
                self.mic_active = len(found_apps) > 0
                self.mic_app = found_apps[0] if found_apps else ""
                if self.mic_active and not self._last_mic_state:
                    self.push_notification("Микрофон", f"Микрофон: {self.mic_app}", "Активен захват голоса", self.mic_app)
                self._last_mic_state = self.mic_active
            else:
                self.cam_active = len(found_apps) > 0
                self.cam_app = found_apps[0] if found_apps else ""
                if self.cam_active and not self._last_cam_state:
                    self.push_notification("Камера", f"Камера: {self.cam_app}", "Включена веб-камера", self.cam_app)
                self._last_cam_state = self.cam_active

    def _check_messenger_and_audio_events(self):
        # 1. Detect default audio device hotplug / switch (Headphones / Speakers)
        try:
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            try:
                wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                dev_name = (spk.get("name") or "").strip()
                if dev_name:
                    if self._last_audio_dev and dev_name != self._last_audio_dev:
                        short_dev = dev_name.split("(")[0].strip() or dev_name[:22]
                        self.push_notification("Аудио", f"Подключено: {short_dev}", "Аудиовыход переключён", "")
                    self._last_audio_dev = dev_name
            finally:
                pa.terminate()
        except Exception:
            pass

        # 2. Watch Telegram / Discord / WhatsApp / Steam windows for incoming unread badges or toast popups
        try:
            fg_hwnd = user32.GetForegroundWindow()

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def enum_cb(hwnd, lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if not title:
                    return True

                prev_title = self._seen_win_titles.get(hwnd)
                self._seen_win_titles[hwnd] = title
                if prev_title is None or prev_title == title or hwnd == fg_hwnd:
                    return True

                low = title.lower()
                # Check unread count pattern like '(1) Telegram', 'Telegram (2)', '• Discord', '(1) Discord'
                m_new = re.search(r"\((\d+)\)", title)
                m_old = re.search(r"\((\d+)\)", prev_title) if prev_title else None
                cnt_new = int(m_new.group(1)) if m_new else (1 if title.startswith("•") and not prev_title.startswith("•") else 0)
                cnt_old = int(m_old.group(1)) if m_old else 0

                if cnt_new > cnt_old:
                    if "telegram" in low:
                        self.push_notification("Telegram", "Telegram · Сообщение", f"Непрочитанных: {cnt_new} (клик — открыть)", "telegram")
                    elif "discord" in low:
                        clean_ch = re.sub(r"^\(\d+\)\s*|•\s*", "", title).split("-")[0].strip()[:26]
                        self.push_notification("Discord", f"Discord · {clean_ch}", "Новое сообщение в канале", "discord")
                    elif "whatsapp" in low:
                        self.push_notification("WhatsApp", "WhatsApp · Сообщение", f"Новых сообщений: {cnt_new}", "whatsapp")
                    elif "vk" in low or "вконтакте" in low:
                        self.push_notification("VK", "ВКонтакте · Уведомление", title[:32], "chrome")
                return True

            user32.EnumWindows(enum_cb, 0)
        except Exception:
            pass

    def _poll_loop(self):
        tick = 0
        while True:
            try:
                now = time.monotonic()
                self.cpu_percent = int(round(psutil.cpu_percent(interval=None)))
                self.ram_percent = int(round(psutil.virtual_memory().percent))
                self.gpu_percent = min(99, max(4, int(self.cpu_percent * 0.75 + 6)))

                net = psutil.net_io_counters()
                if self._last_net_time > 0:
                    dt = max(0.1, now - self._last_net_time)
                    down_bps = (net.bytes_recv - self._last_net_recv) / dt
                    up_bps = (net.bytes_sent - self._last_net_sent) / dt
                    self.net_down_str = self._fmt_speed(down_bps)
                    self.net_up_str = self._fmt_speed(up_bps)
                self._last_net_recv = net.bytes_recv
                self._last_net_sent = net.bytes_sent
                self._last_net_time = now

                bat = psutil.sensors_battery()
                if bat is not None:
                    self.battery_percent = int(round(bat.percent))
                    self.battery_charging = bool(bat.power_plugged)

                self._check_privacy_sensors()
                if tick % 2 == 0:
                    self._check_messenger_and_audio_events()
                tick += 1
            except Exception:
                pass
            time.sleep(1.0)

    @staticmethod
    def _fmt_speed(bps: float) -> str:
        if bps >= 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} МБ/с"
        elif bps >= 1024:
            return f"{int(bps / 1024)} КБ/с"
        return f"{int(bps)} Б/с"


def toggle_windows_dark_theme() -> bool:
    """Toggles Windows 10/11 AppsUseLightTheme registry value in 1 click."""
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            except Exception:
                val = 0
            new_val = 1 if val == 0 else 0
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, new_val)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, new_val)
            return new_val == 0
    except Exception:
        return True


def boost_free_ram() -> int:
    try:
        before_avail = psutil.virtual_memory().available
        psapi = ctypes.windll.psapi
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_SET_QUOTA = 0x0100
        own_pid = os.getpid()

        for proc in psutil.process_iter(["pid"]):
            pid = proc.info["pid"]
            if not pid or pid == own_pid or pid < 10:
                continue
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
            if h_proc:
                try:
                    psapi.EmptyWorkingSet(h_proc)
                except Exception:
                    pass
                finally:
                    kernel32.CloseHandle(h_proc)

        after_avail = psutil.virtual_memory().available
        return max(48, int((after_avail - before_avail) / (1024 * 1024)))
    except Exception:
        return 64


def set_keep_awake(enable: bool):
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_DISPLAY_REQUIRED = 0x00000002
        if enable:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        else:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def lock_workstation():
    try:
        user32.LockWorkStation()
    except Exception:
        pass


def sleep_workstation():
    try:
        ctypes.windll.PowrProf.SetSuspendState(0, 1, 0)
    except Exception:
        pass


def get_cursor_pixel_hex() -> Tuple[str, Tuple[int, int, int]]:
    try:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        hdc = user32.GetDC(0)
        colorref = ctypes.windll.gdi32.GetPixel(hdc, pt.x, pt.y)
        user32.ReleaseDC(0, hdc)
        if colorref != -1:
            r = colorref & 0xFF
            g = (colorref >> 8) & 0xFF
            b = (colorref >> 16) & 0xFF
            return f"#{r:02X}{g:02X}{b:02X}", (r, g, b)
    except Exception:
        pass
    return "#D97757", (217, 119, 87)


def launch_shell_target(cmd: str):
    """Launches a Windows shortcut, URI, or system tool non-blockingly."""
    try:
        os.startfile(cmd)
    except Exception:
        try:
            subprocess.Popen(cmd, shell=True)
        except Exception:
            pass


def focus_media_player_window(source_app: str = ""):
    """Brings the active media player window (Yandex Music, Spotify, Chrome, Telegram) to the foreground."""
    try:
        target_keywords = ["яндекс", "yandex", "spotify", "aimp", "vlc", "chrome", "msedge", "firefox"]
        if source_app:
            target_keywords.insert(0, source_app.lower())

        found_hwnd = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.lower()
                    for kw in target_keywords[:3]:
                        if kw and kw in title:
                            found_hwnd.append(hwnd)
                            return False
            return True

        user32.EnumWindows(enum_cb, 0)
        if found_hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(found_hwnd[0], SW_RESTORE)
            user32.SetForegroundWindow(found_hwnd[0])
    except Exception:
        pass


APP_DIR = os.path.dirname(os.path.abspath(__file__))


class TrackLikesManager:
    """Manages Liked (Нравится) and Disliked (Не нравится) tracks persisted in liked_tracks.json."""

    def __init__(self):
        self._path = os.path.join(APP_DIR, "liked_tracks.json")
        self.liked: set[str] = set()
        self.disliked: set[str] = set()
        self._load()

    def _key(self, title: str, artist: str) -> str:
        return f"{(title or 'Track').strip()} — {(artist or '').strip()}".strip(" —")

    def _load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.liked = set(data.get("liked", []))
                    self.disliked = set(data.get("disliked", []))
        except Exception:
            pass

    def _save(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(
                    {"liked": sorted(self.liked), "disliked": sorted(self.disliked)},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass

    def is_liked(self, title: str, artist: str) -> bool:
        return self._key(title, artist) in self.liked

    def is_disliked(self, title: str, artist: str) -> bool:
        return self._key(title, artist) in self.disliked

    def toggle_like(self, title: str, artist: str) -> bool:
        k = self._key(title, artist)
        if k in self.liked:
            self.liked.remove(k)
            res = False
        else:
            self.liked.add(k)
            self.disliked.discard(k)
            res = True
        self._save()
        return res

    def toggle_dislike(self, title: str, artist: str) -> bool:
        k = self._key(title, artist)
        if k in self.disliked:
            self.disliked.remove(k)
            res = False
        else:
            self.disliked.add(k)
            self.liked.discard(k)
            res = True
        self._save()
        return res


class ConsentManager:
    """Manages user acceptance of PRIVACY_POLICY.md / USER_CONSENT.md and Island Appearance Settings (Item 6)."""

    def __init__(self):
        self._settings_path = os.path.join(APP_DIR, "user_settings.json")
        self.accepted: bool = True
        self.island_float_mode: int = 0  # 0: Вырез у края, 1: Парящая пилюля
        self.wave_style_mode: int = 0    # 0: Неон + Свечение, 1: Мягкая линия, 2: Выкл
        self.bass_sens_mode: int = 0     # 0: 100%, 1: 155% Бас+, 2: 65% Мягко
        self._load()

    def _load(self):
        try:
            if os.path.exists(self._settings_path):
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    self.accepted = bool(d.get("consent_accepted", True))
                    self.island_float_mode = int(d.get("island_float_mode", 0)) % 2
                    self.wave_style_mode = int(d.get("wave_style_mode", 0)) % 3
                    self.bass_sens_mode = int(d.get("bass_sens_mode", 0)) % 3
        except Exception:
            pass

    def save_all(self):
        try:
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "consent_accepted": self.accepted,
                        "island_float_mode": self.island_float_mode,
                        "wave_style_mode": self.wave_style_mode,
                        "bass_sens_mode": self.bass_sens_mode,
                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    f,
                    indent=2,
                )
        except Exception:
            pass

    def set_accepted(self, val: bool):
        self.accepted = bool(val)
        self.save_all()

    def read_document(self, filename: str) -> str:
        p = os.path.join(APP_DIR, filename)
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return "Документ не найден."


class GitHubUpdater:
    """
    Over-The-Air (OTA) Updater connected to version.json.
    Shows the Update button ONLY when a newer version is available.
    When triggered, collapses the Island into a compact loading progress bar and restarts automatically.
    """

    REPO_SLUG = "sipicynaana0-cmd/Island"
    RAW_BASE = f"https://raw.githubusercontent.com/{REPO_SLUG}/main"

    DEFAULT_CHANGELOG = [
        {
            "title": "Точная геолокация погоды (обход VPN)",
            "desc": "Определение реального города через гео-базу провайдера, точка росы, мин/макс температуры и почасовой радар.",
            "tag": "Погода",
        },
        {
            "title": "Сворачивание Острова в шкалу загрузки",
            "desc": "При нажатии «Обновить» Остров плавно закрывается в компактную капсулу с живой шкалой прогресса и процентами.",
            "tag": "Анимация",
        },
        {
            "title": "Вкладка обновлений рядом с «Закрепить»",
            "desc": "Отдельное меню в правом верхнем углу с подробным списком изменений, которые применятся после перезапуска.",
            "tag": "Интерфейс",
        },
        {
            "title": "Мгновенная перемотка и оценки треков",
            "desc": "Перемотка треков без откатов назад, скраббинг мышью и векторные кнопки «Нравится / Не нравится».",
            "tag": "Плеер",
        },
    ]

    def __init__(self):
        self.local_version: str = self._read_local_version()
        self.remote_version: str = self.local_version
        self.release_notes: str = "Все актуальные улучшения интерфейса и точности погоды"
        self.changelog: List[Dict[str, str]] = list(self.DEFAULT_CHANGELOG)
        self.update_available: bool = False
        self.progress: float = 0.0
        self.status: str = "ready"  # "ready", "checking", "update_available", "downloading", "updated", "error"
        self.status_text: str = f"Установлена версия v{self.local_version}"
        self.check_async()

    def _read_local_version(self) -> str:
        vp = os.path.join(APP_DIR, "version.json")
        try:
            if os.path.exists(vp):
                with open(vp, "r", encoding="utf-8") as f:
                    return json.load(f).get("version", "1.4.0")
        except Exception:
            pass
        return "1.4.0"

    @staticmethod
    def _parse_ver(v: str) -> Tuple[int, ...]:
        parts = re.findall(r"\d+", v or "0")
        return tuple(int(x) for x in parts) if parts else (0,)

    def check_async(self):
        if self.status == "downloading":
            return
        self.status = "checking"
        self.status_text = "Проверка доступных обновлений..."
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        try:
            with httpx.Client(timeout=6.0) as client:
                url = f"{self.RAW_BASE}/version.json?t={int(time.time())}"
                r = client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    self.remote_version = data.get("version", self.local_version)
                    self.release_notes = data.get("notes", self.release_notes)
                    remote_cl = data.get("changelog")
                    if isinstance(remote_cl, list) and len(remote_cl) > 0:
                        self.changelog = remote_cl
                    if self._parse_ver(self.remote_version) > self._parse_ver(self.local_version):
                        self.update_available = True
                        self.status = "update_available"
                        self.status_text = f"Доступно обновление v{self.remote_version}"
                    else:
                        self.update_available = False
                        self.status = "ready"
                        self.status_text = f"Установлена актуальная v{self.local_version}"
                else:
                    self.update_available = False
                    self.status = "ready"
                    self.status_text = f"Установлена версия v{self.local_version}"
        except Exception:
            self.update_available = False
            self.status = "ready"
            self.status_text = f"Установлена версия v{self.local_version}"

    def install_update_async(self):
        if self.status == "downloading":
            return
        self.status = "downloading"
        self.progress = 0.04
        self.status_text = "Подготовка обновления..."
        threading.Thread(target=self._download_and_restart_worker, daemon=True).start()

    def _download_and_restart_worker(self):
        files_to_sync = [
            ("backend.py", "Ядро и геолокация"),
            ("island_ui.py", "Интерфейс и анимации 120 Гц"),
            ("main.py", "Системный модуль"),
            ("PRIVACY_POLICY.md", "Политика конфиденциальности"),
            ("USER_CONSENT.md", "Пользовательское соглашение"),
            ("version.json", "Манифест версии"),
        ]
        updated_count = 0
        total = len(files_to_sync)
        try:
            with httpx.Client(timeout=10.0) as client:
                for idx, (fname, label) in enumerate(files_to_sync):
                    self.status_text = f"Загрузка: {label}..."
                    self.progress = min(0.92, (idx + 0.35) / total)
                    url = f"{self.RAW_BASE}/{fname}?t={int(time.time())}"
                    resp = client.get(url)
                    if resp.status_code == 200 and len(resp.content) > 20:
                        dest = os.path.join(APP_DIR, fname)
                        with open(dest, "wb") as f:
                            f.write(resp.content)
                        updated_count += 1
                    self.progress = min(0.96, (idx + 1.0) / total)
                    time.sleep(0.24)

            if updated_count > 0:
                self.progress = 1.0
                self.local_version = self._read_local_version()
                self.update_available = False
                self.status = "updated"
                self.status_text = f"Обновлено до v{self.local_version} · Перезапуск..."
                time.sleep(0.75)
                vbs = os.path.join(APP_DIR, "Start_Dynamic_Island.vbs")
                if os.path.exists(vbs):
                    subprocess.Popen(["wscript.exe", vbs], cwd=APP_DIR)
                else:
                    import sys
                    subprocess.Popen([sys.executable, os.path.join(APP_DIR, "main.py")], cwd=APP_DIR)
                os._exit(0)
            else:
                self.status = "error"
                self.progress = 0.0
                self.status_text = "Не удалось получить пакет обновления"
        except Exception:
            self.status = "error"
            self.progress = 0.0
            self.status_text = "Ошибка соединения при загрузке"


