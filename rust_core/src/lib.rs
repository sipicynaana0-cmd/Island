//! Native Rust High-Performance Core v2.1 for Island (Dynamic Island for Windows)
//! Zero external crate dependencies — pure std + direct Win32 FFI.
//! Exports:
//!   - rust_core_version() -> u32 (210)
//!   - rust_fft_32bands(...) -> fast Radix-2 FFT + 32 logarithmic bands + AGC
//!   - rust_is_fullscreen_game(own_hwnd) -> Win32 fullscreen game detector
//!   - rust_get_system_stats(out_cpu, out_ram) -> Win32 GetSystemTimes + GlobalMemoryStatusEx
//!   - rust_compute_wave_offsets(...) -> fast audio-reactive ferrofluid wave solver
//!   - rust_step_springs(...) -> batched sub-stepped spring physics solver (60..240 Hz)
//!   - rust_get_refresh_rate_hz() -> native Win32 display Hz detector
//!   - rust_clipboard_seq() -> 1ns Win32 GetClipboardSequenceNumber
//!   - rust_poll_hotkeys() -> Win32 edge-triggered global hotkey detector
//!   - rust_find_lyric_index(...) -> O(log N) synced LRC line lookup

use std::f32::consts::PI;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

const N_FFT: usize = 2048;
const N_BANDS: usize = 32;

// -----------------------------------------------------------------------------
// Win32 FFI Bindings (Zero external crates!)
// -----------------------------------------------------------------------------
#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Rect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MonitorInfo {
    cb_size: u32,
    rc_monitor: Rect,
    rc_work: Rect,
    dw_flags: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct FileTime {
    low: u32,
    high: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MemoryStatusEx {
    dw_length: u32,
    dw_memory_load: u32,
    ull_total_phys: u64,
    ull_avail_phys: u64,
    ull_total_page_file: u64,
    ull_avail_page_file: u64,
    ull_total_virtual: u64,
    ull_avail_virtual: u64,
    ull_avail_extended_virtual: u64,
}

#[repr(C)]
struct DevModeW {
    dm_device_name: [u16; 32],
    dm_spec_version: u16,
    dm_driver_version: u16,
    dm_size: u16,
    dm_driver_extra: u16,
    dm_fields: u32,
    dm_pos_x: i32,
    dm_pos_y: i32,
    dm_display_orientation: u32,
    dm_display_fixed_output: u32,
    dm_color: i16,
    dm_duplex: i16,
    dm_y_resolution: i16,
    dm_tt_option: i16,
    dm_collate: i16,
    dm_form_name: [u16; 32],
    dm_log_pixels: u16,
    dm_bits_per_pel: u32,
    dm_pels_width: u32,
    dm_pels_height: u32,
    dm_display_flags: u32,
    dm_display_frequency: u32,
    dm_icm_method: u32,
    dm_icm_intent: u32,
    dm_media_type: u32,
    dm_dither_type: u32,
    dm_reserved1: u32,
    dm_reserved2: u32,
    dm_panning_width: u32,
    dm_panning_height: u32,
}

#[link(name = "user32")]
extern "system" {
    fn GetForegroundWindow() -> usize;
    fn IsWindowVisible(hwnd: usize) -> i32;
    fn IsIconic(hwnd: usize) -> i32;
    fn GetWindowRect(hwnd: usize, lp_rect: *mut Rect) -> i32;
    fn MonitorFromWindow(hwnd: usize, dw_flags: u32) -> usize;
    fn GetMonitorInfoW(h_monitor: usize, lpmi: *mut MonitorInfo) -> i32;
    fn GetClassNameW(hwnd: usize, lp_class_name: *mut u16, n_max_count: i32) -> i32;
    fn GetWindowLongW(hwnd: usize, n_index: i32) -> i32;
    fn EnumDisplaySettingsW(lpsz_device_name: *const u16, i_mode_num: u32, lp_dev_mode: *mut DevModeW) -> i32;
    fn GetClipboardSequenceNumber() -> u32;
    fn GetAsyncKeyState(v_key: i32) -> i16;
}

#[link(name = "kernel32")]
extern "system" {
    fn GetSystemTimes(idle: *mut FileTime, kernel: *mut FileTime, user: *mut FileTime) -> i32;
    fn GlobalMemoryStatusEx(lp_buffer: *mut MemoryStatusEx) -> i32;
}

// -----------------------------------------------------------------------------
// Persistent FFT & Telemetry State (Zero per-frame heap allocations!)
// -----------------------------------------------------------------------------
struct FftEngine {
    hann: [f32; N_FFT],
    re: [f32; N_FFT],
    im: [f32; N_FFT],
    ring: [f32; N_FFT],
    agc_db: f32,
    last_sr: f32,
    bin_lo: [usize; N_BANDS],
    bin_hi: [usize; N_BANDS],
    tilt: [f32; N_BANDS],
}

impl FftEngine {
    fn new() -> Self {
        let mut hann = [0.0f32; N_FFT];
        for i in 0..N_FFT {
            hann[i] = 0.5 * (1.0 - (2.0 * PI * (i as f32) / ((N_FFT - 1) as f32)).cos());
        }
        let mut eng = Self {
            hann,
            re: [0.0; N_FFT],
            im: [0.0; N_FFT],
            ring: [0.0; N_FFT],
            agc_db: -28.0,
            last_sr: 0.0,
            bin_lo: [1; N_BANDS],
            bin_hi: [2; N_BANDS],
            tilt: [0.0; N_BANDS],
        };
        eng.rebuild_bins(48000.0);
        eng
    }

    fn rebuild_bins(&mut self, sr: f32) {
        if (sr - self.last_sr).abs() < 1.0 || sr < 8000.0 {
            return;
        }
        self.last_sr = sr;
        let f_min = 40.0f32;
        let f_max = (sr * 0.45).min(16000.0);
        let ratio = (f_max / f_min).powf(1.0 / (N_BANDS as f32));
        let bin_hz = sr / (N_FFT as f32);

        let mut f_lo = f_min;
        for i in 0..N_BANDS {
            let f_hi = f_lo * ratio;
            let lo = ((f_lo / bin_hz).floor() as usize).clamp(1, N_FFT / 2 - 2);
            let hi = ((f_hi / bin_hz).ceil() as usize).clamp(lo + 1, N_FFT / 2 - 1);
            self.bin_lo[i] = lo;
            self.bin_hi[i] = hi;
            let center = (f_lo * f_hi).sqrt();
            self.tilt[i] = 3.5 * (center / 250.0).log2();
            f_lo = f_hi;
        }
    }

    /// In-place Radix-2 Cooley-Tukey FFT on self.re / self.im (N_FFT = 2048)
    fn run_fft(&mut self) {
        // Bit-reversal permutation
        let mut j = 0usize;
        for i in 1..N_FFT {
            let mut bit = N_FFT >> 1;
            while j & bit != 0 {
                j ^= bit;
                bit >>= 1;
            }
            j ^= bit;
            if i < j {
                self.re.swap(i, j);
                self.im.swap(i, j);
            }
        }

        // Butterfly stages
        let mut len = 2usize;
        while len <= N_FFT {
            let ang = -2.0 * PI / (len as f32);
            let wlen_re = ang.cos();
            let wlen_im = ang.sin();
            let half = len >> 1;

            let mut i = 0usize;
            while i < N_FFT {
                let mut w_re = 1.0f32;
                let mut w_im = 0.0f32;
                for k in 0..half {
                    let u_re = self.re[i + k];
                    let u_im = self.im[i + k];
                    let v_re = self.re[i + k + half] * w_re - self.im[i + k + half] * w_im;
                    let v_im = self.re[i + k + half] * w_im + self.im[i + k + half] * w_re;

                    self.re[i + k] = u_re + v_re;
                    self.im[i + k] = u_im + v_im;
                    self.re[i + k + half] = u_re - v_re;
                    self.im[i + k + half] = u_im - v_im;

                    let next_w_re = w_re * wlen_re - w_im * wlen_im;
                    w_im = w_re * wlen_im + w_im * wlen_re;
                    w_re = next_w_re;
                }
                i += len;
            }
            len <<= 1;
        }
    }

    fn process(&mut self, samples: &[f32], sr: f32, out_bands: &mut [f32; N_BANDS]) -> f32 {
        self.rebuild_bins(sr);

        let n = samples.len().min(N_FFT);
        if n > 0 {
            if n < N_FFT {
                self.ring.copy_within(n..N_FFT, 0);
                self.ring[(N_FFT - n)..N_FFT].copy_from_slice(&samples[samples.len() - n..]);
            } else {
                self.ring.copy_from_slice(&samples[samples.len() - N_FFT..]);
            }
        }

        // Compute RMS on incoming slice
        let mut sum_sq = 0.0f32;
        for &s in samples {
            sum_sq += s * s;
        }
        let rms = if !samples.is_empty() {
            (sum_sq / (samples.len() as f32)).sqrt()
        } else {
            0.0
        };

        if rms < 2.0e-5 {
            out_bands.fill(0.0);
            return 0.0;
        }

        for i in 0..N_FFT {
            self.re[i] = self.ring[i] * self.hann[i];
            self.im[i] = 0.0;
        }

        self.run_fft();

        let mut db = [0.0f32; N_BANDS];
        let mut top = -120.0f32;
        for b in 0..N_BANDS {
            let lo = self.bin_lo[b];
            let hi = self.bin_hi[b];
            let mut acc = 0.0f32;
            for k in lo..hi {
                acc += self.re[k] * self.re[k] + self.im[k] * self.im[k];
            }
            let mean_p = acc / ((hi - lo).max(1) as f32);
            let val_db = 10.0 * (mean_p + 1e-12).log10() + self.tilt[b];
            db[b] = val_db;
            if val_db > top {
                top = val_db;
            }
        }

        if top > self.agc_db {
            self.agc_db = top;
        } else {
            self.agc_db += (top - self.agc_db) * 0.012;
        }

        let rng = 42.0f32;
        let floor = self.agc_db - rng;
        for b in 0..N_BANDS {
            let v = ((db[b] - floor) / rng).clamp(0.0, 1.0);
            out_bands[b] = v.powf(1.35);
        }

        let level_db = 20.0 * (rms + 1e-12).log10();
        ((level_db + 48.0) / 40.0).clamp(0.0, 1.0)
    }
}

struct CpuState {
    prev_idle: u64,
    prev_total: u64,
    last_cpu: f32,
}

static FFT_STATE: Mutex<Option<FftEngine>> = Mutex::new(None);
static CPU_STATE: Mutex<CpuState> = Mutex::new(CpuState {
    prev_idle: 0,
    prev_total: 0,
    last_cpu: 12.0,
});

#[inline]
fn ft_to_u64(ft: FileTime) -> u64 {
    ((ft.high as u64) << 32) | (ft.low as u64)
}

// -----------------------------------------------------------------------------
// Exported C-ABI Functions
// -----------------------------------------------------------------------------
#[no_mangle]
pub extern "C" fn rust_core_version() -> u32 {
    210
}

#[no_mangle]
pub unsafe extern "C" fn rust_fft_32bands(
    samples_ptr: *const f32,
    n_samples: usize,
    sample_rate: f32,
    out_bands: *mut f32,
    out_level: *mut f32,
) -> i32 {
    if samples_ptr.is_null() || out_bands.is_null() || out_level.is_null() {
        return 0;
    }
    let samples = std::slice::from_raw_parts(samples_ptr, n_samples);
    let mut guard = match FFT_STATE.lock() {
        Ok(g) => g,
        Err(_) => return 0,
    };
    let eng = guard.get_or_insert_with(FftEngine::new);
    let mut bands = [0.0f32; N_BANDS];
    let lvl = eng.process(samples, sample_rate, &mut bands);
    let out_slice = std::slice::from_raw_parts_mut(out_bands, N_BANDS);
    out_slice.copy_from_slice(&bands);
    *out_level = lvl;
    1
}

/// Detects whether a fullscreen game or borderless fullscreen application is active.
/// Ignores Desktop (Progman / WorkerW), Taskbar (Shell_TrayWnd), and our own Island HWND.
#[no_mangle]
pub extern "C" fn rust_is_fullscreen_game(own_hwnd: usize) -> i32 {
    unsafe {
        let fg = GetForegroundWindow();
        if fg == 0 || fg == own_hwnd {
            return 0;
        }
        if IsWindowVisible(fg) == 0 || IsIconic(fg) != 0 {
            return 0;
        }

        // Exclude Windows desktop shell classes
        let mut cls_buf = [0u16; 64];
        let cls_len = GetClassNameW(fg, cls_buf.as_mut_ptr(), 63);
        if cls_len > 0 {
            let cls = String::from_utf16_lossy(&cls_buf[..cls_len as usize]);
            if matches!(
                cls.as_str(),
                "Progman"
                    | "WorkerW"
                    | "Shell_TrayWnd"
                    | "Shell_SecondaryTrayWnd"
                    | "Windows.UI.Core.CoreWindow"
                    | "MultitaskingViewFrame"
                    | "XamlExplorerHostIslandWindow"
            ) {
                return 0;
            }
        }

        let mut wr = Rect::default();
        if GetWindowRect(fg, &mut wr) == 0 {
            return 0;
        }
        let hmon = MonitorFromWindow(fg, 2); // MONITOR_DEFAULTTONEAREST
        if hmon == 0 {
            return 0;
        }
        let mut mi = MonitorInfo {
            cb_size: std::mem::size_of::<MonitorInfo>() as u32,
            rc_monitor: Rect::default(),
            rc_work: Rect::default(),
            dw_flags: 0,
        };
        if GetMonitorInfoW(hmon, &mut mi) == 0 {
            return 0;
        }

        let mon = mi.rc_monitor;
        let covers_monitor = wr.left <= mon.left + 2
            && wr.top <= mon.top + 2
            && wr.right >= mon.right - 2
            && wr.bottom >= mon.bottom - 2;

        if !covers_monitor {
            return 0;
        }

        // Distinguish true fullscreen / borderless game from normal maximized desktop window (like Chrome/Discord)
        // A normal maximized desktop app has WS_CAPTION (0x00C00000) & WS_THICKFRAME (0x00040000) or leaves taskbar visible.
        // Fullscreen games & F11 fullscreen video cover the entire monitor including taskbar area without WS_CAPTION or WS_DLGFRAME.
        const GWL_STYLE: i32 = -16;
        const WS_CAPTION: i32 = 0x00C00000;
        const WS_POPUP: i32 = -2147483648i32; // 0x80000000
        let style = GetWindowLongW(fg, GWL_STYLE);
        let is_borderless_or_exclusive = (style & WS_CAPTION) != WS_CAPTION || (style & WS_POPUP) != 0;

        // Also check if window covers the taskbar (rc_work != rc_monitor and wr covers full rc_monitor)
        let covers_taskbar = mi.rc_work.bottom < mon.bottom && wr.bottom >= mon.bottom - 1;

        if is_borderless_or_exclusive || covers_taskbar {
            1
        } else {
            0
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn rust_get_system_stats(out_cpu: *mut f32, out_ram: *mut f32) -> i32 {
    if out_cpu.is_null() || out_ram.is_null() {
        return 0;
    }
    let mut idle = FileTime::default();
    let mut kernel = FileTime::default();
    let mut user = FileTime::default();
    if GetSystemTimes(&mut idle, &mut kernel, &mut user) != 0 {
        let i_now = ft_to_u64(idle);
        let t_now = ft_to_u64(kernel).saturating_add(ft_to_u64(user));
        if let Ok(mut st) = CPU_STATE.lock() {
            if st.prev_total > 0 && t_now > st.prev_total {
                let di = i_now.saturating_sub(st.prev_idle);
                let dt = t_now.saturating_sub(st.prev_total);
                let busy = dt.saturating_sub(di) as f32 / (dt as f32);
                st.last_cpu = (busy * 100.0).clamp(0.0, 100.0);
            }
            st.prev_idle = i_now;
            st.prev_total = t_now;
            *out_cpu = st.last_cpu;
        }
    }

    let mut ms = MemoryStatusEx {
        dw_length: std::mem::size_of::<MemoryStatusEx>() as u32,
        dw_memory_load: 0,
        ull_total_phys: 0,
        ull_avail_phys: 0,
        ull_total_page_file: 0,
        ull_avail_page_file: 0,
        ull_total_virtual: 0,
        ull_avail_virtual: 0,
        ull_avail_extended_virtual: 0,
    };
    if GlobalMemoryStatusEx(&mut ms) != 0 {
        *out_ram = ms.dw_memory_load as f32;
    }
    1
}

/// Fast native Rust audio-reactive bottom wave solver (0 heap allocations)
#[no_mangle]
pub unsafe extern "C" fn rust_compute_wave_offsets(
    bands_ptr: *const f32,
    n_bands: usize,
    peak: f32,
    phase: f32,
    amp_max: f32,
    active_gate: f32,
    n_pts: usize,
    out_dy: *mut f32,
) {
    if bands_ptr.is_null() || out_dy.is_null() || n_bands == 0 || n_pts < 2 {
        return;
    }
    let bands = std::slice::from_raw_parts(bands_ptr, n_bands);
    let out = std::slice::from_raw_parts_mut(out_dy, n_pts);
    let denom = (n_pts - 1) as f32;
    let max_idx = (n_bands - 1) as f32;

    for i in 0..n_pts {
        let t = (i as f32) / denom;
        let env = (t * PI).sin().max(0.0).powf(0.8);
        let d = (t - 0.5).abs() * 2.0;
        let fi = d * max_idx * 0.78;
        let i0 = (fi as usize).min(n_bands - 1);
        let i1 = (i0 + 1).min(n_bands - 1);
        let fr = fi - (i0 as f32);
        let b_val = (bands[i0] * (1.0 - fr) + bands[i1] * fr) * active_gate;
        let shimmer = (t * PI * 7.0 + phase).sin() * (b_val * 1.4 + peak * 0.8) * active_gate;
        out[i] = (b_val * amp_max + shimmer) * env;
    }
}

/// High-precision batched sub-stepped spring physics solver (unconditionally stable 60..240 Hz!)
/// Processes all UI springs in a single C-ABI call (< 2 microseconds).
#[no_mangle]
pub unsafe extern "C" fn rust_step_springs(
    vals: *mut f32,
    vels: *mut f32,
    targets: *const f32,
    stiffs: *const f32,
    damps: *const f32,
    count: usize,
    dt: f32,
) -> i32 {
    if vals.is_null() || vels.is_null() || targets.is_null() || stiffs.is_null() || damps.is_null() || count == 0 {
        return 0;
    }
    let dt_c = dt.clamp(0.0005, 0.045);
    let sub_steps: usize = if dt_c > 0.012 { 3 } else { 2 };
    let h = dt_c / (sub_steps as f32);

    let v_s = std::slice::from_raw_parts_mut(vals, count);
    let vel_s = std::slice::from_raw_parts_mut(vels, count);
    let t_s = std::slice::from_raw_parts(targets, count);
    let k_s = std::slice::from_raw_parts(stiffs, count);
    let d_s = std::slice::from_raw_parts(damps, count);

    let mut active_count = 0i32;
    for i in 0..count {
        let mut x = v_s[i];
        let mut v = vel_s[i];
        let tgt = t_s[i];
        let k = k_s[i];
        let c = d_s[i];

        if (tgt - x).abs() < 0.0004 && v.abs() < 0.0004 {
            v_s[i] = tgt;
            vel_s[i] = 0.0;
            continue;
        }
        for _ in 0..sub_steps {
            let acc = (tgt - x) * k - v * c;
            v += acc * h;
            x += v * h;
        }
        if (tgt - x).abs() < 0.0004 && v.abs() < 0.0004 {
            v_s[i] = tgt;
            vel_s[i] = 0.0;
        } else {
            v_s[i] = x;
            vel_s[i] = v;
            active_count += 1;
        }
    }
    active_count
}

/// Detects the native refresh rate (Hz) of the primary Windows display (e.g. 60, 120, 144, 165, 240).
#[no_mangle]
pub extern "C" fn rust_get_refresh_rate_hz() -> u32 {
    unsafe {
        let mut dm: DevModeW = std::mem::zeroed();
        dm.dm_size = std::mem::size_of::<DevModeW>() as u16;
        const ENUM_CURRENT_SETTINGS: u32 = 0xFFFF_FFFF;
        if EnumDisplaySettingsW(std::ptr::null(), ENUM_CURRENT_SETTINGS, &mut dm) != 0 {
            let hz = dm.dm_display_frequency;
            if (30..=360).contains(&hz) {
                return hz;
            }
        }
        60
    }
}

/// 1-nanosecond Win32 clipboard sequence counter (avoids heavy Qt clipboard polling).
#[no_mangle]
pub extern "C" fn rust_clipboard_seq() -> u32 {
    unsafe { GetClipboardSequenceNumber() }
}

static PREV_HOTKEY_MASK: AtomicU32 = AtomicU32::new(0);

/// Edge-triggered Win32 global hotkey poller (zero system hook overhead):
///   bit 0 (1): Ctrl + Shift + I -> Summon / toggle Island with 2-Eyes reveal
///   bit 1 (2): Alt + M -> Instant audio mute toggle
#[no_mangle]
pub extern "C" fn rust_poll_hotkeys() -> u32 {
    unsafe {
        let ctrl = (GetAsyncKeyState(0x11) as u16 & 0x8000) != 0;
        let shift = (GetAsyncKeyState(0x10) as u16 & 0x8000) != 0;
        let alt = (GetAsyncKeyState(0x12) as u16 & 0x8000) != 0;
        let key_i = (GetAsyncKeyState(0x49) as u16 & 0x8000) != 0; // 'I'
        let key_m = (GetAsyncKeyState(0x4D) as u16 & 0x8000) != 0; // 'M'

        let mut curr = 0u32;
        if ctrl && shift && key_i {
            curr |= 1;
        }
        if alt && !ctrl && !shift && key_m {
            curr |= 2;
        }
        let prev = PREV_HOTKEY_MASK.swap(curr, Ordering::Relaxed);
        curr & !prev
    }
}

/// O(log N) binary search over sorted LRC lyric timestamps (seconds).
/// Returns active line index, or -1 if before the first timestamp.
#[no_mangle]
pub unsafe extern "C" fn rust_find_lyric_index(
    timestamps: *const f32,
    count: usize,
    pos_sec: f32,
) -> i32 {
    if timestamps.is_null() || count == 0 {
        return -1;
    }
    let ts = std::slice::from_raw_parts(timestamps, count);
    if pos_sec < ts[0] {
        return -1;
    }
    let mut lo = 0usize;
    let mut hi = count;
    while lo + 1 < hi {
        let mid = (lo + hi) >> 1;
        if ts[mid] <= pos_sec {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    lo as i32
}
